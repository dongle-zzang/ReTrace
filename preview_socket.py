"""Same-origin WebSocket metadata, status and JPEG video frames.

Streaming callbacks never perform socket I/O. Each connection samples current
metadata at 10 Hz; obsolete detections are replaced, rather than queued.
Video is the latest JPEG per camera at a client-chosen rate; a slow client
skips frames instead of queueing them, so one connection carries every camera
without the browser's per-origin HTTP connection limit.
"""
import json
import math
import os
from queue import Queue, Empty, Full
import select
import socket
import struct
import threading
import time
from urllib.parse import urlsplit

from wsproto import ConnectionType, WSConnection
from wsproto.events import AcceptConnection, CloseConnection, Ping, Pong, TextMessage, BytesMessage
from person_metadata import utc_timestamp

VIDEO_FRAME_VERSION = 1
DEFAULT_VIDEO_FPS = 10
MAX_VIDEO_FPS = 30


def video_frame(camera_id, sequence, jpeg):
    """Binary message: version u8, cameraId length u8, cameraId UTF-8, sequence u32 BE, JPEG."""
    name = camera_id.encode()
    return struct.pack(f">BB{len(name)}sI", VIDEO_FRAME_VERSION, len(name), name, sequence & 0xFFFFFFFF) + jpeg


def normalized_objects(objects, width, height):
    result = []
    for person in objects:
        box = person["bbox"]
        if not all(math.isfinite(box[key]) for key in ("x", "y", "width", "height")):
            continue
        left = max(0.0, min(1.0, box["x"] / width))
        top = max(0.0, min(1.0, box["y"] / height))
        right = max(left, min(1.0, (box["x"] + box["width"]) / width))
        bottom = max(top, min(1.0, (box["y"] + box["height"]) / height))
        result.append({"trackId": None if person["track_id"] is None else str(person["track_id"]),
                       "bbox": {"x": left, "y": top, "width": right - left, "height": bottom - top},
                       "confidence": person["confidence"], "trackerConfidence": person["tracker_confidence"]})
    return result


def detection_message(frame, runtime_session):
    width, height = frame["bbox_width"], frame["bbox_height"]
    persons = []
    for person in frame["persons"]:
        box = person["bbox"]
        if not all(math.isfinite(box[key]) for key in ("x", "y", "width", "height")):
            continue
        left = max(0.0, min(1.0, box["x"] / width))
        top = max(0.0, min(1.0, box["y"] / height))
        right = max(left, min(1.0, (box["x"] + box["width"]) / width))
        bottom = max(top, min(1.0, (box["y"] + box["height"]) / height))
        # NvDCF IDs can exceed JavaScript's safe integer range.
        persons.append({"trackId": None if person["track_id"] is None else str(person["track_id"]),
                        "bbox": {"x": left, "y": top, "width": right - left, "height": bottom - top},
                        "confidence": person["confidence"], "trackerConfidence": person["tracker_confidence"]})
    return {"version": 1, "type": "detections", "cameraId": frame["camera_id"],
            "sourceId": frame["source_id"], "runtimeSession": runtime_session,
            "generation": frame["generation"], "timestamp": frame["timestamp"],
            "frameNumber": frame["frame_number"], "ptsNs": None if frame["pts_ns"] is None else str(frame["pts_ns"]),
            "inferenceDone": frame["inference_done"], "persons": persons,
            "vehicles": normalized_objects(frame.get("vehicles", []), width, height),
            "vehicleDetectionEnabled": frame.get("vehicle_detection_enabled", False),
            "vehicleInferenceDone": frame.get("vehicle_inference_done", False)}


