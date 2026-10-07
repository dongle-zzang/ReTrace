"""CPU regression for NVIDIA OpenCV YAML presets and confidential traceback."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest

import yaml

from preview_diagnostics import log_safe_traceback, log_settings, tracker_diagnostic_settings


TRACKER = """%YAML:1.0
# OpenCV header used by NVIDIA tracker presets.
BaseConfig:
  minDetectorConfidence: 0
TargetManagement:
  minTrackerConfidence: 0.2
  probationAge: 3
TrajectoryManagement:
  useUniqueID: 0
ReID:
  modelEngineFile: private-model.engine
"""


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.tracker = root / 'tracker.yml'
        self.tracker.write_text(TRACKER)
        self.pgie = root / 'pgie.txt'
        self.pgie.write_text('[property]\ninterval=0\nbatch-size=25\ncluster-mode=2\n'
                             '[class-attrs-all]\npre-cluster-threshold=0.4\n')

    def logs(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            log_settings(self.pgie, 0, self.tracker)
        return output.getvalue()

    def test_opencv_header_reproduces_scanner_error_and_diagnostics_now_read_it(self):
        with self.assertRaises(yaml.scanner.ScannerError) as caught:
            yaml.safe_load(TRACKER)
        self.assertEqual((caught.exception.problem_mark.line, caught.exception.problem_mark.column), (0, 5))
        original = self.tracker.read_bytes()
        settings = tracker_diagnostic_settings(self.tracker)
        self.assertEqual(settings['TargetManagement'], {'minTrackerConfidence': 0.2, 'probationAge': 3})
        self.assertEqual(settings['TrajectoryManagement'], {'useUniqueID': 0})
        self.assertEqual(settings['ReID'], {})
        self.assertEqual(self.tracker.read_bytes(), original)
        output = self.logs()
        self.assertIn('interval=0 batch-size=25', output)
        self.assertIn("'pre-cluster-threshold': '0.4'", output)
        self.assertIn("'minTrackerConfidence': 0.2", output)
        self.assertNotIn('unavailable', output)
        self.assertNotIn('private-model', output)

    def test_standard_yaml_and_bom_crlf_opencv_yaml(self):
        for document in (TRACKER.replace('%YAML:1.0\n', ''),
                         TRACKER.replace('%YAML:1.0', '%YAML 1.1\n---'),
                         '\ufeff' + TRACKER.replace('\n', '\r\n')):
            with self.subTest(header=document.splitlines()[0]):
                self.tracker.write_text(document)
                self.assertEqual(tracker_diagnostic_settings(self.tracker)['TargetManagement']['probationAge'], 3)

    def test_bad_yaml_is_nonfatal_and_traceback_does_not_echo_content(self):
        secret = 'rtsp://test-user:test-password@192.0.2.50/video'
        self.tracker.write_text('%YAML:1.0\nsecret: "' + secret + '\n')
        output = self.logs()
        self.assertIn('diagnostics settings unavailable', output)
        self.assertIn('safe_traceback type=ScannerError', output)
        self.assertIn('file=preview_diagnostics.py function=tracker_diagnostic_settings line=', output)
        self.assertIn('file=yaml/scanner.py function=', output)
        self.assertIn('parser_location line=3 column=1', output)
        for value in (secret, 'test-user', 'test-password', '192.0.2.50', str(self.tracker)):
            self.assertNotIn(value, output)

    def test_missing_or_non_mapping_tracker_is_nonfatal(self):
        for document in ('', '[]', '42'):
            self.tracker.write_text(document)
            self.assertIn('diagnostics settings unavailable', self.logs())
        self.tracker.unlink()
        self.assertIn('safe_traceback type=FileNotFoundError', self.logs())

    def test_bad_pgie_diagnostics_do_not_abort_startup_or_echo_values(self):
        self.pgie.write_text('rtsp://test-user:test-password@192.0.2.50/video\n')
        output = self.logs()
        self.assertIn('safe_traceback type=MissingSectionHeaderError', output)
        self.assertNotIn('rtsp://', output)
        self.assertNotIn('192.0.2.50', output)
        self.assertNotIn('test-password', output)

    def test_shared_setup_safe_traceback_omits_exception_message_and_locals(self):
        output = io.StringIO()
        secret = 'rtsp://test-user:test-password@192.0.2.50/video'
        try:
            raise RuntimeError(secret)
        except RuntimeError as error:
            with contextlib.redirect_stdout(output):
                log_safe_traceback(error)
        self.assertIn('safe_traceback type=RuntimeError', output.getvalue())
        self.assertIn('function=test_shared_setup_safe_traceback_omits_exception_message_and_locals', output.getvalue())
        self.assertNotIn(secret, output.getvalue())
        self.assertNotIn('test-password', output.getvalue())


if __name__ == '__main__':
    unittest.main()
