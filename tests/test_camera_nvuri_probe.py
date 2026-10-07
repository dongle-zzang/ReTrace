"""CPU regressions for safe caps, the real pre-NVMM gate, and probe selection."""
import ast
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from camera_runtime import CameraRuntime
from rtsp_diagnostics import classify_error, source_failure_detail
from tools.check_camera_rtsp import probe
from tools.nvuri_probe import safe_caps

ROOT = Path(__file__).resolve().parents[1]
SECRET = 'rtsp://private-user:private-password@192.0.2.50/private'


class Caps:
    def __init__(self, nvmm=False, any_memory=False, media='video/x-raw', **fields):
        self.nvmm, self.any_memory, self.media, self.fields = nvmm, any_memory, media, fields
    def get_size(self):
        return 1
    def get_structure(self, _):
        return NS(get_name=lambda: self.media, has_field=lambda key: key in self.fields,
                  get_value=lambda key: self.fields.get(key))
    def get_features(self, _):
        return NS(is_any=lambda: self.any_memory, get_size=lambda: int(self.nvmm),
                  contains=lambda feature: self.nvmm and feature == 'memory:NVMM')


class NvuriTests(unittest.TestCase):
    def test_caps_whitelist_distinguishes_memory_without_exposing_extra_fields(self):
        for nvmm, expected in ((False, 'SYSTEM'), (True, 'NVMM')):
            result = safe_caps(Caps(nvmm, format='NV12', width=1920, height=1080,
                                    uri=SECRET, location=SECRET))
            self.assertEqual(result, {'media': 'video/x-raw', 'memory': expected,
                                     'format': 'NV12', 'width': 1920, 'height': 1080})
            self.assertNotIn(SECRET, json.dumps(result))
        result = safe_caps(Caps(media=SECRET, format=SECRET, width=SECRET, **{'encoding-name': SECRET}))
        self.assertEqual(result, {'media': 'other', 'memory': 'SYSTEM'})
        self.assertEqual(safe_caps(Caps(any_memory=True))['memory'], 'unknown')
        self.assertEqual(safe_caps(None)['memory'], 'unknown')
        self.assertEqual(safe_caps(Caps(media='application/x-rtp', **{'encoding-name': 'JPEG'}))['codec'], 'JPEG')

    def test_actual_source_helper_observes_pad_before_rejecting_system_memory(self):
        function = next(node for node in ast.parse((ROOT / 'app.py').read_text()).body
                        if isinstance(node, ast.FunctionDef) and node.name == 'create_source_bin')
        for nvmm in (False, True):
            with self.subTest(nvmm=nvmm):
                callbacks, seen, failures = {}, [], []
                decoder = NS(set_property=lambda *_: None,
                             connect=lambda name, callback: callbacks.update({name: callback}))
                target = [None]
                ghost = NS(get_target=lambda: target[0], set_target=lambda pad: target.__setitem__(0, pad) or True)
                bin_ = NS(add=lambda *_: None, add_pad=lambda *_: True)
                namespace = {'Gst': NS(Bin=NS(new=lambda *_: bin_),
                                      GhostPad=NS(new_no_target=lambda *_: ghost), PadDirection=NS(SRC=1)),
                             'make_element': lambda *_: decoder}
                exec(compile(ast.Module(body=[function], type_ignores=[]), 'app.py', 'exec'), namespace)
                namespace['create_source_bin'](0, SECRET, failures.append, 'nvurisrcbin',
                                               observer=lambda role, pad: seen.append((role, pad, target[0])))
                pad = NS(get_current_caps=lambda: Caps(nvmm), query_caps=lambda _: Caps(nvmm))
                with contextlib.redirect_stdout(io.StringIO()):
                    callbacks['pad-added'](decoder, pad)
                self.assertEqual(seen, [('source_pad', pad, None)])
                if nvmm:
                    self.assertIs(target[0], pad)
                    self.assertFalse(failures)
                else:
                    self.assertIsNone(target[0])
                    self.assertEqual(source_failure_detail(failures[0]), ('source_nvmm_gate', 'source_caps_error'))

    def test_caps_link_and_decode_reasons_remain_safe_runtime_details(self):
        cases = [('gst-core-error-quark', 7, '', 'source_caps_error'),
                 ('gst-core-error-quark', 10, '', 'source_caps_error'),
                 ('gst-core-error-quark', 5, '', 'source_link_error'),
                 ('gst-stream-error-quark', 1, 'streaming stopped, reason not-negotiated (-4)', 'source_caps_error'),
                 ('gst-stream-error-quark', 7, '', 'decoder_error')]
        runtime = CameraRuntime('jpeg')
        generation = runtime.begin_attempt()
        for domain, code, text, expected in cases:
            detail = classify_error(domain, code, text, SECRET)
            self.assertEqual(detail['reason'], expected)
            runtime.record_error_reason(expected, generation)
            self.assertEqual(runtime.snapshot().error_reason, expected)
            self.assertNotIn(SECRET, json.dumps(detail))
        runtime.fail('pipeline_error', generation)
        self.assertEqual(runtime.snapshot().last_error, 'pipeline_error')

    def test_source_failure_callback_keeps_backend_code_and_reports_safe_stage(self):
        tree = ast.parse((ROOT / 'preview.py').read_text())
        function = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                        and node.name == 'source_failed')
        recorded, failed = [], []
        ns = {'source_failure_detail': source_failure_detail, 'json': json, 'args': NS(diagnostics=True),
              'manager': NS(record_error_reason=lambda *args: recorded.append(args)),
              'cameras_by_source': {0: NS(camera_id='jpeg', source_id='source0')},
              'fail': lambda *args: failed.append(args)}
        exec(compile(ast.Module(body=[function], type_ignores=[]), 'preview.py', 'exec'), ns)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            ns['source_failed']('source=0: video decoder output is not NVIDIA NVMM', 0)
        self.assertEqual(recorded, [(0, 'source_caps_error')])
        self.assertEqual(failed, [('pipeline_error', '0')])
        self.assertIn('source_nvmm_gate', output.getvalue())
        self.assertNotIn(SECRET, output.getvalue())

    def test_probe_mode_is_private_stdin_and_uses_native_output_isolated_worker(self):
        def runner(argv, **options):
            self.assertNotIn(SECRET, ' '.join(argv))
            request = json.loads(options['input'])
            self.assertEqual(request['source_mode'], 'nvurisrcbin')
            self.assertEqual(request['uri'], SECRET)
            return NS(stdout=b'{"rtsp_connection":"failed","failure_stage":"source_nvmm_gate"}')
        self.assertEqual(probe(SECRET, runner=runner, source_mode='nvurisrcbin')['failure_stage'], 'source_nvmm_gate')
        script = '''
import os
from tools import check_camera_rtsp, nvuri_probe
def fake(uri, timeout, latency, emit):
    os.write(1, uri.encode())
    os.write(2, uri.encode())
    emit({'rtsp_connection': 'failed', 'failure_stage': 'source_nvmm_gate'})
nvuri_probe.gst_nvuri_probe = fake
check_camera_rtsp.worker()
'''
        response = subprocess.run([sys.executable, '-c', script], input=json.dumps(
            {'uri': SECRET, 'timeout': 1, 'latency': 1000, 'source_mode': 'nvurisrcbin'}).encode(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, check=True)
        self.assertEqual(json.loads(response.stdout)['failure_stage'], 'source_nvmm_gate')
        self.assertEqual(response.stderr, b'')
        self.assertNotIn(SECRET.encode(), response.stdout)


if __name__ == '__main__':
    unittest.main()
