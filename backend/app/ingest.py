"""Bounded snapshot polling. No imports of preview.py, GI, PyDS, or GPU code."""
from datetime import datetime, timedelta, timezone
import copy
import math
import re
import threading
import time

import httpx
from pydantic import ValidationError
from sqlalchemy import delete, select

from camera_config import load_public_cameras
from .db import Base, Camera, CameraStatus, ParkingSpace, ParkingEvent, PersonTrack
from .occupancy import OccupancyEvaluator
from .parking import set_decision
from .schemas import FrameIn, StatusOut

STATES = {"connecting", "online", "degraded", "offline", "reconnecting"}
ERRORS = {"rtsp_error", "rtsp_timeout", "source_eos", "pipeline_error",
          "pipeline_setup_failed", "no_frames", "output_stalled", "rtsp_config_error"}


def utcnow():
    return datetime.now(timezone.utc)


def aware(value):
    # SQLite CPU tests return naive datetime; PostgreSQL returns timezone-aware UTC.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (TypeError, ValueError):
        return None


def counter(value):
    return value if type(value) is int and 0 <= value <= 2147483647 else 0


def normalize_status(camera_id, runtime, now):
    runtime = runtime if isinstance(runtime, dict) else {}
    state = runtime.get("state")
    state = state if isinstance(state, str) and state in STATES else "offline"
    fps = runtime.get("fps", 0)
    fps = float(fps) if type(fps) in (int, float) and math.isfinite(fps) and fps >= 0 else 0.0
    error = runtime.get("last_error")
    return dict(camera_id=camera_id, state=state, fps=0.0 if state == "offline" else fps,
                last_frame_at=timestamp(runtime.get("last_frame_at")),
                last_error=error if isinstance(error, str) and error in ERRORS else None,
                reconnect_count=counter(runtime.get("reconnect_count")),
                generation=counter(runtime.get("generation")), observed_at=now)


def status_out(row, stale_after, now=None):
    now = now or utcnow()
    stale = (now - aware(row.observed_at)).total_seconds() > stale_after
    return StatusOut(camera_id=row.camera_id, state="offline" if stale else row.state,
                     fps=0.0 if stale else row.fps, last_frame_at=row.last_frame_at,
                     last_error="preview_unreachable" if stale else row.last_error,
                     reconnect_count=row.reconnect_count, generation=row.generation,
                     runtime_session=row.runtime_session, observed_at=aware(row.observed_at), stale=stale)


def sync_cameras(session, path, now=None):
    """YAML -> DB only. Removed cameras are disabled to retain track references."""
    now = now or utcnow()
    entries = load_public_cameras(path)
    incoming = {entry["camera_id"] for entry in entries}
    for entry in entries:
        camera = session.get(Camera, entry["camera_id"])
        if camera is None:
            camera = Camera(**entry)
            session.add(camera)
            session.flush()
        else:
            for key, value in entry.items():
                setattr(camera, key, value)
        status = session.get(CameraStatus, camera.camera_id)
        if status is None:
            status = CameraStatus(camera_id=camera.camera_id, state="offline", fps=0,
                                  last_error="not_observed", observed_at=now,
                                  reconnect_count=0, generation=0)
            session.add(status)
        status.state, status.fps = "offline", 0
        status.last_error = "not_observed" if camera.enabled else "disabled"
        status.runtime_session = None
        status.observed_at = now
    for camera in session.scalars(select(Camera)):
        if camera.camera_id not in incoming:
            camera.enabled, camera.source_id = False, None
            status = session.get(CameraStatus, camera.camera_id)
            status.state, status.fps, status.last_error = "offline", 0, "disabled"
            status.runtime_session = None


class LiveStore:
    """One latest valid frame per camera; TTL prevents frozen/stale bbox display."""
    def __init__(self, stale_after):
        self.stale_after = stale_after
        self.lock = threading.Lock()
        self.frames = {}

    def replace(self, frames):
        with self.lock:
            self.frames = frames

    def get(self, camera_id, session_id, generation, now=None):
        with self.lock:
            item = self.frames.get(camera_id)
        if item is None:
            return None
        identity, frame = item
        age = ((now or utcnow()) - frame.timestamp).total_seconds()
        if identity != session_id or frame.generation != generation or not 0 <= age <= self.stale_after:
            return None
        return frame


