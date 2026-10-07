"""CPU checks for the canonical Docker command and private-data-safe inspect."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml
from test_rtsp_inputs import load_parsers

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('startup_check', ROOT / 'tools/check_container_startup.py')
startup_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(startup_check)


class CanonicalStartupTests(unittest.TestCase):
    def test_docker_and_compose_defaults_match_camera_preview(self):
        service = yaml.safe_load((ROOT / 'compose.yml').read_text())['services']['retrace']
        dockerfile = (ROOT / 'Dockerfile').read_text().splitlines()
        command = json.loads(next(line[4:] for line in reversed(dockerfile) if line.startswith('CMD ')))
        self.assertEqual(command, service['command'])
        self.assertEqual(command[:3], ['python3', '-u', '/workspace/ReTrace/preview.py'])
        self.assertEqual(command[command.index('--cameras') + 1], '/workspace/ReTrace/configs/cameras.yaml')
        self.assertEqual(command[command.index('--env-file') + 1], '/workspace/ReTrace/.env')
        self.assertNotIn('entrypoint', service)
        self.assertEqual(service['container_name'], 'retrace')
        self.assertIn('.:/workspace/ReTrace', service['volumes'])
        self.assertNotIn('RTSP_URL', service['environment'])
        self.assertNotIn('RTSP_URL_2', service['environment'])
        self.assertIn("/streams.json", service['healthcheck']['test'][-1])

    def test_default_preview_uses_only_enabled_cctv_keys_from_file(self):
        parser = load_parsers()[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'configs').mkdir()
            (root / 'configs/cameras.yaml').write_text('''cameras:
  - {id: first, floor: 1, name: First, rtsp_env: CCTV_FIRST_RTSP}
  - {id: disabled, floor: 2, name: Disabled, rtsp_env: CCTV_DISABLED_RTSP, enabled: false}
  - {id: third, floor: 3, name: Third, rtsp_env: CCTV_THIRD_RTSP}
''')
            (root / '.env').write_text('CCTV_FIRST_RTSP=rtsp://first.example/video\nCCTV_THIRD_RTSP=rtsp://third.example/video\n')
            with patch.dict(parser.__globals__, ROOT=root), patch.dict(os.environ, {}, clear=True), \
                    patch('sys.argv', ['preview.py', '--diagnostics']):
                args = parser()
            self.assertEqual([(camera.source_id, camera.camera_id, camera.rtsp_env) for camera in args.cameras],
                             [(0, 'first', 'CCTV_FIRST_RTSP'), (1, 'third', 'CCTV_THIRD_RTSP')])
            self.assertTrue(args.diagnostics)

    def test_inspect_summary_does_not_expose_secret_values(self):
        container = {'Config': {'Cmd': ['python3', 'preview.py', '--input', 'rtsp://secret-user:secret-pass@private-host/video'],
                                'Env': ['CCTV_FIRST_RTSP=private-secret'],
                                'WorkingDir': '/workspace/ReTrace'},
                     'Mounts': [{'Type': 'bind', 'Source': '/private-host/path', 'Destination': '/workspace/ReTrace'}],
                     'State': {'Status': 'running', 'Health': {'Status': 'healthy'}}}
        result = startup_check.summarize(container)
        encoded = json.dumps(result)
        for private in ('secret-user', 'secret-pass', 'private-host', 'private-secret'):
            self.assertNotIn(private, encoded)
        self.assertTrue(result['preview_command'])
        self.assertTrue(result['explicit_input'])
        self.assertTrue(result['project_bind_mount'])
        self.assertEqual(result['health'], 'healthy')


if __name__ == '__main__':
    unittest.main()