class RealtimeHub:
    def __init__(self, metadata_snapshot, stream_snapshot, stores, shutdown, max_connections=16):
        self.metadata_snapshot, self.stream_snapshot = metadata_snapshot, stream_snapshot
        self.stores, self.shutdown = stores, shutdown
        self.connections = threading.BoundedSemaphore(max_connections)
        self.outbox_lock = threading.Lock()
        self.outboxes = {}
        self.parking_current = {}
        self.color_parking_current = {}
        self.allowed_origins = {value.strip() for value in os.environ.get(
            "PREVIEW_WS_ORIGINS", "").split(",") if value.strip()}

    def publish_message(self, camera_id, message_type, data):
        """Optional producer boundary for Re-ID/parking/events; no socket I/O.

        Delivery is live and bounded, not durable. Slow consumers disconnect.
        Call with detached JSON data, never with native PyDS objects.
        """
        self.publish_messages([(camera_id, message_type, data)])

    def publish_messages(self, items):
        """Publish ordered events and their final snapshots as one cache/queue update."""
        streams = {item["camera_id"]: item for item in self.stream_snapshot()}
        messages = []
        for camera_id, message_type, data in items:
            stream = streams.get(camera_id)
            if stream is None or message_type not in ("track_update", "parking_status", "parking.status_updated", "event"):
                raise ValueError("Unknown camera or event type")
            message = {"version": 1, "type": message_type, "cameraId": camera_id,
                       "sourceId": stream["id"], "runtimeSession": stream["runtime_session"],
                       "generation": stream["runtime"]["generation"], "timestamp": utc_timestamp(), "data": data}
            # Validate the entire batch before changing caches or any outbox.
            serialized = json.dumps(message, allow_nan=False)
            if len(serialized.encode()) > 65536:
                raise ValueError("Event message too large")
            messages.append(json.loads(serialized))
        with self.outbox_lock:
            for message in messages:
                if message["type"] == "parking_status":
                    self.parking_current[message["cameraId"]] = message
                elif message["type"] == "parking.status_updated":
                    self.color_parking_current[message["cameraId"]] = message
            for message in messages:
                for outgoing, overflow, subscription in self.outboxes.values():
                    if subscription[0] is not None and message["cameraId"] not in subscription[0]:
                        continue
                    try:
                        outgoing.put_nowait(message)
                    except Full:
                        overflow.set()

    def serve(self, handler):
        if handler.command != "GET" or handler.headers.get("Upgrade", "").lower() != "websocket":
            handler.send_error(426)
            return
        origin = handler.headers.get("Origin")
        if origin:
            parsed = urlsplit(origin)
            if (parsed.scheme not in ("http", "https") or parsed.netloc != handler.headers.get("Host")) and origin not in self.allowed_origins:
                handler.send_error(403)
                return
        if not self.connections.acquire(blocking=False):
            handler.send_error(503)
            return
        ws = WSConnection(ConnectionType.SERVER)
        outgoing = Queue(maxsize=1024)
        overflow = threading.Event()
        subscribed = None  # None = all configured cameras, [] = none.
        subscription = [None]
        outbox = (outgoing, overflow, subscription)
        previous_frames, previous_status = {}, {}
        video_interval = None  # None = metadata only.
        sent_video = {}  # camera_id -> (store sequence, monotonic send time)
        # Cameras are fixed for the process lifetime.
        video_stores = [(stream["camera_id"], self.stores[stream["id"]]) for stream in self.stream_snapshot()
                        if stream["id"] in self.stores]
        fragmented = ""
        handler.close_connection = True
        upgraded = False
        received_since_message = 0

        def send(message):
            try:
                outgoing.put_nowait(message)
            except Full:
                overflow.set()  # A blocked client is disconnected, not buffered.

        def wire(event, timeout=1.0):
            data = ws.send(event)
            handler.connection.settimeout(timeout)
            handler.connection.sendall(data)

        def command(message):
            nonlocal subscribed, previous_frames, previous_status, video_interval, sent_video
            if not isinstance(message, dict) or message.get("version", 1) != 1:
                raise ValueError("Invalid message")
            kind = message.get("type")
            cameras = {item["camera_id"]: item for item in self.stream_snapshot()}
            if kind == "subscribe":
                ids = message.get("cameraIds")
                if not isinstance(ids, list) or any(not isinstance(value, str) or value not in cameras for value in ids):
                    raise ValueError("Invalid subscription")
                video, fps = message.get("video", False), message.get("videoFps", DEFAULT_VIDEO_FPS)
                if (type(video) is not bool or isinstance(fps, bool) or not isinstance(fps, (int, float))
                        or not 1 <= fps <= MAX_VIDEO_FPS):
                    raise ValueError("Invalid video request")
                subscribed = set(ids)
                subscription[0] = subscribed
                previous_frames, previous_status = {}, {}
                video_interval = 1 / fps if video else None
                sent_video = {}
                send({"version": 1, "type": "subscribed", "cameraIds": sorted(subscribed),
                      "video": video, "videoFps": fps if video else None})
                with self.outbox_lock:
                    for current in list(self.parking_current.values()) + list(self.color_parking_current.values()):
                        if current["cameraId"] in subscribed:
                            send(current)
                return
            raise ValueError("Unsupported command")

        try:
            ws.initiate_upgrade_connection([(key.encode("ascii"), value.encode("latin1"))
                                           for key, value in handler.headers.items()], handler.path)
            list(ws.events())
            wire(AcceptConnection())
            upgraded = True
            with self.outbox_lock:
                self.outboxes[id(outgoing)] = outbox
                for current in list(self.parking_current.values()) + list(self.color_parking_current.values()):
                    send(current)
            # Room for a few JPEGs; a client that cannot drain them skips frames.
            handler.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 262144)
            # No addresses/credentials are reflected in hello or errors.
            wire(TextMessage(data=json.dumps({"version": 1, "type": "hello", "metadataHz": 10})))
            last_metadata = last_ping = last_pong = time.monotonic()
            while not self.shutdown.is_set() and not overflow.is_set():
                ready, _, _ = select.select([handler.connection], [], [], 0.02)
                if ready:
                    data = handler.connection.recv(65536)
                    if not data:
                        break
                    received_since_message += len(data)
                    if received_since_message > 131072:
                        wire(CloseConnection(code=1009, reason="Message too large"))
                        return
                    ws.receive_data(data)
                    for event in ws.events():
                        if isinstance(event, CloseConnection):
                            wire(event.response())
                            return
                        if isinstance(event, Ping):
                            wire(event.response())
                        elif isinstance(event, Pong):
                            last_pong = time.monotonic()
                        elif isinstance(event, BytesMessage):
                            wire(CloseConnection(code=1003, reason="JSON text required"))
                            return
                        elif isinstance(event, TextMessage):
                            fragmented += event.data
                            if len(fragmented) > 65536:
                                wire(CloseConnection(code=1009, reason="Message too large"))
                                return
                            if event.message_finished:
                                message = None
                                try:
                                    message = json.loads(fragmented)
                                    command(message)
                                except Exception:
                                    error = {"version": 1, "type": "error", "code": "command_failed"}
                                    if isinstance(message, dict):
                                        value = message.get("cameraId")
                                        if isinstance(value, str) and len(value) <= 64:
                                            error["cameraId"] = value
                                    send(error)
                                fragmented = ""
                                received_since_message = 0
                now = time.monotonic()
                if now - last_metadata >= 0.1:
                    snapshot = self.metadata_snapshot()
                    current_frames = {}
                    for frame in snapshot["frames"]:
                        camera_id = frame["camera_id"]
                        if subscribed is not None and camera_id not in subscribed:
                            continue
                        identity = (snapshot["runtime_session"], frame["generation"], frame["frame_number"])
                        current_frames[camera_id] = identity
                        if previous_frames.get(camera_id) != identity:
                            wire(TextMessage(data=json.dumps(detection_message(frame, snapshot["runtime_session"]), allow_nan=False)))
                    # Explicitly clear stale overlay when an output is invalidated.
                    for camera_id in previous_frames.keys() - current_frames.keys():
                        wire(TextMessage(data=json.dumps({"version": 1, "type": "detections", "cameraId": camera_id,
                                                         "runtimeSession": snapshot["runtime_session"], "timestamp": utc_timestamp(),
                                                         "persons": [], "vehicles": [], "vehicleInferenceDone": False,
                                                         "stale": True})))
                    previous_frames = current_frames
                    for stream in self.stream_snapshot():
                        camera_id = stream["camera_id"]
                        if subscribed is not None and camera_id not in subscribed:
                            continue
                        status = {"version": 1, "type": "camera_status", "cameraId": camera_id,
                                  "sourceId": stream["id"], "runtimeSession": stream["runtime_session"],
                                  "generation": stream["runtime"]["generation"], "status": stream["runtime"]}
                        identity = json.dumps(status)
                        if previous_status.get(camera_id) != identity:
                            wire(TextMessage(data=json.dumps({**status, "timestamp": utc_timestamp()})))
                        previous_status[camera_id] = identity
                    last_metadata = now
                for _ in range(128):
                    try:
                        message = outgoing.get_nowait()
                        if subscribed is None or "cameraId" not in message or message["cameraId"] in subscribed:
                            wire(TextMessage(data=json.dumps(message)))
                    except Empty:
                        break
                if video_interval is not None:
                    for camera_id, store in video_stores:
                        if camera_id not in subscribed:
                            continue
                        sequence, jpeg = store.latest()
                        last = sent_video.get(camera_id)
                        if jpeg is None or (last is not None and (last[0] == sequence or now - last[1] < video_interval)):
                            continue
                        # Replies above precede frames; large frames get longer to drain on slow links.
                        wire(BytesMessage(data=video_frame(camera_id, sequence, jpeg)), timeout=5.0)
                        sent_video[camera_id] = (sequence, now)
                    now = time.monotonic()
                if now - last_ping >= 15:
                    wire(Ping(payload=b"preview"))
                    last_ping = now
                if now - last_pong > 45:
                    break
        except Exception:
            if not upgraded:
                handler.send_error(400)
        finally:
            with self.outbox_lock:
                self.outboxes.pop(id(outgoing), None)
            self.connections.release()
