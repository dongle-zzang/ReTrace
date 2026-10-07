"""CPU checks for API readiness independent of the removed root page."""
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("check_preview", ROOT / "tools/check_preview.py")
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class Response(io.BytesIO):
    def __init__(self, body, status=200):
        super().__init__(body)
        self.status = status


class CheckPreviewTests(unittest.TestCase):
    def run_check(self, body, status=200):
        listeners = "header\n0: 00000000:9D21 00000000:0000 0A\n"
        with patch.object(checker.Path, "read_text", return_value=listeners), \
                patch.object(checker, "urlopen", return_value=Response(body, status)) as fetch, \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            checker.main()
            fetch.assert_called_once_with("http://127.0.0.1:40225/streams.json", timeout=5)
            return output.getvalue()

    def test_streams_ready_without_requesting_root(self):
        output = self.run_check(b'[{"id": 0, "url": "/mjpeg/source0"}]')
        self.assertIn("/streams.json HTTP 200", output)
        self.assertIn("1 configured streams", output)

    def test_invalid_stream_lists_fail_readiness(self):
        for body in (b"[]", b"{}"):
            with self.subTest(body=body), self.assertRaisesRegex(SystemExit, "no configured streams"):
                self.run_check(body)

    def test_non_200_stream_response_fails_readiness(self):
        with self.assertRaisesRegex(SystemExit, "HTTP status 503"):
            self.run_check(b"[]", status=503)

    def test_missing_listener_fails_readiness(self):
        with patch.object(checker.Path, "read_text", return_value="header\n"), \
                patch.object(checker, "urlopen") as fetch, \
                self.assertRaisesRegex(SystemExit, "no IPv4 listener"):
            checker.main()
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
