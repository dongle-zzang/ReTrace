import copy
import threading
import unittest
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import urlopen

from parking_relay import ParkingRelay
from preview_mjpeg import FrameStore
from test_preview_http import PreviewHandler


class ColorTransportTests(unittest.TestCase):
    def test_snapshot_reuses_store_and_rejects_unavailable_frame(self):
        store = FrameStore()
        server = ThreadingHTTPServer(('127.0.0.1', 0), PreviewHandler)
        server.mjpeg_streams = {'/mjpeg/source0': store}
        stream = {'camera_id': 'first', 'url': '/mjpeg/source0', 'runtime_session': 's',
                  'runtime': {'state': 'online', 'generation': 1}}
        server.stream_snapshot = lambda: [stream]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{server.server_port}/snapshots/first.jpg'
        try:
            with self.assertRaises(HTTPError) as error:
                urlopen(url)
            self.assertEqual(error.exception.code, 503)
            store.put(b'cached-jpeg')
            with urlopen(url) as response:
                self.assertEqual(response.read(), b'cached-jpeg')
                self.assertEqual(response.headers['X-Frame-Identity'], 's:1:1')
                self.assertGreaterEqual(float(response.headers['X-Frame-Age']), 0)
            store.clear()
            with self.assertRaises(HTTPError):
                urlopen(url)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_relay_dedup_conflict_outage_and_deleted_zone(self):
        messages = []
        hub = SimpleNamespace(stream_snapshot=lambda: [{'camera_id': 'a'}, {'camera_id': 'b'}],
                              publish_messages=lambda items: messages.extend(copy.deepcopy(items)))
        relay = ParkingRelay('http://backend.example', hub, threading.Event())
        spaces = [{'parkingSpaceId': 'p', 'label': 'P', 'status': 'OCCUPIED', 'conflict': True,
                   'zones': [{'zoneId': 'a1', 'cameraId': 'a', 'enabled': True, 'status': 'EMPTY', 'score': 0},
                             {'zoneId': 'b1', 'cameraId': 'b', 'enabled': True, 'status': 'OCCUPIED', 'score': 1}]}]
        relay.publish_color(spaces)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0][1], 'parking.status_updated')
        spaces[0]['zones'][0]['score'] = .01
        relay.publish_color(spaces)
        self.assertEqual(len(messages), 2)
        relay.color_unavailable()
        self.assertEqual(messages[-1][2]['spaces'][0]['status'], 'UNKNOWN')
        self.assertFalse(messages[-1][2]['spaces'][0]['conflict'])
        relay.publish_color([])
        self.assertEqual(messages[-1][2], {'spaces': []})
