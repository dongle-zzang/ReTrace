"""Run in retrace: python3 -m unittest discover -s tests -p 'test_*.py'."""

import functools
import io
import json
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

PROJECT_CACHE = Path(__file__).resolve().parents[1] / ".cache"
PROJECT_CACHE.mkdir(exist_ok=True)
os.environ["GST_REGISTRY"] = str(PROJECT_CACHE / "gstreamer-registry.bin")

from preview import Gst, PreviewHandler, build_output, parse_args
from preview_mjpeg import FrameStore
from preview_timing import FrameTiming


class PreviewTests(unittest.TestCase):
    def test_jpeg_output_links_without_rtsp(self):
        Gst.init(None)
        with patch("sys.argv", ["preview.py", "--input", "rtsp://camera.example/video"]):
            args = parse_args()
        output = build_output(0, args, FrameStore(), self.fail)
        try:
            self.assertIsNotNone(output.get_static_pad("sink"))
            sink = output.get_by_name("jpeg-sink-0")
            self.assertTrue(sink.get_property("sync"))
            self.assertEqual(sink.get_property("max-buffers"), 1)
            self.assertTrue(sink.get_property("drop"))
        finally:
            output.set_state(Gst.State.NULL)

    def test_store_keeps_latest_and_shutdown_wakes_waiter(self):
        store = FrameStore()
        store.put(b"old")
        store.put(b"latest")
        self.assertEqual(store.wait_next(0), (2, b"latest", False))
        result = []
        waiter = threading.Thread(target=lambda: result.append(store.wait_next(2, 30)))
        waiter.start()
        store.close()
        waiter.join(timeout=1)
        self.assertFalse(waiter.is_alive())
        self.assertEqual(result, [(2, b"latest", True)])
        store.put(b"ignored after shutdown")
        self.assertEqual(store.wait_next(0), (2, b"latest", True))

    def test_timing_separates_arrival_jitter_from_pts(self):
        now = [0.0]
        counter = FrameTiming(clock=lambda: now[0])
        counter.observe(0)
        now[0] = 0.033
        counter.observe(33_000_000)
        now[0] = 0.2
        counter.observe(66_000_000)
        first = counter.snapshot()
        self.assertEqual(first["frames"], 3)
        self.assertEqual(first["fps"], 15)
        self.assertEqual(first["max_gap_ms"], 167)
        self.assertEqual(first["gaps_over_100ms"], 1)
        self.assertEqual(first["pts_min_ms"], 33)
        self.assertEqual(first["pts_max_ms"], 33)
        now[0] = 0.233
        counter.observe(60_000_000)
        second = counter.snapshot()
        self.assertEqual(second["frames"], 4)
        self.assertEqual(second["pts_backwards"], 1)
        self.assertEqual(second["gaps_over_100ms"], 0)

    def test_cli_defaults_and_multiple_inputs(self):
        with patch("sys.argv", ["preview.py", "--diagnostics",
                               "--input", "rtsp://camera1.example/video",
                               "--input", "rtsp://camera2.example/video"]):
            args = parse_args()
        self.assertEqual(len(args.input), 2)
        self.assertEqual((args.mux_live_source, args.rtsp_latency, args.jpeg_quality), (1, 1000, 80))
        self.assertFalse(args.rtsp_drop_on_latency)
        self.assertTrue(args.diagnostics)
        self.assertFalse(hasattr(args, "output"))

    def test_only_client_disconnects_are_suppressed(self):
        handler = PreviewHandler.__new__(PreviewHandler)
        for method in ("handle", "finish"):
            for error in (BrokenPipeError(), ConnectionResetError()):
                with patch.object(SimpleHTTPRequestHandler, method, side_effect=error):
                    getattr(handler, method)()
                self.assertTrue(handler.close_connection)
            with patch.object(SimpleHTTPRequestHandler, method, side_effect=ValueError("real error")):
                with self.assertRaises(ValueError):
                    getattr(handler, method)()

    def test_http_mjpeg_assets_and_visible_404(self):
        cache = Path(__file__).resolve().parents[1] / ".cache"
        cache.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=cache) as directory:
            root = Path(directory)
            (root / "index.html").write_text("preview", encoding="utf-8")
            (root / "private.txt").write_text("not a public asset", encoding="utf-8")
            store = FrameStore()
            jpeg = b"\xff\xd8test\xff\xd9"
            store.put(jpeg)
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0), functools.partial(PreviewHandler, directory=directory),
            )
            server.mjpeg_streams = {"/mjpeg/source0": store}
            server.streams = [{"id": 0, "format": "mjpeg", "url": "/mjpeg/source0"}]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                with urlopen(base + "/streams.json", timeout=2) as response:
                    self.assertEqual(json.load(response), server.streams)
                with urlopen(base + "/", timeout=2) as response:
                    self.assertEqual(response.read(), b"preview")
                with urlopen(base + "/mjpeg/source0?v=1", timeout=2) as response:
                    self.assertIn("boundary=frame", response.headers["Content-Type"])
                    self.assertEqual(response.readline(), b"--frame\r\n")
                    headers = {}
                    while True:
                        line = response.readline()
                        if line == b"\r\n":
                            break
                        key, value = line.decode().split(":", 1)
                        headers[key] = value.strip()
                    self.assertEqual(response.read(int(headers["Content-Length"])), jpeg)
                errors = io.StringIO()
                with patch("sys.stderr", errors):
                    with self.assertRaises(HTTPError) as caught:
                        urlopen(base + "/private.txt", timeout=2)
                    self.assertEqual(caught.exception.code, 404)
                    caught.exception.close()
                self.assertIn("HTTP error status=404", errors.getvalue())
            finally:
                store.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    (Path(__file__).resolve().parents[1] / ".cache").mkdir(exist_ok=True)
    unittest.main()
