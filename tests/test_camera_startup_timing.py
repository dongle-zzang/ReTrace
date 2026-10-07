"""Startup timing tests without GI, PyDS, RTSP, or a GPU."""

import ast
import contextlib
import io
from pathlib import Path
import threading
from types import SimpleNamespace as NS
import unittest

from preview_mjpeg import FrameStore, serve_mjpeg
from startup_timing import StartupTiming, source_observer, bus_observer


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        self.timing = StartupTiming([NS(camera_id='a'), NS(camera_id='b')], 10.0,
                                    'test', clock=lambda: self.now)

    def test_first_arrival_thread_safe_and_partial_summary(self):
        self.now = 12.0
        threads = [threading.Thread(target=self.timing.mark, args=('first_mjpeg_available', 0)) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.timing.mark('online', 0)
        self.now = 15.0
        self.timing.mark('first_mjpeg_available', 0)
        result = self.timing.summary()
        self.assertEqual(result['first_camera_ms'], 2000)
        self.assertEqual(result['missing_frames'], ['b'])
        self.assertIsNone(result['all_configured_online_ms'])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.timing.drain()
            self.timing.drain()
        self.assertEqual(len(output.getvalue().splitlines()), 2)

    def test_spread_and_explicit_entry_timestamp(self):
        self.timing.mark('python_entry', at=10.5)
        self.now = 12
        self.timing.mark('first_mjpeg_available', 0)
        self.timing.mark('online', 0)
        self.now = 15
        self.timing.mark('first_mjpeg_available', 1)
        self.timing.mark('online', 1)
        result = self.timing.summary()
        self.assertEqual(result['shared_ms']['python_entry'], 500)
        self.assertEqual(result['first_frame_spread_ms'], 3000)
        self.assertEqual(result['all_configured_online_ms'], 5000)

    def test_http_available_precedes_flush_and_requires_client(self):
        store = FrameStore()
        store.startup_observer = lambda stage: self.timing.mark(stage, 0)
        store.put(b'jpeg')
        self.assertNotIn('first_http_frame_flushed', self.timing.summary()['cameras_ms']['a'])
        class Writer(io.BytesIO):
            def flush(self):
                self.assert_available = 'first_mjpeg_available' in self_outer.timing.summary()['cameras_ms']['a']
                store.close()
        self_outer = self
        writer = Writer()
        handler = NS(send_response=lambda *_: None, send_header=lambda *_: None,
                     end_headers=lambda: None, connection=NS(settimeout=lambda *_: None), wfile=writer)
        self.now = 13
        serve_mjpeg(handler, store)
        self.assertTrue(writer.assert_available)
        self.assertIn(b'jpeg', writer.getvalue())
        self.assertEqual(self.timing.summary()['cameras_ms']['a']['first_http_frame_flushed'], 3000)

    def test_native_signals_never_inspect_sensitive_payloads(self):
        class Element:
            def __init__(self, name):
                self.name, self.callbacks, self.probes = name, {}, {}
            def get_factory(self):
                return NS(get_name=lambda: self.name)
            def connect(self, signal, callback):
                self.callbacks[signal] = callback
            def get_static_pad(self, name):
                return NS(add_probe=lambda _type, callback: self.probes.update({name: callback}))
        gst = NS(Element=Element, PadProbeType=NS(BUFFER=1),
                 PadProbeReturn=NS(OK='ok', REMOVE='remove'), CLOCK_TIME_NONE=-1)
        observe = source_observer(self.timing, 0, gst)
        rtsp, decoder = Element('rtspsrc'), Element('nvv4l2decoder')
        observe('child', rtsp)
        observe('child', rtsp)
        self.assertTrue(rtsp.callbacks['before-send'](rtsp, 'rtsp://user:secret@private/video'))
        rtsp.callbacks['on-sdp'](rtsp, 'private SDP')
        observe('child', decoder)
        self.now = 11
        self.assertEqual(decoder.probes['src'](None, NS(get_buffer=lambda: NS(pts=123))), 'remove')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.timing.drain()
        self.assertNotIn('private', output.getvalue())
        self.assertNotIn('secret', output.getvalue())
        stages = self.timing.summary()['cameras_ms']['a']
        self.assertEqual(stages['decoder_created'], 0)
        self.assertEqual(stages['first_decoded_frame'], 1000)

    def test_batch_probe_attributes_only_present_sources_and_inferred_frames(self):
        tree = ast.parse(Path('preview.py').read_text())
        probe = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == 'startup_probe')
        callbacks = []
        frames = [NS(pad_index=1, buf_pts=99, bInferDone=False)]
        namespace = {'startup': self.timing,
                     'Gst': NS(PadProbeType=NS(BUFFER=1), CLOCK_TIME_NONE=-1,
                               PadProbeReturn=NS(OK='ok', REMOVE='remove')),
                     'camera_for_frame': lambda frame: None,
                     'pyds': NS(gst_buffer_get_nvds_batch_meta=lambda _: NS(frame_meta_list=NS(data=frames[0], next=None)),
                                NvDsFrameMeta=NS(cast=lambda frame: frame))}
        exec(compile(ast.Module(body=[probe], type_ignores=[]), 'preview.py', 'exec'), namespace)
        namespace['startup_probe'](NS(add_probe=lambda _type, cb: callbacks.append(cb)), 'first_pgie_output', batched=True)
        buffer = object()
        info = NS(get_buffer=lambda: buffer)
        self.assertEqual(callbacks[0](None, info), 'ok')
        self.assertFalse(self.timing.complete('first_pgie_output'))
        self.assertNotIn('first_inference_done', self.timing.summary()['cameras_ms']['b'])
        frames[0].bInferDone = True
        callbacks[0](None, info)
        frames[0] = NS(pad_index=0, buf_pts=50, bInferDone=True)
        self.assertEqual(callbacks[0](None, info), 'remove')
        self.assertTrue(self.timing.complete('first_inference_done'))

    def test_bus_timestamps_and_progress_do_not_emit_private_text(self):
        pipeline = object()
        gst = NS(MessageType=NS(STATE_CHANGED=1, PROGRESS=2),
                 State=NS(READY=1, PAUSED=2, PLAYING=3),
                 ProgressType=NS(START=1, CONTINUE=2, COMPLETE=3), BusSyncReply=NS(PASS='pass'))
        observer = bus_observer(self.timing, gst, {'pipeline': pipeline}, lambda _: ('0', 'rtsp_error'))
        self.now = 12
        message = NS(type=1, src=pipeline, parse_state_changed=lambda: (2, 3, 0))
        self.assertEqual(observer(None, message, None), 'pass')
        message = NS(type=2, src=NS(get_factory=lambda: NS(get_name=lambda: 'rtspsrc')),
                     parse_progress=lambda: (2, 'connect', 'rtsp://user:secret@private/video'))
        observer(None, message, None)
        self.now = 15
        message.parse_progress = lambda: (3, 'open', 'private address')
        observer(None, message, None)
        result = self.timing.summary()
        self.assertEqual(result['shared_ms']['pipeline_playing'], 2000)
        self.assertEqual(result['cameras_ms']['a']['rtsp_connect_start'], 2000)
        self.assertEqual(result['cameras_ms']['a']['rtsp_open_complete'], 5000)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.timing.drain()
        self.assertNotIn('private', output.getvalue())
        self.assertNotIn('secret', output.getvalue())


if __name__ == '__main__':
    unittest.main()
