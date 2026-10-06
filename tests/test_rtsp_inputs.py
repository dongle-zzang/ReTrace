"""Exercise the real argument parsers without loading GPU-only dependencies."""

import argparse
import ast
import contextlib
import io
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]


def load_parsers():
    namespace = {"argparse": argparse, "os": os, "urlsplit": urlsplit,
                 "__doc__": "Input configuration test"}
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
        for parser in load_parsers():
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


if __name__ == "__main__":
    unittest.main()
