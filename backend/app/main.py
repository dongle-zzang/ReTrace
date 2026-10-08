from contextlib import asynccontextmanager
import threading
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

from .db import Camera, CameraStatus, ParkingSpace, ParkingEvent, PersonTrack, make_engine, sessions_for
from .ingest import LiveStore, Poller, status_out, utcnow, aware
from .parking import event_out, record_transition
from .schemas import (CameraOut, HealthOut, LiveTrackOut, StatusOut, StatusSummary, TrackOut, TracksOut,
                      ParkingSpaceCreate, ParkingSpaceUpdate, ParkingSpaceOut, ParkingSummary)
from .settings import Settings
from .color_parking import ColorParkingWorker, router as color_parking_router


def create_app(settings=None, engine=None, transport=None, start_poller=True):
    @asynccontextmanager
    async def lifespan(app):
        config = settings or Settings.from_env()
        db = engine if engine is not None else make_engine(config.database_url)
        sessions = sessions_for(db)
        live = LiveStore(config.stale_after)
        poller = Poller(config, db, sessions, live, transport)
        app.state.config, app.state.sessions = config, sessions
        app.state.poller, app.state.live = poller, live
        try:
            poller.initialize()
        except SQLAlchemyError:
            pass  # DB can recover later; health/API respond 503 in the meantime.
        color_parking = ColorParkingWorker(config, sessions, transport)
        app.state.color_parking = color_parking
        thread = threading.Thread(target=poller.run, name="preview-poller", daemon=True)
        if start_poller:
            thread.start()
            color_parking.thread.start()
        try:
            yield
        finally:
            poller.stop.set()
            color_parking.stop.set()
            if start_poller:
                thread.join(timeout=10)
                color_parking.thread.join(timeout=10)
            if not thread.is_alive():
                poller.client.close()
            if not color_parking.thread.is_alive():
                color_parking.client.close()
            if engine is None:
                db.dispose()

    app = FastAPI(title="ReTrace Backend", lifespan=lifespan)
    app.include_router(color_parking_router)
    origins = settings.cors_origins if settings is not None else Settings.cors_origins_from_env()
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(origins),
                           allow_credentials=False, allow_methods=["GET", "POST", "PATCH", "DELETE"],
                           allow_headers=["Content-Type"])

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError):
        return JSONResponse(status_code=503, content={"detail": "Database unavailable"})

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # Default validation details may echo arbitrary query/path inputs.
        return JSONResponse(status_code=422, content={"detail": "Invalid request"})

    def find_camera(session, camera_id):
        camera = session.get(Camera, camera_id)
        if camera is None:
            raise HTTPException(status_code=404, detail="Camera not found")
        return camera

    def public_status(session, camera_id):
        return status_out(session.get(CameraStatus, camera_id), app.state.config.stale_after)

    def public_camera(session, camera):
        return CameraOut(camera_id=camera.camera_id, floor=camera.floor, name=camera.name,
                         enabled=camera.enabled, source_id=camera.source_id,
                         preview_path=f"/mjpeg/source{camera.source_id}" if camera.enabled else None,
                         status=public_status(session, camera.camera_id))

    def find_space(session, camera_id, space_id, lock=False):
        find_camera(session, camera_id)
        query = select(ParkingSpace).where(ParkingSpace.camera_id == camera_id,
                                           ParkingSpace.space_id == str(space_id))
        if lock:
            query = query.with_for_update()
        space = session.scalar(query)
        if space is None:
            raise HTTPException(status_code=404, detail="Parking space not found")
        return space

    def current_space(space):
        result = ParkingSpaceOut.model_validate(space)
        at = space.occupancy_observed_at
        if at is None or not 0 <= (utcnow() - aware(at)).total_seconds() <= app.state.config.stale_after:
            result.occupancy = "unknown"
        return result

    @app.post("/api/cameras/{camera_id}/parking-spaces", response_model=ParkingSpaceOut, status_code=201)
    def create_parking_space(camera_id: str, body: ParkingSpaceCreate):
        with app.state.sessions.begin() as session:
            find_camera(session, camera_id)
            space = ParkingSpace(camera_id=camera_id, name=body.name,
                                 polygon=[point.model_dump() for point in body.polygon])
            session.add(space)
            session.flush()
            result = ParkingSpaceOut.model_validate(space)
        return result

    @app.get("/api/cameras/{camera_id}/parking-spaces", response_model=list[ParkingSpaceOut])
    def parking_spaces(camera_id: str):
        with app.state.sessions() as session:
            find_camera(session, camera_id)
            return [current_space(space) for space in session.scalars(
                select(ParkingSpace).where(ParkingSpace.camera_id == camera_id)
                .order_by(ParkingSpace.created_at, ParkingSpace.space_id))]

    @app.get("/api/cameras/{camera_id}/parking-spaces/{space_id}", response_model=ParkingSpaceOut)
    def parking_space(camera_id: str, space_id: UUID):
        with app.state.sessions() as session:
            return current_space(find_space(session, camera_id, space_id))

    @app.patch("/api/cameras/{camera_id}/parking-spaces/{space_id}", response_model=ParkingSpaceOut)
    def edit_parking_space(camera_id: str, space_id: UUID, body: ParkingSpaceUpdate):
        if not body.model_fields_set:
            raise HTTPException(status_code=422, detail="At least one update field is required")
        with app.state.sessions.begin() as session:
            space = find_space(session, camera_id, space_id, lock=True)
            if "name" in body.model_fields_set:
                space.name = body.name
            if "polygon" in body.model_fields_set:
                polygon = [point.model_dump() for point in body.polygon]
                if polygon != space.polygon:
                    previous = space.occupancy
                    space.polygon = polygon
                    space.revision += 1
                    space.occupancy = "unknown"
                    space.occupancy_observed_at = None
                    record_transition(session, space, previous, "polygon_changed", utcnow())
            space.updated_at = utcnow()
            session.flush()
            result = current_space(space)
        return result

    @app.delete("/api/cameras/{camera_id}/parking-spaces/{space_id}", status_code=204)
    def delete_parking_space(camera_id: str, space_id: UUID):
        with app.state.sessions.begin() as session:
            session.delete(find_space(session, camera_id, space_id, lock=True))
        return Response(status_code=204)

    @app.get("/api/cameras/{camera_id}/parking-summary", response_model=ParkingSummary)
    def parking_summary(camera_id: str):
        with app.state.sessions() as session:
            find_camera(session, camera_id)
            counts = {state: 0 for state in ("occupied", "empty", "unknown")}
            for space in session.scalars(select(ParkingSpace).where(ParkingSpace.camera_id == camera_id)):
                counts[current_space(space).occupancy] += 1
            return ParkingSummary(camera_id=camera_id, total=sum(counts.values()),
                                  **{state: counts.get(state, 0) for state in ("occupied", "empty", "unknown")})

    @app.get("/api/parking")
    def parking_snapshot():
        with app.state.sessions() as session:
            # Cursor and rows must describe one committed instant. READ COMMITTED
            # could include a newer state whose event is replayed in a later batch.
            if session.get_bind().dialect.name == "postgresql":
                session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            cursor = session.scalar(select(func.max(ParkingEvent.event_id))) or 0
            groups = {}
            for space in session.scalars(select(ParkingSpace).order_by(ParkingSpace.camera_id, ParkingSpace.space_id)):
                groups.setdefault(space.camera_id, []).append(current_space(space))
            cameras = []
            for camera_id, spaces in groups.items():
                counts = {state: sum(s.occupancy == state for s in spaces) for state in ("occupied", "empty", "unknown")}
                cameras.append({"camera_id": camera_id, "spaces": [s.model_dump(include={"space_id", "revision", "occupancy", "occupancy_observed_at"}) for s in spaces],
                                "summary": {"total": len(spaces), **counts}})
            return {"event_cursor": str(cursor), "cameras": cameras}

    @app.get("/api/parking/events")
    def parking_events(after: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=500)):
        with app.state.sessions() as session:
            events = list(session.scalars(select(ParkingEvent).where(ParkingEvent.event_id > after)
                          .order_by(ParkingEvent.event_id).limit(limit)))
            return {"cursor": str(events[-1].event_id if events else after),
                    "events": [event_out(event) for event in events]}

    @app.get("/api/cameras/{camera_id}/parking-assessments")
    def parking_assessments(camera_id: str):
        with app.state.sessions() as session:
            find_camera(session, camera_id)
            # Memory snapshot is replaced after commit; never mutate it here.
            decisions = app.state.poller.occupancy.diagnostics.get(camera_id, [])
            revisions = dict(session.execute(select(ParkingSpace.space_id, ParkingSpace.revision)
                             .where(ParkingSpace.camera_id == camera_id)).all())
            return {"camera_id": camera_id, "policy": app.state.config.parking_policy,
                    "assessments": [decision for decision in decisions
                                    if revisions.get(decision.space_id) == decision.revision
                                    and 0 <= (utcnow() - decision.timestamp).total_seconds() <= app.state.config.stale_after]}

    @app.get("/api/cameras/{camera_id}/vehicles")
    def camera_vehicles(camera_id: str):
        with app.state.sessions() as session:
            find_camera(session, camera_id)
            status = public_status(session, camera_id)
            frame = (app.state.live.get(camera_id, status.runtime_session, status.generation)
                     if not status.stale and status.state in ("online", "degraded") else None)
            return {"camera_id": camera_id, "runtime_session": status.runtime_session,
                    "generation": status.generation, "timestamp": frame.timestamp if frame else None,
                    "bbox_width": frame.bbox_width if frame else None, "bbox_height": frame.bbox_height if frame else None,
                    "vehicle_detection_enabled": frame.vehicle_detection_enabled if frame else False,
                    "vehicle_inference_done": frame.vehicle_inference_done if frame else False,
                    "vehicles": [{**v.model_dump(), "track_id": str(v.track_id) if v.track_id is not None else None,
                                  "class": "vehicle"} for v in frame.vehicles] if frame else []}

    @app.get("/api/health", response_model=HealthOut)
    def health():
        try:
            with app.state.sessions() as session:
                session.execute(text("SELECT 1"))
            if not app.state.poller.ready:
                raise RuntimeError("Not initialized")
        except (SQLAlchemyError, RuntimeError):
            return JSONResponse(status_code=503, content=HealthOut(
                status="unavailable", database="unavailable", preview=app.state.poller.preview_health()).model_dump())
        return HealthOut(status="ok", database="ok", preview=app.state.poller.preview_health())

    @app.get("/api/cameras", response_model=list[CameraOut])
    def cameras():
        with app.state.sessions() as session:
            return [public_camera(session, camera) for camera in session.scalars(select(Camera).order_by(Camera.camera_id))]

    @app.get("/api/cameras/{camera_id}", response_model=CameraOut)
    def camera_detail(camera_id: str):
        with app.state.sessions() as session:
            return public_camera(session, find_camera(session, camera_id))

    @app.get("/api/cameras/{camera_id}/status", response_model=StatusOut)
    def camera_status(camera_id: str):
        with app.state.sessions() as session:
            find_camera(session, camera_id)
            return public_status(session, camera_id)

    @app.get("/api/status", response_model=StatusSummary)
    def all_status():
        with app.state.sessions() as session:
            statuses = [public_status(session, camera.camera_id) for camera in
                        session.scalars(select(Camera).order_by(Camera.camera_id))]
        counts = {state: 0 for state in ("connecting", "online", "degraded", "offline", "reconnecting")}
        for status in statuses:
            counts[status.state] += 1
        return StatusSummary(counts=counts, cameras=statuses)

    @app.get("/api/cameras/{camera_id}/tracks", response_model=TracksOut)
    def camera_tracks(camera_id: str, limit: int = Query(default=100, ge=1, le=500)):
        with app.state.sessions() as session:
            find_camera(session, camera_id)
            status = public_status(session, camera_id)
            frame = (app.state.live.get(camera_id, status.runtime_session, status.generation)
                     if not status.stale and status.state in ("online", "degraded") else None)
            recent = list(session.scalars(select(PersonTrack).where(PersonTrack.camera_id == camera_id)
                          .order_by(PersonTrack.last_seen_at.desc()).limit(limit)))
            return TracksOut(camera_id=camera_id, runtime_session=status.runtime_session,
                             generation=status.generation, timestamp=frame.timestamp if frame else None,
                             bbox_width=frame.bbox_width if frame else None,
                             bbox_height=frame.bbox_height if frame else None,
                             current=[LiveTrackOut(track_id=str(p.track_id) if p.track_id is not None else None,
                                      class_id=p.class_id, confidence=p.confidence,
                                      tracker_confidence=p.tracker_confidence, bbox=p.bbox) for p in frame.persons] if frame else [],
                             recent=[TrackOut.model_validate(row) for row in recent])

    return app


app = create_app()
