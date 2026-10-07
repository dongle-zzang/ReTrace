"""CPU-only camera health, backoff, isolation and lifecycle tests."""
import ast
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace as NS
import unittest

from camera_config import Camera, CameraConfigError, resolve_camera_rtsp
from dataclasses import replace
from camera_runtime import CameraRuntime
from person_metadata import MetadataStore
from preview_mjpeg import FrameStore, serve_mjpeg

ROOT = Path(__file__).resolve().parents[1]


def load_preview(names, namespace):
    namespace.update(ROOT=ROOT, Path=Path, replace=replace,
                     resolve_camera_rtsp=resolve_camera_rtsp, CameraConfigError=CameraConfigError)
    tree = ast.parse((ROOT / 'preview.py').read_text())
    tree.body = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
    exec(compile(tree, 'preview.py', 'exec'), namespace)
    return namespace


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.runtime = CameraRuntime('first', clock=lambda: self.now,
                                     wall_clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))

    def frames(self, runtime=None, generation=None, seconds=3):
        runtime = runtime or self.runtime
        generation = generation or runtime.snapshot().generation
        for _ in range(seconds * 10):
            self.now += .1
            runtime.observe_frame(generation)
            runtime.observe_output(generation)

    def test_initial_and_continuous_frame_recovery(self):
        self.assertEqual(self.runtime.snapshot().state, 'connecting')
        generation = self.runtime.begin_attempt()
        self.runtime.observe_frame(generation)
        self.assertEqual(self.runtime.snapshot().state, 'connecting')
        self.frames()
        self.assertEqual(self.runtime.snapshot().state, 'online')
        self.assertGreater(self.runtime.snapshot().fps, 9)
        self.assertTrue(self.runtime.snapshot().last_frame_at.endswith('+00:00'))

    def test_frame_gap_degraded_then_offline(self):
        self.runtime.begin_attempt()
        self.frames()
        self.now += 3.1
        self.assertIsNone(self.runtime.check_health())
        self.assertEqual(self.runtime.snapshot().state, 'degraded')
        self.now += 7
        self.assertEqual(self.runtime.check_health(), 'no_frames')
        status = self.runtime.snapshot()
        self.assertEqual(status.state, 'offline')
        self.assertEqual(status.fps, 0)
        self.assertTrue(self.runtime.attempt_stop.is_set())
        self.assertGreater(status.last_frame_age, 10)

    def test_model_initialization_does_not_consume_rtsp_timeout(self):
        generation = self.runtime.begin_attempt(watchdog=False)
        self.now = 300
        self.assertIsNone(self.runtime.check_health())
        self.runtime.arm_watchdog(generation)
        self.now = 329
        self.assertIsNone(self.runtime.check_health())
        self.now = 330
        self.assertEqual(self.runtime.check_health(), 'no_frames')

    def test_connect_timeout_and_low_fps(self):
        self.runtime.begin_attempt()
        self.now = 29
        self.assertIsNone(self.runtime.check_health())
        self.now = 30
        self.assertEqual(self.runtime.check_health(), 'no_frames')
        self.runtime.begin_attempt()
        for _ in range(4):
            self.now += 2
            self.runtime.observe_frame(self.runtime.snapshot().generation)
            self.runtime.observe_output(self.runtime.snapshot().generation)
        self.assertEqual(self.runtime.snapshot().state, 'degraded')

    def test_output_stall_even_when_source_frames_continue(self):
        generation = self.runtime.begin_attempt()
        for _ in range(110):
            self.now += .1
            self.runtime.observe_frame(generation)
        self.assertEqual(self.runtime.check_health(), 'output_stalled')
        self.assertEqual(self.runtime.snapshot().state, 'offline')

    def test_backoff_cap_counts_stale_frames_and_reconnect(self):
        first = self.runtime.begin_attempt()
        for count, expected in enumerate((1, 2, 5, 10, 30, 30)):
            generation = self.runtime.snapshot().generation
            self.runtime.fail('rtsp_timeout', generation)
            self.assertEqual(self.runtime.retry_delay(), expected)
            self.assertEqual(self.runtime.snapshot().last_error, 'rtsp_timeout')
            self.runtime.observe_frame(generation)
            self.assertEqual(self.runtime.snapshot().state, 'offline')
            self.now += expected
            self.runtime.begin_attempt()
            self.assertEqual(self.runtime.snapshot().state, 'reconnecting')
            self.assertEqual(self.runtime.snapshot().reconnect_count, count + 1)
            self.assertFalse(self.runtime.attempt_stop.is_set())
        self.runtime.observe_frame(first)
        self.assertIsNone(self.runtime.snapshot().last_frame_at)
        self.frames()
        self.assertEqual(self.runtime.snapshot().state, 'online')
        self.assertEqual(self.runtime.snapshot().last_error, 'rtsp_timeout')  # historical fault

    def test_backoff_reset_only_after_stable_recovery(self):
        generation = self.runtime.begin_attempt()
        self.runtime.fail('rtsp_error', generation)
        self.now += 1
        generation = self.runtime.begin_attempt()
        self.frames(seconds=31)
        self.runtime.fail('rtsp_error', generation)
        self.assertEqual(self.runtime.retry_delay(), 1)

    def test_safe_errors_serialization_and_camera_isolation(self):
        second = CameraRuntime('second', clock=lambda: self.now)
        first_gen = self.runtime.begin_attempt()
        second_gen = second.begin_attempt()
        self.frames(second, second_gen)
        before = second.snapshot()
        self.runtime.fail('rtsp://private-secret@camera.example/video', first_gen)
        serialized = json.dumps(self.runtime.snapshot().to_dict(), allow_nan=False)
        self.assertNotIn('private-secret', serialized)
        self.assertEqual(self.runtime.snapshot().last_error, 'pipeline_error')
        self.assertEqual(second.snapshot(), before)
        self.assertFalse(second.attempt_stop.is_set())

    def test_stale_attempt_cannot_publish_to_shared_stores(self):
        first = self.runtime.begin_attempt()
        writes = []
        self.assertTrue(self.runtime.publish(first, lambda: writes.append('first'), output=True))
        self.runtime.fail('rtsp_error', first)
        self.assertFalse(self.runtime.publish(first, lambda: writes.append('offline')))
        second = self.runtime.begin_attempt()
        self.assertFalse(self.runtime.publish(first, lambda: writes.append('stale')))
        self.assertTrue(self.runtime.publish(second, lambda: writes.append('second'), output=True))
        self.assertEqual(writes, ['first', 'second'])

    def test_invalid_timeout_rejected(self):
        for kwargs in ({'degraded_after': 10, 'offline_after': 3}, {'min_fps': 0}, {'connect_timeout': float('nan')}):
            with self.assertRaises(ValueError):
                CameraRuntime('first', **kwargs)


