"""On-demand gate and stale handoff lifecycle without GPU imports."""
import io
import threading
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock
from test_camera_runtime import load_preview
from preview_capture import RawCapture, CaptureBusy, CaptureUnavailable, add_capture_branch


class CaptureTests(unittest.TestCase):
    def test_disabled_and_timeout_close_gate(self):
        capture = RawCapture()
        with self.assertRaises(CaptureUnavailable):
            capture.request(.01)
        values = []
        capture.bind(values.append)
        with self.assertRaises(CaptureUnavailable):
            capture.request(.01)
        self.assertEqual(values, [True, False])
        self.assertFalse(capture.admit(1))

    def test_one_request_one_frame_and_busy(self):
        capture = RawCapture()
        gate = threading.Event()
        capture.bind(lambda enabled: gate.set() if enabled else gate.clear())
        result = []
        thread = threading.Thread(target=lambda: result.append(capture.request(1)))
        thread.start()
        self.assertTrue(gate.wait(1))
        with self.assertRaises(CaptureBusy):
            capture.request(.01)
        self.assertTrue(capture.admit(100))
        self.assertFalse(gate.is_set())
        self.assertFalse(capture.admit(101))
        capture.deliver(b'wrong-frame', 101)
        self.assertFalse(result)
        capture.deliver(b'clean-jpeg', 100)
        thread.join(1)
        self.assertEqual(result[0][0], b'clean-jpeg')
        self.assertFalse(capture.pending)


    def test_opt_in_branch_precedes_osd_conversion_and_is_bounded(self):
        elements, links = {}, []
        class Element:
            def __init__(self, factory, name):
                self.name, self.factory, self.props, self.callbacks, self.probes = name, factory, {}, {}, []
                elements[name] = self
            def set_property(self, key, value): self.props[key] = value
            def find_property(self, key): return key
            def connect(self, name, callback): self.callbacks[name] = callback
            def add(self, element): pass
            def add_pad(self, pad): return True
            def get_static_pad(self, name): return NS(add_probe=lambda kind, callback: self.probes.append(callback))
            def link(self, element): links.append((self.name, element.name)); return True
            def set_state(self, state): pass
        gst = NS(Bin=NS(new=lambda name: Element('bin', name)), Caps=NS(from_string=lambda text: text),
                 GhostPad=NS(new=lambda *args: 'pad'), State=NS(NULL='NULL'),
                 FlowReturn=NS(OK='OK', EOS='EOS'), MapFlags=NS(READ=1),
                 PadProbeReturn=NS(OK='OK', DROP='DROP'), PadProbeType=NS(BUFFER=1))
        namespace = load_preview({'build_output'}, {'Gst': gst, 'make_element': Element, 'add_capture_branch': add_capture_branch})
        capture = RawCapture()
        namespace['build_output'](0, NS(jpeg_quality=80, raw_captures={0: capture}), NS(put=lambda data: None), lambda reason: None)
        self.assertIn(('queue-0', 'capture-tee-0'), links)
        self.assertIn(('capture-tee-0', 'capture-valve-0'), links)
        self.assertIn(('capture-tee-0', 'capture-preview-queue-0'), links)
        self.assertIn(('capture-preview-queue-0', 'osd-convert-0'), links)
        self.assertTrue(elements['capture-valve-0'].props['drop'])
        self.assertEqual(elements['capture-valve-0'].props['drop-mode'], 1)
        for name in ('capture-queue-0', 'capture-preview-queue-0'):
            self.assertEqual(elements[name].props['max-size-buffers'], 1)
            self.assertEqual(elements[name].props['leaky'], 2)
        self.assertNotIn('width=', elements['capture-caps-0'].props['caps'])
        self.assertFalse(elements['capture-sink-0'].props['async'])
        self.assertFalse(elements['capture-sink-0'].props['sync'])
        # Only the demand path passes one frame; handoff exceptions keep Gst flow healthy.
        probe = elements['capture-valve-0'].probes[0]
        info = NS(get_buffer=lambda: NS(pts=42))
        self.assertEqual(probe(None, info), 'DROP')
        capture.pending = True
        self.assertEqual(probe(None, info), 'OK')
        self.assertEqual(probe(None, info), 'DROP')
        callback = elements['capture-sink-0'].callbacks['new-sample']
        self.assertEqual(callback(NS(emit=Mock(side_effect=RuntimeError('fixture')))), 'OK')


    def test_gate_failure_does_not_leave_capture_permanently_busy(self):
        capture = RawCapture()
        capture.bind(Mock(side_effect=RuntimeError('fixture')))
        with self.assertRaises(CaptureUnavailable):
            capture.request(.01)
        self.assertFalse(capture.pending)
        self.assertFalse(capture.request_lock.locked())

    def test_http_capture_rejects_stale_generation_and_head(self):
        from http.server import BaseHTTPRequestHandler
        from urllib.parse import urlsplit
        handler_class = load_preview({'PreviewHandler'}, {'BaseHTTPRequestHandler': BaseHTTPRequestHandler,
            'urlsplit': urlsplit, 'CaptureBusy': CaptureBusy, 'CaptureUnavailable': CaptureUnavailable})['PreviewHandler']
        state = {'camera_id': 'first', 'runtime_session': 'session', 'runtime': {'state': 'online', 'generation': 1}}
        capture = Mock()
        capture.request.return_value = (b'clean-jpeg', '2026-10-12T00:00:00+00:00')
        handler = handler_class.__new__(handler_class)
        handler.path, handler.command, handler.wfile = '/training-snapshots/first.jpg', 'GET', io.BytesIO()
        handler.server = NS(stream_snapshot=lambda: [state], raw_captures={'first': capture})
        handler.send_response, handler.send_header, handler.end_headers, handler.send_error = Mock(), Mock(), Mock(), Mock()
        handler.do_GET()
        handler.send_response.assert_called_once_with(200)
        handler.send_header.assert_any_call('X-Capture-Clean', 'pre-osd-v1')
        self.assertEqual(handler.wfile.getvalue(), b'clean-jpeg')
        handler.command = 'HEAD'
        handler.do_GET()
        handler.send_error.assert_called_with(503)
        capture.request.assert_called_once()
        handler.command = 'GET'
        def stale():
            state['runtime']['generation'] += 1
            return b'wrong-generation', '2026-10-12T00:00:00+00:00'
        capture.request.side_effect = stale
        handler.do_GET()
        handler.send_error.assert_called_with(503)
        self.assertEqual(handler.wfile.getvalue(), b'clean-jpeg')
        capture.request.side_effect = CaptureBusy()
        handler.do_GET()
        handler.send_error.assert_called_with(409)
