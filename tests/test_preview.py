"""Run in retrace: python3 -m unittest discover -s tests -p 'test_*.py'."""

import os
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

PROJECT_CACHE = Path(__file__).resolve().parents[1] / ".cache"
PROJECT_CACHE.mkdir(exist_ok=True)
os.environ["GST_REGISTRY"] = str(PROJECT_CACHE / "gstreamer-registry.bin")

from preview import Gst, build_output, parse_args
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
            self.assertFalse(sink.get_property("sync"))
            self.assertFalse(sink.get_property("async"))
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
        self.assertTrue(args.rtsp_drop_on_latency)
        self.assertTrue(args.diagnostics)
        self.assertFalse(hasattr(args, "output"))


if __name__ == "__main__":
    (Path(__file__).resolve().parents[1] / ".cache").mkdir(exist_ok=True)
    unittest.main()
