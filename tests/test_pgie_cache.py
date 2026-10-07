"""CPU regressions for engine identity, native output paths and config overrides."""
import ast
import configparser
import contextlib
import errno
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import pgie_cache

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = {"gpu": "test GPU", "compute": [7, 0], "tensorrt": 8601, "nvdsinfer": "test"}


class EngineCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.model = self.root / 'model.onnx'
        self.model.write_bytes(b'model version 1')
        (self.root / 'labels.txt').write_text('person\nbag\nface\n')
        self.props = {'onnx-file': str(self.model), 'gpu-id': '0', 'batch-size': '2',
                      'network-mode': '2', 'infer-dims': '3;544;960'}

    def key(self, **changes):
        return pgie_cache.engine_cache_key(dict(self.props, **changes), RUNTIME)

    def prepare(self, changes=None, batch=2, threshold='0.2'):
        # Execute the production config function without importing GI/PyDS.
        config = configparser.ConfigParser(interpolation=None)
        config.read_dict({'property': dict(self.props, **{
            'labelfile-path': str(self.root / 'labels.txt'), 'num-detected-classes': '3'}),
            'class-attrs-all': {'pre-cluster-threshold': threshold}})
        config['property'].update(changes or {})
        tree = ast.parse((ROOT / 'app.py').read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                        and n.name == 'prepare_pgie_config')
        namespace = {'Path': Path, 'PGIE_ID': 1, 'PRECISIONS': pgie_cache.PRECISIONS,
                     'prepare_engine_cache': pgie_cache.prepare_engine_cache,
                     'load_peoplenet_config': lambda: (self.root / 'sample.txt', config)}
        exec(compile(ast.Module(body=[function], type_ignores=[]), 'app.py', 'exec'), namespace)
        with patch('pgie_cache.engine_runtime_identity', return_value=RUNTIME), contextlib.redirect_stdout(io.StringIO()):
            path, _ = namespace['prepare_pgie_config'](self.root / 'cache', batch)
        generated = configparser.ConfigParser(interpolation=None)
        generated.read(path)
        return Path(path), generated

    def test_same_settings_reuse_native_engine_path_after_process_restart(self):
        path, config = self.prepare()
        props = config['property']
        engine = Path(props['model-engine-file'])
        self.assertTrue(engine.is_absolute())
        self.assertEqual(engine.parent, (self.root / 'cache/pgie'))
        self.assertEqual(str(engine), props['onnx-file'] + '_b2_gpu0_fp16.engine')
        self.assertFalse(Path(props['onnx-file']).is_symlink())
        self.assertEqual(Path(props['onnx-file']).resolve().parent, engine.parent)
        engine.write_bytes(b'simulated native serialized engine')
        stamp = engine.stat().st_mtime_ns
        # Independent interpreter: no in-memory mapping can make this pass.
        script = ('import json,sys; from pathlib import Path; from pgie_cache import prepare_engine_cache; '
                  'p=json.loads(sys.argv[1]); prepare_engine_cache(p, sys.argv[2], json.loads(sys.argv[3])); '
                  'print(p["model-engine-file"])')
        import json
        result = subprocess.run([sys.executable, '-c', script, json.dumps(self.props),
                                 str(self.root / 'cache'), json.dumps(RUNTIME)],
                                cwd=ROOT, check=True, capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), str(engine))
        self.assertEqual(engine.stat().st_mtime_ns, stamp)
        self.assertEqual(self.prepare()[0], path)

    def test_threshold_and_postprocessing_keep_engine_but_update_config(self):
        first, before = self.prepare(threshold='0.4')
        second, after = self.prepare(threshold='0.2')
        self.assertEqual(first, second)
        self.assertEqual(before['property']['model-engine-file'], after['property']['model-engine-file'])
        self.assertEqual(after['class-attrs-all']['pre-cluster-threshold'], '0.2')
        self.assertEqual(after['class-attrs-0']['pre-cluster-threshold'], '0.2')
        for key, value in {'interval': '5', 'filter-out-class-ids': '1;2', 'cluster-mode': '4',
                           'net-scale-factor': '0.5', 'offsets': '1;2;3', 'num-detected-classes': '4',
                           'model-engine-file': '/ignored.engine', 'labelfile-path': '/ignored.txt',
                           'nms-iou-threshold': '0.8', 'pre-cluster-threshold': '0.1'}.items():
            with self.subTest(key=key):
                self.assertEqual(self.key(), self.key(**{key: value}))

    def test_batch_precision_and_build_options_change_identity(self):
        _, base = self.prepare()
        engine = base['property']['model-engine-file']
        self.assertNotEqual(engine, self.prepare(batch=4)[1]['property']['model-engine-file'])
        for mode, precision in [('0', 'fp32'), ('1', 'int8')]:
            _, config = self.prepare({'network-mode': mode})
            self.assertEqual(config['property']['network-mode'], mode)
            self.assertNotEqual(engine, config['property']['model-engine-file'])
            self.assertTrue(config['property']['model-engine-file'].endswith(f'_{precision}.engine'))
        for key, value in {'infer-dims': '3;272;480', 'workspace-size': '2048',
                           'force-implicit-batch-dim': '1', 'gpu-id': '1',
                           'output-io-formats': 'output:fp16:chw',
                           'layer-device-precision': 'conv:fp16:gpu',
                           'enable-dla': '1', 'output-blob-names': 'new-output'}.items():
            with self.subTest(key=key):
                self.assertNotEqual(self.key(), self.key(**{key: value}))

    def test_model_content_not_timestamp_or_location_is_identity(self):
        original = self.key()
        original_engine = self.prepare()[1]['property']['model-engine-file']
        stat = self.model.stat()
        os.utime(self.model, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000))
        self.assertEqual(original, self.key())
        copy = self.root / 'copy.onnx'
        copy.write_bytes(self.model.read_bytes())
        self.assertEqual(original, self.key(**{'onnx-file': str(copy)}))
        self.model.write_bytes(b'model version 2')
        os.utime(self.model, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertNotEqual(original, self.key())
        self.assertNotEqual(original_engine, self.prepare()[1]['property']['model-engine-file'])

    def test_calibration_and_runtime_compatibility(self):
        calibration = self.root / 'calibration.bin'
        calibration.write_bytes(b'calibration one')
        self.props['int8-calib-file'] = str(calibration)
        _, config = self.prepare({'network-mode': '1', 'int8-calib-file': 'calibration.bin'})
        self.assertEqual(config['property']['int8-calib-file'], str(calibration))
        self.assertNotIn('int8-calib-file', self.prepare()[1]['property'])
        fp16 = self.key()
        int8 = self.key(**{'network-mode': '1'})
        calibration.write_bytes(b'calibration two')
        self.assertEqual(fp16, self.key())
        self.assertNotEqual(int8, self.key(**{'network-mode': '1'}))
        for key, value in {'gpu': 'other GPU', 'compute': [8, 0], 'tensorrt': 8602, 'nvdsinfer': 'other'}.items():
            self.assertNotEqual(fp16, pgie_cache.engine_cache_key(self.props, dict(RUNTIME, **{key: value})))

    def test_copy_fallback_and_unproven_legacy_engines_are_not_imported(self):
        legacy = Path(str(self.model) + '_b2_gpu0_fp16.engine')
        legacy.write_bytes(b'unverified engine')
        old_cache = self.root / 'cache/pgie'
        old_cache.mkdir(parents=True)
        (old_cache / 'peoplenet_b2_gpu0_fp16_olddigest.engine').write_bytes(b'old engine')
        with patch('pgie_cache.os.link', side_effect=OSError(errno.EXDEV, 'cross-device')):
            _, config = self.prepare()
        props = config['property']
        self.assertEqual(Path(props['onnx-file']).read_bytes(), self.model.read_bytes())
        self.assertFalse(Path(props['model-engine-file']).exists())
        self.assertEqual(legacy.read_bytes(), b'unverified engine')

    def test_config_and_gobject_paths_agree_before_state_change(self):
        path, config = self.prepare()
        calls = []
        class Element:
            def set_property(self, name, value):
                calls.append((name, value))
        pgie_cache.configure_pgie(Element(), path, 2)
        self.assertEqual(calls, [('config-file-path', str(path)),
                               ('model-engine-file', config['property']['model-engine-file']),
                               ('batch-size', 2)])

    def test_unknown_custom_builder_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'native PeopleNet'):
            self.prepare({'engine-create-func-name': 'UnknownBuilder'})

    def test_native_precision_fallback_is_reused_only_in_current_build_namespace(self):
        _, config = self.prepare({'network-mode': '1'})
        requested = Path(config['property']['model-engine-file'])
        actual = requested.with_name(requested.name.replace('_int8.engine', '_fp16.engine'))
        actual.write_bytes(b'native precision fallback')
        _, warm = self.prepare({'network-mode': '1'})
        self.assertEqual(warm['property']['model-engine-file'], str(actual))
        _, changed = self.prepare({'network-mode': '1', 'workspace-size': '2048'})
        self.assertNotEqual(changed['property']['model-engine-file'], str(actual))

    def test_unknown_runtime_does_not_share_a_cache_key(self):
        with patch('pgie_cache.ctypes.CDLL', side_effect=OSError('unavailable')):
            with self.assertRaisesRegex(ValueError, 'cache environment'):
                pgie_cache.engine_runtime_identity(0)


if __name__ == '__main__':
    unittest.main()
