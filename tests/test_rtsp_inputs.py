"""Exercise the real argument parsers without loading GPU-only dependencies."""

import argparse
import ast
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest

from camera_config import CameraConfigError, load_cameras
from camera_runtime import CameraRuntime
from unittest.mock import patch
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]


def load_parsers():
    namespace = {"argparse": argparse, "os": os, "urlsplit": urlsplit,
                 "__doc__": "Input configuration test", "ROOT": ROOT, "Path": Path,
                 "CameraConfigError": CameraConfigError, "load_cameras": load_cameras, "CameraRuntime": CameraRuntime}
    import sys
    namespace["sys"] = sys
    parsers = []
    for filename, names in (
        ("app.py", {"SafeArgumentParser", "resolve_inputs", "parse_args"}),
        ("preview.py", {"SafeParser", "parse_args"}),
    ):
        tree = ast.parse((ROOT / filename).read_text())
        tree.body = [node for node in tree.body
                     if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
        exec(compile(tree, filename, "exec"), namespace)
        parsers.append(namespace["parse_args"])
    return parsers


class RTSPInputTests(unittest.TestCase):
    def run_parsers(self, environment, arguments, expected=None):
        for parser in load_parsers()[:1]:
            with self.subTest(parser=parser.__code__.co_filename):
                with patch.dict(os.environ, environment, clear=True), \
                        patch("sys.argv", ["retrace", *arguments]):
                    if expected is not None:
                        self.assertEqual(parser().input, expected)
                    else:
                        error = io.StringIO()
                        with contextlib.redirect_stderr(error), self.assertRaises(SystemExit) as caught:
                            parser()
                        self.assertEqual(caught.exception.code, 2)
                        for value in environment.values():
                            if value:
                                self.assertNotIn(value, error.getvalue())

    def test_environment_inputs_preserve_order_and_skip_empty_values(self):
        first, second = "rtsp://camera1.example/video", "rtsp://camera2.example/video"
        self.run_parsers({"RTSP_URL": first, "RTSP_URL_2": second}, [], [first, second])
        self.run_parsers({"RTSP_URL": "", "RTSP_URL_2": second}, [], [second])
        self.run_parsers({"RTSP_URL": "  " + first + "  "}, [], [first])

    def test_cli_replaces_environment_even_when_environment_is_invalid(self):
        first, second = "rtsp://camera1.example/video", "rtsp://camera2.example/video"
        self.run_parsers({"RTSP_URL": "invalid-private-input"},
                         ["--input", first, "--input", second], [first, second])

    def test_missing_and_invalid_environment_fail_without_exposing_values(self):
        self.run_parsers({}, [])
        self.run_parsers({"RTSP_URL": "invalid-private-input"}, [])
        self.run_parsers({"RTSP_URL": "rtsp://camera.example:invalid/video"}, [])

    def test_preview_cli_overrides_config_and_environment(self):
        parser = load_parsers()[1]
        with patch.dict(os.environ, {"RTSP_URL": "private-invalid"}, clear=True), \
                patch("sys.argv", ["preview.py", "--input", "rtsp://camera.example/v", "--diagnostics"]):
            args = parser()
        self.assertEqual(args.cameras[0].camera_id, "cli_0")
        self.assertEqual(args.input, ["rtsp://camera.example/v"])

    def test_preview_drop_on_latency_defaults_to_true_and_can_be_disabled(self):
        parser = load_parsers()[1]
        for flags, expected in (([], True), (['--rtsp-drop-on-latency'], True),
                                (['--no-rtsp-drop-on-latency'], False)):
            with patch('sys.argv', ['preview.py', '--input', 'rtsp://camera.example/v', *flags]):
                self.assertEqual(parser().rtsp_drop_on_latency, expected)

    def test_startup_subset_requires_diagnostics_and_preserves_order(self):
        parser = load_parsers()[1]
        inputs = [item for i in range(4) for item in ('--input', f'rtsp://camera{i}.example/v')]
        with patch('sys.argv', ['preview.py', *inputs, '--diagnostics', '--startup-source-count', '2']):
            args = parser()
        self.assertEqual([camera.camera_id for camera in args.cameras], ['cli_0', 'cli_1'])
        self.assertEqual(len(args.input), 2)
        for extra in (['--startup-source-count', '2'],
                      ['--diagnostics', '--startup-source-count', '8']):
            with patch('sys.argv', ['preview.py', *inputs, *extra]), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    parser()
                self.assertEqual(caught.exception.code, 2)

    def test_preview_port_and_diagnostics_accept_environment_inputs(self):
        preview_parser = load_parsers()[1]
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "cameras.yaml"
            config.write_text("cameras: [{id: second, floor: 2, name: Second, rtsp_env: RTSP_URL_2}]\n")
            for arguments, environment, expected_port in (
                (["--diagnostics"], {"WEB_PORT": "40225"}, 40225),
                (["--port", "40225", "--diagnostics"], {"WEB_PORT": "invalid"}, 40225),
                (["--diagnostics"], {}, 40225),
            ):
                with self.subTest(arguments=arguments, environment=environment):
                    environment = {**environment, "RTSP_URL_2": "rtsp://camera.example/video"}
                    with patch.dict(os.environ, environment, clear=True), \
                            patch("sys.argv", ["preview.py", "--cameras", str(config),
                                               "--env-file", str(Path(directory) / "missing.env"), *arguments]):
                        args = preview_parser()
                    self.assertEqual(args.port, expected_port)
                    self.assertTrue(args.diagnostics)
                    self.assertEqual(args.input, [environment["RTSP_URL_2"]])

    def test_preview_missing_input_reports_cause_for_both_commands(self):
        preview_parser = load_parsers()[1]
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "cameras.yaml"
            config.write_text("cameras: [{id: first, floor: 1, name: First, rtsp_env: FIRST_RTSP}]\n")
            for arguments in (["--diagnostics"], ["--port", "40225", "--diagnostics"]):
                with patch.dict(os.environ, {}, clear=True), \
                        patch("sys.argv", ["preview.py", "--cameras", str(config),
                                           "--env-file", str(Path(directory) / "missing.env"), *arguments]):
                    error = io.StringIO()
                    with contextlib.redirect_stderr(error), self.assertRaises(SystemExit) as caught:
                        preview_parser()
                self.assertEqual(caught.exception.code, 2)
                self.assertIn("Missing RTSP input", error.getvalue())
                self.assertIn("id=first", error.getvalue())
                self.assertIn("FIRST_RTSP", error.getvalue())
                self.assertNotIn("Invalid arguments", error.getvalue())


if __name__ == "__main__":
    unittest.main()
