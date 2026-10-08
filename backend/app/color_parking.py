"""Additive color-parking API and bounded latest-frame worker (one backend process)."""
from datetime import datetime, timezone
import threading
import time
from uuid import UUID

import cv2
import httpx
import numpy as np
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .color_analysis import StableState, aggregate, color_score, histogram
from .db import Camera, ColorParkingSpace, ParkingZone
from .schemas import PolygonPoint, validate_polygon

router = APIRouter(prefix='/api/parking')


class SpaceCreate(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    label: str = Field(min_length=1, max_length=128)


class ZoneCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    cameraId: str = Field(min_length=1, max_length=128)
    parkingSpaceId: UUID
    polygon: list[PolygonPoint] = Field(min_length=3, max_length=64)
    enabled: bool = True
    threshold: float = Field(default=.3, ge=0, le=1, allow_inf_nan=False)
    hysteresis: float = Field(default=.05, ge=0, le=.5, allow_inf_nan=False)
    confirmFrames: int = Field(default=3, ge=1, le=30, strict=True)

    @field_validator('polygon')
    @classmethod
    def valid_polygon(cls, value):
        return validate_polygon(value)

    @model_validator(mode='after')
    def valid_band(self):
        if not self.hysteresis <= self.threshold <= 1 - self.hysteresis:
            raise ValueError('Hysteresis band must fit in 0..1')
        return self


class ZoneUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    polygon: list[PolygonPoint] | None = Field(default=None, min_length=3, max_length=64)
    enabled: bool | None = None
    threshold: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    hysteresis: float | None = Field(default=None, ge=0, le=.5, allow_inf_nan=False)
    confirmFrames: int | None = Field(default=None, ge=1, le=30, strict=True)

    @model_validator(mode='after')
    def validate_update(self):
        if not self.model_fields_set or any(getattr(self, f) is None for f in self.model_fields_set):
            raise ValueError('Non-null update required')
        if self.polygon is not None:
            validate_polygon(self.polygon)
        return self


def zone_out(zone):
    return {'id': zone.id, 'cameraId': zone.camera_id, 'parkingSpaceId': zone.parking_space_id,
            'polygon': zone.polygon, 'enabled': zone.enabled, 'threshold': zone.threshold,
            'hysteresis': zone.hysteresis, 'confirmFrames': zone.confirm_frames,
            'calibrated': zone.baseline is not None, 'revision': zone.revision}


def find_zone(session, zone_id):
    zone = session.get(ParkingZone, str(zone_id), with_for_update=True)
    if zone is None:
        raise HTTPException(404, 'Parking zone not found')
    return zone


@router.get('/spaces')
def spaces(request: Request):
    with request.app.state.sessions() as session:
        return [{'id': s.id, 'label': s.label} for s in session.scalars(select(ColorParkingSpace).order_by(ColorParkingSpace.label))]


@router.post('/spaces', status_code=201)
def create_space(body: SpaceCreate, request: Request):
    try:
        with request.app.state.sessions.begin() as session:
            space = ColorParkingSpace(label=body.label)
            session.add(space)
            session.flush()
            result = {'id': space.id, 'label': space.label}
        return result
    except IntegrityError:
        raise HTTPException(409, 'Parking label already exists') from None


@router.get('/zones')
def zones(request: Request, cameraId: str | None = Query(default=None)):
    with request.app.state.sessions() as session:
        query = select(ParkingZone).order_by(ParkingZone.id)
        if cameraId is not None:
            query = query.where(ParkingZone.camera_id == cameraId)
        return [zone_out(z) for z in session.scalars(query)]


@router.post('/zones', status_code=201)
def create_zone(body: ZoneCreate, request: Request):
    with request.app.state.sessions.begin() as session:
        if session.get(Camera, body.cameraId) is None or session.get(ColorParkingSpace, str(body.parkingSpaceId)) is None:
            raise HTTPException(404, 'Camera or parking space not found')
        zone = ParkingZone(camera_id=body.cameraId, parking_space_id=str(body.parkingSpaceId),
                           polygon=[p.model_dump() for p in body.polygon], enabled=body.enabled,
                           threshold=body.threshold, hysteresis=body.hysteresis, confirm_frames=body.confirmFrames)
        session.add(zone)
        session.flush()
        return zone_out(zone)


@router.patch('/zones/{zone_id}')
def edit_zone(zone_id: UUID, body: ZoneUpdate, request: Request):
    with request.app.state.sessions.begin() as session:
        zone = find_zone(session, zone_id)
        for field in body.model_fields_set:
            value = getattr(body, field)
            if field == 'polygon':
                value = [p.model_dump() for p in value]
                if value != zone.polygon:
                    zone.baseline = None
            setattr(zone, 'confirm_frames' if field == 'confirmFrames' else field, value)
        if not zone.hysteresis <= zone.threshold <= 1 - zone.hysteresis:
            raise HTTPException(422, 'Hysteresis band must fit in 0..1')
        zone.revision += 1
        session.flush()
        return zone_out(zone)


@router.delete('/zones/{zone_id}', status_code=204)
def delete_zone(zone_id: UUID, request: Request):
    with request.app.state.sessions.begin() as session:
        session.delete(find_zone(session, zone_id))
    return Response(status_code=204)


@router.post('/zones/{zone_id}/calibrate')
def calibrate(zone_id: UUID, request: Request):
    worker = request.app.state.color_parking
    # Avoid holding a DB lock during HTTP/decode work; revision rejects concurrent edits.
    with request.app.state.sessions() as session:
        zone = find_zone(session, zone_id)
        revision, camera_id, polygon = zone.revision, zone.camera_id, zone.polygon
        camera = session.get(Camera, camera_id)
        if not zone.enabled or not camera.enabled:
            raise HTTPException(409, 'Zone or camera disabled')
    try:
        image, _, _ = worker.fetch_frame(camera_id)
        baseline = histogram(image, polygon)
    except (httpx.HTTPError, ValueError, KeyError, cv2.error):
        raise HTTPException(409, 'Fresh frame unavailable') from None
    if baseline is None:
        raise HTTPException(422, 'ROI too small')
    with request.app.state.sessions.begin() as session:
        zone = find_zone(session, zone_id)
        if zone.revision != revision:
            raise HTTPException(409, 'Zone changed; retry calibration')
        zone.baseline = baseline.tolist()
        zone.revision += 1
        session.flush()
        return zone_out(zone)


@router.delete('/zones/{zone_id}/calibrate', status_code=204)
def clear_calibration(zone_id: UUID, request: Request):
    with request.app.state.sessions.begin() as session:
        zone = find_zone(session, zone_id)
        zone.baseline = None
        zone.revision += 1
    return Response(status_code=204)


@router.get('/status')
def status(request: Request):
    return request.app.state.color_parking.snapshot()


class ColorParkingWorker:
    def __init__(self, settings, sessions, transport=None):
        self.settings, self.sessions = settings, sessions
        self.client = httpx.Client(base_url=settings.preview_url, timeout=settings.http_timeout,
                                   transport=transport, trust_env=False, follow_redirects=False)
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.observations = {}
        self.states = {}
        self.thread = threading.Thread(target=self.run, name='color-parking', daemon=True)

    def fetch_frame(self, camera_id):
        from urllib.parse import quote
        started = time.monotonic()
        with self.client.stream('GET', '/snapshots/' + quote(camera_id, safe='') + '.jpg') as response:
            response.raise_for_status()
            age = float(response.headers['X-Frame-Age'])
            if not 0 <= age <= self.settings.color_parking_stale_after:
                raise ValueError('Stale frame')
            identity = response.headers['X-Frame-Identity']
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > 8 * 1024 * 1024 or time.monotonic() - started > self.settings.http_timeout * 2:
                    raise ValueError('Frame too large')
        image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError('Invalid JPEG')
        # Bound mask/HSV cost; the ROI minimum is measured at analysis resolution.
        height, width = image.shape[:2]
        if width > 640:
            image = cv2.resize(image, (640, max(1, round(height * 640 / width))), interpolation=cv2.INTER_AREA)
        age += time.monotonic() - started
        if age > self.settings.color_parking_stale_after:
            raise ValueError("Stale frame")
        return image, identity, age

    def poll_once(self):
        with self.sessions() as session:
            zones = list(session.scalars(select(ParkingZone)))
            cameras = {c.camera_id: c.enabled for c in session.scalars(select(Camera))}
        grouped = {}
        for zone in zones:
            grouped.setdefault(zone.camera_id, []).append(zone)
        active_keys = {(z.id, z.revision) for z in zones}
        self.states = {key: state for key, state in self.states.items() if key in active_keys}
        with self.lock:
            self.observations = {key: obs for key, obs in self.observations.items() if key in active_keys}
        for camera_id, group in grouped.items():
            if self.stop.is_set():
                break
            image, identity, age = None, None, 0
            if cameras.get(camera_id) and any(z.enabled for z in group):
                try:
                    image, identity, age = self.fetch_frame(camera_id)
                except (httpx.HTTPError, ValueError, KeyError, cv2.error):
                    pass
            for zone in group:
                key = (zone.id, zone.revision)
                state = self.states.setdefault(key, StableState())
                with self.lock:
                    previous = self.observations.get(key)
                now = time.monotonic()
                if previous and (now - previous['observed'] > self.settings.color_parking_stale_after
                                 or (identity and previous['identity'] and identity.rsplit(':', 1)[0] != previous['identity'].rsplit(':', 1)[0])):
                    state.update(None, 0, 0, 1)
                if image is not None and zone.enabled and previous and previous['identity'] == identity:
                    continue  # A repeated JPEG must never count toward confirmation or renew TTL.
                hist = histogram(image, zone.polygon) if image is not None and zone.enabled else None
                score = color_score(hist, zone.baseline) if hist is not None else None
                state.update(score, zone.threshold, zone.hysteresis, zone.confirm_frames)
                result = {'identity': identity, 'observed': now - age,
                          'status': state.status, 'score': score,
                          'updatedAt': datetime.fromtimestamp(time.time() - age, timezone.utc).isoformat() if score is not None else None}
                with self.lock:
                    self.observations[key] = result

    def snapshot(self):
        with self.sessions() as session:
            spaces = list(session.scalars(select(ColorParkingSpace).order_by(ColorParkingSpace.label)))
            zones = list(session.scalars(select(ParkingZone).order_by(ParkingZone.id)))
            cameras = {c.camera_id: c.enabled for c in session.scalars(select(Camera))}
        with self.lock:
            observations = dict(self.observations)
        grouped = {}
        for zone in zones:
            obs = observations.get((zone.id, zone.revision))
            valid = (zone.enabled and cameras.get(zone.camera_id) and obs is not None
                     and 0 <= time.monotonic() - obs['observed'] <= self.settings.color_parking_stale_after)
            row = {'zoneId': zone.id, 'cameraId': zone.camera_id, 'enabled': zone.enabled,
                   'status': obs['status'] if valid else 'UNKNOWN',
                   'score': obs['score'] if valid else None, 'updatedAt': obs['updatedAt'] if obs else None}
            grouped.setdefault(zone.parking_space_id, []).append(row)
        results = []
        for space in spaces:
            rows = grouped.get(space.id, [])
            state, conflict = aggregate([r for r in rows if r['enabled']])
            results.append({'parkingSpaceId': space.id, 'label': space.label,
                            'status': state, 'conflict': conflict, 'zones': rows})
        return {'spaces': results}

    def run(self):
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                self.poll_once()
            except Exception:
                # Fail closed on DB/worker failure; never print private upstream values.
                self.states.clear()
                with self.lock:
                    self.observations.clear()
            self.stop.wait(max(.01, self.settings.color_parking_interval - (time.monotonic() - started)))