class StoreTests(unittest.TestCase):
    def test_mjpeg_clear_advances_sequence_and_resumes_same_connection(self):
        import io
        calls = []
        replies = iter([(1, b'first', False), (2, None, False), (3, b'recovered', False), (3, None, True)])
        def wait_next(sequence):
            calls.append(sequence)
            return next(replies)
        handler = NS(send_response=lambda *args: None, send_header=lambda *args: None,
                     end_headers=lambda: None, connection=NS(settimeout=lambda value: None),
                     wfile=io.BytesIO())
        serve_mjpeg(handler, NS(wait_next=wait_next))
        self.assertEqual(calls, [0, 1, 2, 3])
        body = handler.wfile.getvalue()
        self.assertIn(b'first', body)
        self.assertIn(b'recovered', body)
        self.assertEqual(body.count(b'--frame'), 2)

    def test_jpeg_clear_waiters_and_metadata_clear_are_source_local(self):
        first, second = FrameStore(), FrameStore()
        first.put(b'first')
        second.put(b'second')
        first.clear()
        self.assertEqual(first.wait_next(1, timeout=0), (2, None, False))
        self.assertEqual(second.wait_next(0, timeout=0), (1, b'second', False))
        metadata = MetadataStore()
        metadata.put(NS(source_id=0))
        metadata.put(NS(source_id=1))
        metadata.clear(0)
        self.assertEqual(set(metadata.snapshot()), {1})


if __name__ == '__main__':
    unittest.main()
