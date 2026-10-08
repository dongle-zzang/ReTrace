"""Backend current states/events -> existing preview WebSocket, outside GPU callbacks."""
import copy
import json
import threading
from urllib.request import build_opener, ProxyHandler


class ParkingRelay:
    def __init__(self, backend_url, hub, shutdown, interval=1.0):
        self.backend_url = backend_url.rstrip("/")
        self.hub, self.shutdown, self.interval = hub, shutdown, interval
        self.opener = build_opener(ProxyHandler({}))
        self.cursor = None
        self.current = {}
        self.color_current = {}
        self.pending = None
        self.thread = threading.Thread(target=self.run, name="parking-relay", daemon=True)

    def fetch(self, path):
        with self.opener.open(self.backend_url + path, timeout=1.5) as response:
            data = response.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            raise ValueError("Parking snapshot too large")
        return json.loads(data)

    def publish_current(self, cameras, events=()):
        active = {s["camera_id"] for s in self.hub.stream_snapshot()}
        incoming = {c["camera_id"]: copy.deepcopy(c) for c in cameras if c["camera_id"] in active}
        messages = [(e["camera_id"], "event", {"kind": "parking_occupancy_changed", **e})
                    for e in events if e["camera_id"] in active]
        event_cameras = {message[0] for message in messages}
        for camera_id in self.current.keys() | incoming.keys() | event_cameras:
            if camera_id not in active:
                continue
            camera = incoming.get(camera_id, {"camera_id": camera_id, "spaces": [],
                     "summary": {"total": 0, "occupied": 0, "empty": 0, "unknown": 0}})
            if camera_id in event_cameras or self.current.get(camera_id) != camera:
                messages.append((camera_id, "parking_status", camera))
        self.hub.publish_messages(messages)
        self.current = incoming

    def poll_once(self):
        if self.pending is None:
            snapshot = self.fetch("/api/parking")
            target = int(snapshot["event_cursor"])
            if self.cursor is None:
                self.publish_current(snapshot["cameras"])
                self.cursor = target
                return
            # Retention can remove all events; never move the published cursor back.
            self.pending = {"snapshot": snapshot, "target": max(self.cursor, target),
                            "after": self.cursor, "events": []}
        pending = self.pending
        # Freeze the snapshot while draining its history. Later commits belong to
        # the next pass; never send them ahead of this older snapshot. Four pages
        # per pass keep shutdown/retries responsive without dropping backlog.
        for _ in range(4):
            if pending["after"] >= pending["target"]:
                break
            page = self.fetch(f'/api/parking/events?after={pending["after"]}&limit=100')
            for event in page["events"]:
                event_id = int(event["event_id"])
                if event_id > pending["target"]:
                    pending["after"] = pending["target"]
                    break
                if event_id <= pending["after"]:
                    raise ValueError("Unordered parking events")
                pending["events"].append(event)
                pending["after"] = event_id
            if len(page["events"]) < 100:
                # Deleted history is not replayable; the snapshot still is.
                pending["after"] = pending["target"]
                break
        if pending["after"] >= pending["target"]:
            self.publish_current(pending["snapshot"]["cameras"], pending["events"])
            self.cursor = pending["target"]
            self.pending = None

    def unavailable(self):
        # Do not replay a pre-outage snapshot on recovery. Recollect from the
        # last published cursor against a fresh snapshot instead.
        self.pending = None
        cameras = copy.deepcopy(list(self.current.values()))
        for camera in cameras:
            camera["unavailable"] = True
            for space in camera["spaces"]:
                space["occupancy"] = "unknown"
            camera["summary"] = {"total": len(camera["spaces"]), "occupied": 0,
                                 "empty": 0, "unknown": len(camera["spaces"])}
        self.publish_current(cameras)

    def publish_color(self, spaces):
        active = {s['camera_id'] for s in self.hub.stream_snapshot()}
        incoming = {}
        for space in spaces:
            for camera_id in {z['cameraId'] for z in space['zones']} & active:
                incoming.setdefault(camera_id, []).append(space)
        messages = []
        for camera_id in sorted(active | self.color_current.keys()):
            if camera_id not in active:
                continue
            data = {'spaces': incoming.get(camera_id, [])}
            # Scores/timestamps change each frame; publish only state/topology changes.
            def signature(value):
                return [(s['parkingSpaceId'], s['label'], s['status'], s['conflict'],
                         [(z['zoneId'], z['cameraId'], z['enabled'], z['status']) for z in s['zones']])
                        for s in value['spaces']]
            previous = self.color_current.get(camera_id)
            if previous is None or signature(previous) != signature(data):
                messages.append((camera_id, 'parking.status_updated', data))
        self.hub.publish_messages(messages)
        self.color_current = {camera_id: {'spaces': copy.deepcopy(incoming.get(camera_id, []))} for camera_id in active}

    def poll_color(self):
        self.publish_color(self.fetch('/api/parking/status')['spaces'])

    def color_unavailable(self):
        spaces = {}
        for group in self.color_current.values():
            for space in group['spaces']:
                space = copy.deepcopy(space)
                space['status'], space['conflict'] = 'UNKNOWN', False
                for zone in space['zones']:
                    zone['status'], zone['score'] = 'UNKNOWN', None
                spaces[space['parkingSpaceId']] = space
        self.publish_color(list(spaces.values()))

    def run(self):
        while not self.shutdown.is_set():
            try:
                self.poll_once()
            except Exception:
                # Never print private Backend addresses or upstream exceptions.
                try:
                    self.unavailable()
                except Exception:
                    pass
            try:
                self.poll_color()
            except Exception:
                try:
                    self.color_unavailable()
                except Exception:
                    pass
            self.shutdown.wait(self.interval)
