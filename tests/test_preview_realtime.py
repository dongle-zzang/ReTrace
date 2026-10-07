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
from unittest.mock import patch
from urllib.parse import urlsplit

from wsproto import ConnectionType, WSConnection
from wsproto.events import Request, AcceptConnection, TextMessage, CloseConnection, Ping, Pong

from person_metadata import BoundingBox, FrameMetadata, PersonMetadata
from preview_socket import RealtimeHub, detection_message
from preview_webrtc import EncodedStore, PeerRegistry, WebRTCPeer, AccessUnit, build_h264_output


ROOT = Path(__file__).resolve().parents[1]


def frame(source=0, camera="first", number=1):
    person = PersonMetadata(camera, "2026-01-01T00:00:00Z", (1 << 63) + 3, 0, 0.9,
                            BoundingBox(-192, 108, 576, 1080), None)
    return FrameMetadata(source, camera, number, person.timestamp, 1234, True, (person,)).to_dict()


class StorageTests(unittest.TestCase):
    def test_gpu_branch_preserves_nvmm_and_copies_only_encoded_access_units(self):
        elements = {}
        class Element:
            def __init__(self, factory, name):
                self.factory, self.name, self.props, self.callbacks = factory, name, {}, {}
                elements[name] = self
            def set_property(self, name, value):
                self.props[name] = value
            def find_property(self, name):
                return None
            def get_static_pad(self, name):
                return (self.name, name)
            def link(self, other):
                return True
            def connect(self, name, callback):
                self.callbacks[name] = callback
        output = SimpleNamespace(add=lambda element: None, add_pad=lambda pad: True,
                                 set_state=lambda state: None)
        gst = SimpleNamespace(Bin=SimpleNamespace(new=lambda name: output),
            Caps=SimpleNamespace(from_string=lambda value: value),
            GhostPad=SimpleNamespace(new=lambda name, pad: pad), MapFlags=SimpleNamespace(READ=1),
            BufferFlags=SimpleNamespace(DELTA_UNIT=1), FlowReturn=SimpleNamespace(OK="ok", EOS="eos"))
        received = []
        store = SimpleNamespace(put_access_unit=lambda *args: received.append(args))
        failures = []
        build_h264_output(0, SimpleNamespace(video_width=1280, video_height=720, video_bitrate=2000000, video_gop=30),
                          store, failures.append, gst, Element)
        self.assertEqual([item.factory for item in elements.values()],
                         ["queue", "nvvideoconvert", "capsfilter", "nvv4l2h264enc", "h264parse", "capsfilter", "appsink"])
        self.assertIn("memory:NVMM", elements["video-caps-0"].props["caps"])
        self.assertEqual(elements["video-encoder-0"].props["profile"], 0)
        self.assertEqual(elements["video-encoder-0"].props["idrinterval"], 30)
        self.assertFalse(elements["video-sink-0"].props["drop"])
        buffer = SimpleNamespace(pts=123, has_flags=lambda flags: False,
                                 map=lambda flags: (True, SimpleNamespace(data=b"h264")), unmap=lambda info: None)
        appsink = SimpleNamespace(emit=lambda signal: SimpleNamespace(get_buffer=lambda: buffer))
        self.assertEqual(elements["video-sink-0"].callbacks["new-sample"](appsink), "ok")
        self.assertEqual(received, [(b"h264", 123, True)])
        self.assertEqual(failures, [])

    def test_normalized_clipped_coordinates_and_lossless_identifiers(self):
        message = detection_message(frame(), "session")
        self.assertEqual(message["persons"][0]["bbox"], {"x": 0, "y": 0.1, "width": 0.2, "height": 0.9})
        self.assertEqual(message["persons"][0]["trackId"], str((1 << 63) + 3))
        self.assertEqual(message["ptsNs"], "1234")
        self.assertEqual((message["cameraId"], message["sourceId"]), ("first", 0))

    def test_storage_is_bounded_expiring_and_discontinuous_after_clear(self):
        store = EncodedStore()
        with patch("preview_webrtc.time.monotonic", return_value=1):
            for number in range(20):
                store.put(b"compressed", number % 5 == 0)
            self.assertEqual(len(store.read_after(0)), 8)
            last = store.read_after(0)[-1].sequence
            store.clear()
            store.put(b"new", True)
            self.assertEqual(store.read_after(last)[0].sequence, last + 2)
        with patch("preview_webrtc.time.monotonic", return_value=2):
            self.assertEqual(store.read_after(0), [])
        store.close()
        store.put(b"ignored", True)
        self.assertEqual(store.read_after(0), [])

    def test_peer_recovers_only_at_keyframe_after_gap_and_backpressure(self):
        peer = WebRTCPeer.__new__(WebRTCPeer)
        peer.closed = threading.Event()
        peer.started = time.monotonic()
        peer.sequence, peer.wait_keyframe, peer.dropped = 0, True, 0
        peer.remote_set = threading.Event()
        peer.remote_set.set()
        peer.pending_ice = []
        peer.ice_lock = threading.Lock()
        peer.offered = True
        peer.buffer_limit = False
        peer.bus = SimpleNamespace(pop_filtered=lambda _types: None)
        peer.webrtc = SimpleNamespace(get_property=lambda _key: "connected")
        peer.WebRTC = SimpleNamespace(WebRTCPeerConnectionState=SimpleNamespace(FAILED="failed", CONNECTED="connected"))
        buffers = []
        peer.Gst = SimpleNamespace(MessageType=SimpleNamespace(ERROR=1, EOS=2), FlowReturn=SimpleNamespace(OK="ok"),
            Buffer=SimpleNamespace(new_allocate=lambda *args: SimpleNamespace(fill=lambda _offset, data: buffers.append(data))))
        queued_bytes = [0]
        peer.source = SimpleNamespace(get_property=lambda _key: queued_bytes[0], emit=lambda *args: "ok")
        batch = [AccessUnit(1, b"delta", False, 1), AccessUnit(2, b"idr", True, 1),
                 AccessUnit(4, b"gap-delta", False, 1), AccessUnit(5, b"new-idr", True, 1)]
        peer.store = SimpleNamespace(read_after=lambda _sequence: batch)
        self.assertTrue(peer.pump())
        self.assertEqual(buffers, [b"idr", b"new-idr"])
        queued_bytes[0] = 1048576
        batch[:] = [AccessUnit(6, b"full", True, 1)]
        self.assertTrue(peer.pump())
        self.assertTrue(peer.wait_keyframe)
        self.assertEqual(peer.dropped, 3)