class Poller:
    def __init__(self, settings, engine, sessions, live, transport=None):
        self.settings, self.engine, self.sessions, self.live = settings, engine, sessions, live
        self.client = httpx.Client(base_url=settings.preview_url, timeout=settings.http_timeout,
                                   transport=transport, trust_env=False, follow_redirects=False)
        self.stop = threading.Event()
        self.ready = False
        self.last_success = None
        self.preview_state = "unknown"
        self.last_cleanup = 0.0
        self.occupancy = OccupancyEvaluator(settings.parking_policy)

    def initialize(self):
        Base.metadata.create_all(self.engine)
        with self.sessions.begin() as session:
            sync_cameras(session, self.settings.cameras_path)
            for space in session.scalars(select(ParkingSpace).with_for_update()):
                set_decision(session, space, "unknown", "backend_restart", utcnow())
        self.ready = True

    def fetch(self, path):
        # Limit transfer and parsing memory even if the upstream is misconfigured.
        with self.client.stream("GET", path) as response:
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > 8 * 1024 * 1024:
                    raise ValueError("Preview snapshot too large")
            import json
            return json.loads(data)

    def poll_once(self):
        now = utcnow()
        streams = None
        try:
            streams = self.fetch("/streams.json")
            if not isinstance(streams, list) or len(streams) > 1000:
                raise ValueError("Invalid streams snapshot")
            by_camera = {}
            for stream in streams:
                if not isinstance(stream, dict) or not isinstance(stream.get("camera_id"), str):
                    raise ValueError("Invalid stream entry")
                if stream["camera_id"] in by_camera:
                    raise ValueError("Duplicate stream entry")
                by_camera[stream["camera_id"]] = stream
            self.last_success = time.monotonic()
            self.preview_state = "reachable"
        except (httpx.HTTPError, ValueError):
            streams, by_camera = None, {}
            self.preview_state = "unreachable"
        frames, metadata_session = [], None
        if streams is not None:
            try:
                payload = self.fetch("/metadata.json")
                if not isinstance(payload, dict) or not isinstance(payload.get("frames"), list):
                    raise ValueError("Invalid metadata snapshot")
                if len(payload["frames"]) > 1000:
                    raise ValueError("Invalid metadata snapshot")
                metadata_session = payload.get("runtime_session")
                frames = [FrameIn.model_validate(frame) for frame in payload["frames"]]
            except (httpx.HTTPError, ValueError, ValidationError):
                frames = []
        live_frames = {}
        trial = copy.deepcopy(self.occupancy)
        try:
            with self.sessions.begin() as session:
                cameras = list(session.scalars(select(Camera)))
                statuses = {}
                for camera in cameras:
                    row = session.get(CameraStatus, camera.camera_id)
                    stream = by_camera.get(camera.camera_id)
                    if camera.enabled and stream and stream.get("id") == camera.source_id:
                        normalized = normalize_status(camera.camera_id, stream.get("runtime"), now)
                        for key, value in normalized.items():
                            setattr(row, key, value)
                        identity = stream.get("runtime_session")
                        row.runtime_session = identity if isinstance(identity, str) and re.fullmatch(r"[a-f0-9]{32}", identity) else None
                    else:
                        row.state, row.fps = "offline", 0.0
                        row.last_error = ("disabled" if not camera.enabled else
                                          "preview_unreachable" if streams is None else "camera_missing")
                        # Keep last successful observation time when upstream is unreachable.
                        if streams is not None:
                            row.observed_at = now
                    statuses[camera.camera_id] = (camera, row)
                for frame in frames:
                    pair = statuses.get(frame.camera_id)
                    if pair is None:
                        continue
                    camera, status = pair
                    age = (now - frame.timestamp).total_seconds()
                    if (not camera.enabled or status.state not in ("online", "degraded")
                            or frame.source_id != camera.source_id or frame.generation != status.generation
                            or not status.runtime_session or metadata_session != status.runtime_session
                            or not -1 <= age <= self.settings.stale_after):
                        continue
                    live_frames[camera.camera_id] = (status.runtime_session, frame)
                    for person in frame.persons:
                        if person.track_id is None:
                            continue
                        key = (camera.camera_id, status.runtime_session, frame.generation, str(person.track_id))
                        record = session.get(PersonTrack, key)
                        if record is None:
                            session.add(PersonTrack(camera_id=key[0], runtime_session=key[1], generation=key[2],
                                                    track_id=key[3], first_seen_at=frame.timestamp,
                                                    last_seen_at=frame.timestamp, max_confidence=person.confidence))
                            session.flush()
                        elif frame.timestamp > aware(record.last_seen_at):
                            record.last_seen_at = frame.timestamp
                            if person.confidence is not None:
                                record.max_confidence = max(record.max_confidence or 0.0, person.confidence)
                spaces = list(session.scalars(select(ParkingSpace).with_for_update()))
                present_ids = {space.space_id for space in spaces}
                trial.memory = {key: value for key, value in trial.memory.items() if key in present_ids}
                by_space_camera = {}
                for space in spaces:
                    by_space_camera.setdefault(space.camera_id, []).append(space)
                for camera_id, camera_spaces in by_space_camera.items():
                    item = live_frames.get(camera_id)
                    if item is None:
                        trial.forget_camera(camera_id, camera_spaces)
                        for space in camera_spaces:
                            set_decision(session, space, "unknown", "camera_or_metadata_unavailable", now)
                        continue
                    identity, frame = item
                    lookup = {space.space_id: space for space in camera_spaces}
                    for decision in trial.observe(frame, identity, camera_spaces):
                        evidence = {key: value for key, value in decision.evidence.items() if key != "candidates"}
                        evidence.update(runtime_session=identity, generation=frame.generation,
                                        frame_number=frame.frame_number)
                        set_decision(session, lookup[decision.space_id], decision.occupancy,
                                     decision.reason, decision.timestamp, evidence)
                if time.monotonic() - self.last_cleanup >= 60:
                    session.execute(delete(PersonTrack).where(PersonTrack.last_seen_at <
                                    now - timedelta(days=self.settings.track_retention_days)))
                    self.last_cleanup = time.monotonic()
                    session.execute(delete(ParkingEvent).where(ParkingEvent.observed_at <
                                    now - timedelta(days=self.settings.track_retention_days)))
            # Publish evaluator state only after the DB transaction commits.
            self.occupancy = trial
        finally:
            # DB failures cannot keep old bbox visible. Never log exception strings.
            self.live.replace(live_frames)

    def run(self):
        while not self.stop.is_set():
            try:
                if not self.ready:
                    self.initialize()
                self.poll_once()
            except Exception:
                # Retry on next interval, including DB restart; never leak upstream/DB credentials.
                self.live.replace({})
            self.stop.wait(self.settings.poll_interval)

    def preview_health(self):
        if self.last_success is not None and time.monotonic() - self.last_success > self.settings.stale_after:
            return "unreachable"
        return self.preview_state
