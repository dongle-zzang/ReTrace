from contextlib import asynccontextmanager
import threading

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from .db import Camera, CameraStatus, PersonTrack, make_engine, sessions_for
from .ingest import LiveStore, Poller, status_out
from .schemas import CameraOut, HealthOut, LiveTrackOut, StatusOut, StatusSummary, TrackOut, TracksOut
from .settings import Settings


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
        thread = threading.Thread(target=poller.run, name="preview-poller", daemon=True)
        if start_poller:
            thread.start()
        try:
            yield
        finally:
            poller.stop.set()
            if start_poller:
                thread.join(timeout=10)
            if not thread.is_alive():
                poller.client.close()
            if engine is None:
                db.dispose()

    app = FastAPI(title="ReTrace Backend", lifespan=lifespan)
    origins = settings.cors_origins if settings is not None else Settings.cors_origins_from_env()
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(origins),
                           allow_credentials=False, allow_methods=["GET"])

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
        mode = app.state.config.preview_mode
        return CameraOut(camera_id=camera.camera_id, floor=camera.floor, name=camera.name,
                         enabled=camera.enabled, source_id=camera.source_id,
                         preview_path=(("/ws" if mode == "webrtc" else f"/mjpeg/source{camera.source_id}")
                                       if camera.enabled else None),
                         preview_format=mode, signaling_path="/ws" if mode == "webrtc" else None,
                         status=public_status(session, camera.camera_id))

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