class FakePeer:
    instances = []
    def __init__(self, camera_id, peer_id, store, send, *_args):
        self.camera_id, self.peer_id, self.store = camera_id, peer_id, store
        self.closed = False
        self.answers, self.candidates = [], []
        self.instances.append(self)
        send({"version": 1, "type": "offer", "cameraId": camera_id, "peerId": peer_id, "sdp": "test-offer"})

    def answer(self, value):
        self.answers.append(value)

    def ice(self, candidate, mline):
        self.candidates.append((candidate, mline))

    def pump(self):
        return True

    def close(self):
        self.closed = True


class SocketTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((ROOT / "preview.py").read_text())
        handler_node = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "PreviewHandler")
        namespace = {"BaseHTTPRequestHandler": BaseHTTPRequestHandler, "urlsplit": urlsplit}
        exec(compile(ast.Module(body=[handler_node], type_ignores=[]), "preview.py", "exec"), namespace)
        self.frames = [frame(), frame(3, "second")]
        self.streams = [{"camera_id": name, "id": source, "format": "webrtc", "runtime_session": "session",
                         "runtime": {"state": "online", "generation": 0}} for source, name in ((0, "first"), (3, "second"))]
        self.shutdown = threading.Event()
        self.registry = PeerRegistry(limit=2)
        FakePeer.instances = []
        self.stores = {0: object(), 3: object()}
        self.hub = RealtimeHub(lambda: {"runtime_session": "session", "frames": self.frames},
                               lambda: self.streams, self.stores, self.shutdown, FakePeer, self.registry)
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
                elif isinstance(event, Ping):
                    self.sock.sendall(self.ws.send(event.response()))

    def send(self, **message):
        self.sock.sendall(self.ws.send(TextMessage(data=json.dumps({"version": 1, **message}))))

    def test_one_socket_two_cameras_signaling_capacity_and_cleanup(self):
        self.until(lambda: len([m for m in self.messages if m["type"] == "detections"]) >= 2)
        detections = [m for m in self.messages if m["type"] == "detections"]
        self.assertEqual({(m["cameraId"], m["sourceId"]) for m in detections}, {("first", 0), ("second", 3)})
        self.send(type="watch", cameraId="second", peerId="one")
        self.until(lambda: any(m["type"] == "offer" for m in self.messages))
        self.assertIs(FakePeer.instances[0].store, self.stores[3])
        self.send(type="answer", cameraId="second", peerId="one", sdp="test-answer")
        self.send(type="ice", cameraId="second", peerId="one", candidate="candidate:test", sdpMLineIndex=0)
        self.send(type="watch", cameraId="first", peerId="two")
        self.until(lambda: len([m for m in self.messages if m["type"] == "offer"]) == 2)
        self.assertEqual(FakePeer.instances[0].answers, ["test-answer"])
        self.assertEqual(FakePeer.instances[0].candidates, [("candidate:test", 0)])
        self.send(type="watch", cameraId="first", peerId="three")
        self.until(lambda: any(m["type"] == "error" for m in self.messages))
        self.assertEqual(self.registry.active, 2)
        self.send(type="unwatch", cameraId="second", peerId="one")
        self.send(type="subscribe", cameraIds=[])
        self.until(lambda: any(m["type"] == "subscribed" for m in self.messages))
        self.assertTrue(FakePeer.instances[0].closed)
        self.assertEqual(self.registry.active, 1)
        self.sock.shutdown(socket.SHUT_RDWR)
        deadline = time.monotonic() + 2
        while self.registry.active and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.registry.active, 0)
        self.assertTrue(all(peer.closed for peer in FakePeer.instances))

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


if __name__ == "__main__":
    unittest.main()
