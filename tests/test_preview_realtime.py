"""CPU contracts and real HTTP-upgraded WebSocket lifecycle, without GI/PyDS."""
import ast
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import threading
import time
from types import SimpleNamespace
import unittest
from queue import Queue
from unittest.mock import patch
from urllib.parse import urlsplit

from wsproto import ConnectionType, WSConnection
from wsproto.events import Request, AcceptConnection, TextMessage, BytesMessage, CloseConnection, Ping, Pong

from person_metadata import BoundingBox, FrameMetadata, PersonMetadata
from preview_mjpeg import FrameStore
from preview_socket import RealtimeHub, detection_message, video_frame


ROOT = Path(__file__).resolve().parents[1]


def frame(source=0, camera="first", number=1):
    person = PersonMetadata(camera, "2026-01-01T00:00:00Z", (1 << 63) + 3, 0, 0.9,
                            BoundingBox(-192, 108, 576, 1080), None)
    return FrameMetadata(source, camera, number, person.timestamp, 1234, True, (person,)).to_dict()


def parse_video_frame(data):
    length = data[1]
    return data[0], data[2:2 + length].decode(), int.from_bytes(data[2 + length:6 + length], "big"), data[6 + length:]


class MessageTests(unittest.TestCase):
    def test_parking_batch_updates_reconnect_cache_before_any_event_and_is_atomic(self):
        streams = [{"camera_id": "first", "id": 0, "runtime_session": "session",
                    "runtime": {"generation": 1}}]
        hub = RealtimeHub(lambda: {}, lambda: streams, {}, threading.Event())
        hub.publish_message('first', 'parking_status', {'spaces': [{'occupancy': 'occupied'}]})
        observed = []
        class InspectQueue(Queue):
            def put_nowait(self, message):
                observed.append(hub.parking_current['first']['data']['spaces'][0]['occupancy'])
                super().put_nowait(message)
        queue = InspectQueue()
        hub.outboxes[1] = (queue, threading.Event(), [None])
        batch = [('first', 'event', {'kind': 'parking_occupancy_changed', 'occupancy': 'empty'}),
                 ('first', 'parking_status', {'spaces': [{'occupancy': 'empty'}]})]
        hub.publish_messages(batch)
        self.assertEqual(observed, ['empty', 'empty'])
        self.assertEqual([queue.get_nowait()['type'] for _ in range(2)], ['event', 'parking_status'])
        batch[1][2]['spaces'][0]['occupancy'] = 'occupied'
        self.assertEqual(hub.parking_current['first']['data']['spaces'][0]['occupancy'], 'empty')
        with self.assertRaises(ValueError):
            hub.publish_messages(batch + [('first', 'event', {'oversized': 'x' * 65536})])
        self.assertTrue(queue.empty())
        self.assertEqual(hub.parking_current['first']['data']['spaces'][0]['occupancy'], 'empty')

    def test_normalized_clipped_coordinates_and_lossless_identifiers(self):
        message = detection_message(frame(), "session")
        self.assertEqual(message["persons"][0]["bbox"], {"x": 0, "y": 0.1, "width": 0.2, "height": 0.9})
        self.assertEqual(message["persons"][0]["trackId"], str((1 << 63) + 3))
        self.assertEqual(message["ptsNs"], "1234")
        self.assertEqual((message["cameraId"], message["sourceId"]), ("first", 0))

    def test_binary_video_frame_layout(self):
        self.assertEqual(parse_video_frame(video_frame("카메라-1", (1 << 32) + 7, b"\xff\xd8jpeg")),
                         (1, "카메라-1", 7, b"\xff\xd8jpeg"))

    def test_frame_store_latest_is_non_blocking_and_reports_clear(self):
        store = FrameStore()
        self.assertEqual(store.latest(), (0, None))
        store.put(b"jpeg")
        self.assertEqual(store.latest(), (1, b"jpeg"))
        store.clear()
        self.assertEqual(store.latest(), (2, None))


class SocketTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((ROOT / "preview.py").read_text())
        handler_node = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "PreviewHandler")
        namespace = {"BaseHTTPRequestHandler": BaseHTTPRequestHandler, "urlsplit": urlsplit}
        exec(compile(ast.Module(body=[handler_node], type_ignores=[]), "preview.py", "exec"), namespace)
        self.frames = [frame(), frame(3, "second")]
        self.streams = [{"camera_id": name, "id": source, "format": "mjpeg", "runtime_session": "session",
                         "runtime": {"state": "online", "generation": 0}} for source, name in ((0, "first"), (3, "second"))]
        self.shutdown = threading.Event()
        self.stores = {0: FrameStore(), 3: FrameStore()}
        self.hub = RealtimeHub(lambda: {"runtime_session": "session", "frames": self.frames},
                               lambda: self.streams, self.stores, self.shutdown)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), namespace["PreviewHandler"])
        self.server.daemon_threads = True
        self.server.realtime_hub = self.hub
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.sock = socket.create_connection(self.server.server_address, timeout=2)
        self.ws = WSConnection(ConnectionType.CLIENT)
        host = f"127.0.0.1:{self.server.server_port}"
        self.sock.sendall(self.ws.send(Request(host=host, target="/ws", extra_headers=[(b"Origin", f"http://{host}".encode())])))
        self.messages = []
        self.videos = []
        self.until(lambda: any(item.get("type") == "hello" for item in self.messages))

    def tearDown(self):
        self.sock.close()
        self.shutdown.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def until(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("WebSocket response timeout")
            self.ws.receive_data(self.sock.recv(65536))
            for event in self.ws.events():
                if isinstance(event, TextMessage) and event.message_finished:
                    self.messages.append(json.loads(event.data))
                elif isinstance(event, BytesMessage) and event.message_finished:
                    self.videos.append(parse_video_frame(bytes(event.data)))
                elif isinstance(event, Ping):
                    self.sock.sendall(self.ws.send(event.response()))

    def send(self, **message):
        self.sock.sendall(self.ws.send(TextMessage(data=json.dumps({"version": 1, **message}))))

    def test_one_socket_carries_metadata_and_latest_jpeg_for_each_subscribed_camera(self):
        self.until(lambda: len([m for m in self.messages if m["type"] == "detections"]) >= 2)
        detections = [m for m in self.messages if m["type"] == "detections"]
        self.assertEqual({(m["cameraId"], m["sourceId"]) for m in detections}, {("first", 0), ("second", 3)})
        self.stores[0].put(b"first-jpeg")
        self.stores[3].put(b"second-jpeg")
        time.sleep(0.1)
        self.assertEqual(self.videos, [])  # Metadata-only until video is requested.
        self.send(type="subscribe", cameraIds=["first", "second"], video=True, videoFps=30)
        self.until(lambda: {v[1] for v in self.videos} == {"first", "second"})
        self.assertIn((1, "first", 1, b"first-jpeg"), self.videos)
        self.assertIn((1, "second", 1, b"second-jpeg"), self.videos)
        subscribed = next(m for m in self.messages if m["type"] == "subscribed")
        self.assertEqual((subscribed["video"], subscribed["videoFps"]), (True, 30))
        # Unchanged JPEGs are not resent; a new one is, and cleared cameras send nothing.
        time.sleep(0.15)
        self.assertEqual(len(self.videos), 2)
        self.stores[3].put(b"second-new")
        self.stores[0].clear()
        self.until(lambda: (1, "second", 2, b"second-new") in self.videos)
        time.sleep(0.1)
        self.assertEqual([v for v in self.videos if v[1] == "first"], [(1, "first", 1, b"first-jpeg")])
        self.videos.clear()
        self.send(type="subscribe", cameraIds=["first"], video=False)
        self.until(lambda: sum(m["type"] == "subscribed" for m in self.messages) == 2)
        self.stores[0].put(b"ignored")
        time.sleep(0.1)
        self.assertEqual(self.videos, [])

    def test_rate_limit_invalid_requests_and_removed_signaling(self):
        self.send(type="subscribe", cameraIds=["first"], video=True, videoFps=2)
        self.until(lambda: any(m["type"] == "subscribed" for m in self.messages))
        for number in range(10):
            self.stores[0].put(b"frame-%d" % number)
            time.sleep(0.04)
        self.until(lambda: len(self.videos) >= 1)
        time.sleep(0.1)
        self.assertLessEqual(len(self.videos), 2)  # About 0.5 s at 2 fps, latest frame wins.
        for message in ({"type": "subscribe", "cameraIds": ["first"], "video": True, "videoFps": 0},
                        {"type": "subscribe", "cameraIds": ["first"], "video": "yes"},
                        {"type": "subscribe", "cameraIds": ["missing"]},
                        {"type": "watch", "cameraId": "first", "peerId": "old-client"}):
            self.messages.clear()
            self.send(**message)
            self.until(lambda: any(m["type"] == "error" for m in self.messages))
            self.assertEqual(next(m for m in self.messages if m["type"] == "error")["code"], "command_failed")

    def test_subscription_and_explicit_stale_overlay_clear(self):
        self.send(type="subscribe", cameraIds=["second"])
        self.until(lambda: any(m["type"] == "subscribed" for m in self.messages))
        self.messages.clear()
        self.frames = [frame(0, "first", 2), frame(3, "second", 2)]
        self.until(lambda: any(m["type"] == "detections" for m in self.messages))
        self.assertEqual({m["cameraId"] for m in self.messages if m["type"] == "detections"}, {"second"})
        self.frames = []
        self.until(lambda: any(m.get("stale") for m in self.messages))
        stale = next(m for m in self.messages if m.get("stale"))
        self.assertEqual((stale["cameraId"], stale["persons"]), ("second", []))

    def test_generic_event_is_detached_and_respects_camera_subscription(self):
        self.send(type="subscribe", cameraIds=["second"])
        self.until(lambda: any(m["type"] == "subscribed" for m in self.messages))
        data = {"kind": "zone", "eventId": "test"}
        self.hub.publish_message("first", "event", data)
        self.hub.publish_message("second", "event", data)
        data["kind"] = "modified"
        self.until(lambda: any(m["type"] == "event" for m in self.messages))
        event = next(m for m in self.messages if m["type"] == "event")
        self.assertEqual((event["cameraId"], event["data"]["kind"]), ("second", "zone"))
        self.assertEqual(event["generation"], 0)
        self.assertIn("timestamp", event)

    def test_parking_current_replayed_after_subscription_and_event_distinct(self):
        self.send(type="subscribe", cameraIds=["second"])
        self.until(lambda: any(m["type"] == "subscribed" for m in self.messages))
        self.hub.publish_message("first", "parking_status", {"spaces": [{"occupancy": "occupied"}]})
        self.messages.clear()
        self.send(type="subscribe", cameraIds=["first"])
        self.until(lambda: any(m["type"] == "parking_status" for m in self.messages))
        current = next(m for m in self.messages if m["type"] == "parking_status")
        self.assertEqual(current["data"]["spaces"][0]["occupancy"], "occupied")
        self.hub.publish_message("first", "event", {"kind": "parking_occupancy_changed", "event_id": "4"})
        self.until(lambda: any(m["type"] == "event" for m in self.messages))
        self.assertEqual(next(m for m in self.messages if m["type"] == "event")["data"]["event_id"], "4")

    def test_color_parking_replayed_with_existing_envelope(self):
        self.send(type='subscribe', cameraIds=['second'])
        self.until(lambda: any(m['type'] == 'subscribed' for m in self.messages))
        self.hub.publish_message('first', 'parking.status_updated', {'spaces': [
            {'parkingSpaceId': 'p', 'status': 'OCCUPIED', 'conflict': True, 'zones': []}]})
        self.messages.clear()
        self.send(type='subscribe', cameraIds=['first'], video=True)
        self.stores[0].put(b'color-jpeg')
        self.until(lambda: any(m['type'] == 'parking.status_updated' for m in self.messages) and bool(self.videos))
        event = next(m for m in self.messages if m['type'] == 'parking.status_updated')
        self.assertEqual((event['version'], event['cameraId'], event['sourceId']), (1, 'first', 0))
        self.assertEqual(event['data']['spaces'][0]['status'], 'OCCUPIED')
        self.assertIn('runtimeSession', event)
        self.assertIn('timestamp', event)

    def test_parking_batch_order_and_reconnect_with_jpeg_and_people(self):
        self.send(type='subscribe', cameraIds=['first'], video=True)
        self.until(lambda: any(m['type'] == 'subscribed' for m in self.messages))
        self.hub.publish_messages([
            ('first', 'event', {'kind': 'parking_occupancy_changed', 'event_id': '5', 'occupancy': 'empty'}),
            ('first', 'parking_status', {'spaces': [{'occupancy': 'empty'}]})])
        self.stores[0].put(b'parking-test-jpeg')
        self.frames = [frame(number=2)]
        self.until(lambda: any(m['type'] == 'parking_status' for m in self.messages)
                   and any(m['type'] == 'detections' and m['frameNumber'] == 2 for m in self.messages)
                   and bool(self.videos))
        parking = [m for m in self.messages if m['type'] in ('event', 'parking_status')]
        self.assertEqual([m['type'] for m in parking], ['event', 'parking_status'])
        self.assertEqual(parking[-1]['data']['spaces'][0]['occupancy'], 'empty')
        self.assertEqual(self.videos[-1][-1], b'parking-test-jpeg')
        self.assertTrue(next(m for m in self.messages if m['type'] == 'detections')['persons'])

        # A new connection receives the final cache, never the pre-event state.
        self.sock.close()
        self.sock = socket.create_connection(self.server.server_address, timeout=2)
        self.ws = WSConnection(ConnectionType.CLIENT)
        host = f'127.0.0.1:{self.server.server_port}'
        self.sock.sendall(self.ws.send(Request(host=host, target='/ws')))
        self.messages = []
        self.until(lambda: any(m['type'] == 'parking_status' for m in self.messages))
        self.assertEqual(next(m for m in self.messages if m['type'] == 'parking_status')
                         ['data']['spaces'][0]['occupancy'], 'empty')
        self.messages = []
        self.send(type='subscribe', cameraIds=['first'])
        self.until(lambda: any(m['type'] == 'parking_status' for m in self.messages))
        self.assertEqual(next(m for m in self.messages if m['type'] == 'parking_status')
                         ['data']['spaces'][0]['occupancy'], 'empty')


if __name__ == "__main__":
    unittest.main()
