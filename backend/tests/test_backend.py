"""CPU-only API, polling, persistence, and confidentiality checks."""
from datetime import timedelta
from dataclasses import replace
import json
from pathlib import Path
import threading

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, inspect, select
from sqlalchemy.exc import OperationalError

from backend.app.db import Camera, CameraStatus, PersonTrack, make_engine
from backend.app.ingest import normalize_status, sync_cameras, utcnow
from backend.app.main import create_app
from backend.app.settings import Settings
from camera_config import CameraConfigError, load_public_cameras

SESSION = "a" * 32


@pytest.fixture
def service(tmp_path):
    path = tmp_path / "cameras.yaml"
    path.write_text('''cameras:
  - {id: first, floor: B1, name: First, rtsp_env: PRIVATE_FIRST, enabled: true}
  - {id: disabled, floor: 2, name: Disabled, rtsp_env: PRIVATE_DISABLED, enabled: false}
  - {id: second, floor: 3, name: Second, rtsp_env: PRIVATE_SECOND, enabled: true}
''')
    settings = Settings(f"sqlite:///{tmp_path / 'test.db'}", path)
    engine = make_engine(settings.database_url)
    upstream = {"error": False, "metadata_error": False,
                "streams": [stream("first", 0), stream("second", 1)],
                "metadata": {"runtime_session": SESSION, "frames": []}}

    def respond(request):
        if upstream["error"]:
            raise httpx.ConnectError("Synthetic upstream failure", request=request)
        if request.url.path == "/streams.json":
            return httpx.Response(200, json=upstream["streams"])
        if upstream["metadata_error"]:
            return httpx.Response(404)
        return httpx.Response(200, json=upstream["metadata"])

    app = create_app(settings, engine, httpx.MockTransport(respond), start_poller=False)
    with TestClient(app) as client:
        yield client, app, upstream, path, engine
    engine.dispose()


def stream(camera_id, source_id, state="online", generation=1):
    return {"camera_id": camera_id, "id": source_id, "runtime_session": SESSION,
            "url": f"/mjpeg/source{source_id}", "runtime": {
                "state": state, "fps": 12.5, "last_frame_at": utcnow().isoformat(),
                "generation": generation, "reconnect_count": 0, "last_error": None}}


def frame(track_id=42, generation=1, confidence=.9, at=None):
    return {"camera_id": "first", "source_id": 0, "generation": generation,
            "timestamp": (at or utcnow()).isoformat(), "frame_number": 10,
            "bbox_width": 1920, "bbox_height": 1080, "persons": [
                {"track_id": track_id, "class_id": 0, "class": "person", "confidence": confidence,
                 "bbox": {"x": 10, "y": 20, "width": 30, "height": 50}}]}


def test_health_list_detail_status_summary_and_404(service):
    client, app, upstream, _, _ = service
    assert client.get("/api/health").json() == {"status": "ok", "database": "ok", "preview": "unknown"}
    app.state.poller.poll_once()
    cameras = client.get("/api/cameras").json()
    assert len(cameras) == 3
    first = client.get("/api/cameras/first").json()
    assert first["floor"] == "B1"
    assert first["preview_path"] == "/mjpeg/source0"
    assert first["metadata_path"] == "/ws"
    assert "preview_format" not in first and "signaling_path" not in first
    assert first["status"]["state"] == "online"
    assert client.get("/api/cameras/second").json()["source_id"] == 1
    disabled = client.get("/api/cameras/disabled").json()
    assert disabled["source_id"] is None and disabled["preview_path"] is None
    assert disabled["status"]["last_error"] == "disabled"
    assert client.get("/api/cameras/first/status").json()["fps"] == 12.5
    assert client.get("/api/status").json()["counts"]["online"] == 2
    for suffix in ("", "/status", "/tracks"):
        assert client.get("/api/cameras/unknown" + suffix).status_code == 404
    assert client.get("/mjpeg/source0").status_code == 404


def test_cors_defaults_to_no_cross_origin_access(service):
    client, _, _, _, _ = service
    response = client.get("/api/health", headers={"Origin": "http://localhost:3000"})
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_cors_allows_only_configured_origins_and_read_requests(service):
    _, existing, _, _, engine = service
    settings = replace(existing.state.config, cors_origins=("http://localhost:3000",))
    app = create_app(settings, engine, start_poller=False)
    with TestClient(app) as client:
        response = client.get("/api/health", headers={"Origin": "http://localhost:3000"})
        assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
        assert "access-control-allow-credentials" not in response.headers
        assert "Origin" in response.headers["vary"]
        for origin in ("http://127.0.0.1:3000", "http://localhost:3001", "https://untrusted.example"):
            response = client.get("/api/health", headers={"Origin": origin})
            assert "access-control-allow-origin" not in response.headers
        headers = {"Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET"}
        assert client.options("/api/health", headers=headers).status_code == 200
        headers["Access-Control-Request-Method"] = "POST"
        assert client.options("/api/health", headers=headers).status_code == 400


def test_cors_environment_is_opt_in_and_rejects_wildcards(monkeypatch):
    monkeypatch.delenv("BACKEND_CORS_ORIGINS", raising=False)
    assert Settings.cors_origins_from_env() == ()
    monkeypatch.setenv("BACKEND_CORS_ORIGINS", " http://localhost:3000, ,http://127.0.0.1:3000 ")
    assert Settings.cors_origins_from_env() == ("http://localhost:3000", "http://127.0.0.1:3000")
    monkeypatch.setenv("POSTGRES_PASSWORD", "synthetic-test-password")
    assert Settings.from_env().cors_origins == Settings.cors_origins_from_env()
    for value in ("*", "http://*.example"):
        monkeypatch.setenv("BACKEND_CORS_ORIGINS", value)
        with pytest.raises(RuntimeError, match="explicit origins"):
            Settings.cors_origins_from_env()
        with pytest.raises(RuntimeError, match="explicit origins"):
            create_app()


def test_yaml_public_parser_does_not_resolve_secrets(service):
    _, _, _, path, _ = service
    public = load_public_cameras(path)
    assert len(public) == 3
    assert [c["source_id"] for c in public] == [0, None, 1]
    assert "rtsp" not in json.dumps(public).lower()


@pytest.mark.parametrize("document", ["cameras: [", "cameras: bad", "cameras: [{id: first}]",
    "cameras: [{id: a, floor: 1, name: First, rtsp_env: A, enabled: 'false'}]",
    "cameras: [{id: a, floor: 1, name: First, rtsp_env: A, rtsp_url: secret-marker}]",
    "cameras: [{id: a, floor: 1, name: First, rtsp_env: A}, {id: a}]"])
def test_yaml_errors_do_not_echo_values(tmp_path, document):
    path = tmp_path / "cameras.yaml"
    path.write_text(document)
    with pytest.raises(CameraConfigError) as caught:
        load_public_cameras(path)
    assert "secret-marker" not in str(caught.value)


def test_sync_updates_disables_removed_and_is_idempotent(service):
    client, app, _, path, _ = service
    path.write_text("cameras: [{id: first, floor: 5, name: Changed, rtsp_env: A, enabled: false}, "
                    "{id: new, floor: 4, name: New, rtsp_env: B}]")
    for _ in range(3):
        with app.state.sessions.begin() as session:
            sync_cameras(session, path)
    with app.state.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Camera)) == 4
        assert session.scalar(select(func.count()).select_from(CameraStatus)) == 4
    assert client.get("/api/cameras/first").json()["name"] == "Changed"
    assert not client.get("/api/cameras/second").json()["enabled"]
    assert client.get("/api/cameras/new").json()["source_id"] == 0


@pytest.mark.parametrize("state", ["connecting", "online", "degraded", "offline", "reconnecting"])
def test_all_runtime_states(service, state):
    client, app, upstream, _, _ = service
    upstream["streams"][0]["runtime"]["state"] = state
    app.state.poller.poll_once()
    result = client.get("/api/cameras/first/status").json()
    assert result["state"] == state
    assert result["fps"] == (0 if state == "offline" else 12.5)


def test_status_normalization():
    row = normalize_status("first", {"state": "unexpected", "fps": float("nan"),
        "last_error": "secret-marker", "last_frame_at": "bad", "reconnect_count": -4,
        "generation": True}, utcnow())
    assert row["state"] == "offline" and row["fps"] == 0
    assert row["last_error"] is None and row["last_frame_at"] is None
    assert row["reconnect_count"] == row["generation"] == 0


def test_unreachable_preview_clears_current_keeps_summary_and_recovers(service):
    client, app, upstream, _, _ = service
    upstream["metadata"]["frames"] = [frame()]
    app.state.poller.poll_once()
    assert len(client.get("/api/cameras/first/tracks").json()["current"]) == 1
    upstream["error"] = True
    app.state.poller.poll_once()
    status = client.get("/api/cameras/first/status").json()
    assert status["state"] == "offline" and status["last_error"] == "preview_unreachable"
    tracks = client.get("/api/cameras/first/tracks").json()
    assert tracks["current"] == [] and len(tracks["recent"]) == 1
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/health").json()["preview"] == "unreachable"
    upstream["error"] = False
    app.state.poller.poll_once()
    assert client.get("/api/cameras/first/status").json()["state"] == "online"


def test_missing_camera_and_bad_snapshot(service):
    client, app, upstream, _, _ = service
    upstream["streams"] = [stream("second", 1)]
    app.state.poller.poll_once()
    assert client.get("/api/cameras/first/status").json()["last_error"] == "camera_missing"
    upstream["streams"] = {"invalid": True}
    app.state.poller.poll_once()
    assert client.get("/api/cameras/second/status").json()["state"] == "offline"


def test_status_row_count_does_not_grow(service):
    _, app, upstream, _, engine = service
    for state in ["online"] * 5 + ["offline"] * 5 + ["online"] * 5:
        upstream["streams"][0]["runtime"]["state"] = state
        app.state.poller.poll_once()
    with app.state.sessions() as session:
        assert session.scalar(select(func.count()).select_from(CameraStatus)) == 3
    assert set(inspect(engine).get_table_names()) == {"cameras", "camera_status", "person_tracks"}


def test_track_summary_upsert_not_frame_insert_and_uint64(service):
    client, app, upstream, _, engine = service
    track_id = (1 << 63) + 77
    started = utcnow() - timedelta(seconds=1)
    for i in range(10):
        upstream["metadata"]["frames"] = [frame(track_id, confidence=.5 + i / 20,
                                                at=started + timedelta(milliseconds=i))]
        app.state.poller.poll_once()
    result = client.get("/api/cameras/first/tracks").json()
    assert len(result["recent"]) == 1
    assert result["recent"][0]["track_id"] == str(track_id)
    assert result["recent"][0]["max_confidence"] == .95
    assert result["recent"][0]["first_seen_at"] != result["recent"][0]["last_seen_at"]
    assert result["current"][0]["bbox"]["x"] == 10
    assert result["current"][0]["class"] == "person"
    assert not any("bbox" in c["name"] for c in inspect(engine).get_columns("person_tracks"))
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", capture)
    app.state.poller.poll_once()  # Same frame yields no track INSERT/UPDATE.
    event.remove(engine, "before_cursor_execute", capture)
    assert not any(s.startswith(("INSERT INTO person_tracks", "UPDATE person_tracks")) for s in statements)


def test_generation_and_process_restart_split_track_identity(service):
    client, app, upstream, _, _ = service
    upstream["metadata"]["frames"] = [frame()]
    app.state.poller.poll_once()
    upstream["streams"][0]["runtime"]["generation"] = 2
    upstream["metadata"]["frames"] = [frame(generation=2)]
    app.state.poller.poll_once()
    upstream["streams"][0]["runtime_session"] = "b" * 32
    upstream["metadata"]["runtime_session"] = "b" * 32
    app.state.poller.poll_once()
    assert len(client.get("/api/cameras/first/tracks").json()["recent"]) == 3


def test_same_numeric_track_id_is_independent_per_camera(service):
    client, app, upstream, _, engine = service
    first = frame(track_id=42)
    second = {**frame(track_id=42), "camera_id": "second", "source_id": 1}
    upstream["metadata"]["frames"] = [first, second]
    app.state.poller.poll_once()
    for camera_id in ("first", "second"):
        tracks = client.get(f"/api/cameras/{camera_id}/tracks").json()
        assert tracks["current"][0]["track_id"] == "42"
        assert tracks["recent"][0]["camera_id"] == camera_id
    with app.state.sessions() as session:
        assert session.scalar(select(func.count()).select_from(PersonTrack)) == 2


@pytest.mark.parametrize("bad_frame", ["expired", "generation", "session", "source", "no_session"])
def test_stale_or_wrong_identity_frames_are_rejected(service, bad_frame):
    client, app, upstream, _, _ = service
    item = frame()
    if bad_frame == "expired": item["timestamp"] = (utcnow() - timedelta(seconds=30)).isoformat()
    if bad_frame == "generation": item["generation"] = 90
    if bad_frame == "source": item["source_id"] = 1
    if bad_frame == "session": upstream["metadata"]["runtime_session"] = "b" * 32
    if bad_frame == "no_session": upstream["streams"][0].pop("runtime_session")
    upstream["metadata"]["frames"] = [item]
    app.state.poller.poll_once()
    result = client.get("/api/cameras/first/tracks").json()
    assert result["current"] == result["recent"] == []


def test_empty_untracked_and_missing_metadata(service):
    client, app, upstream, _, _ = service
    upstream["metadata"]["frames"] = [frame(track_id=None)]
    app.state.poller.poll_once()
    tracks = client.get("/api/cameras/first/tracks").json()
    assert len(tracks["current"]) == 1 and tracks["recent"] == []
    upstream["metadata"]["frames"][0]["persons"] = []
    app.state.poller.poll_once()
    assert client.get("/api/cameras/first/tracks").json()["current"] == []
    upstream["metadata_error"] = True
    app.state.poller.poll_once()
    assert client.get("/api/cameras/first/status").json()["state"] == "online"


def test_status_and_live_expire_without_polling(service):
    client, app, upstream, _, _ = service
    upstream["metadata"]["frames"] = [frame()]
    app.state.poller.poll_once()
    with app.state.sessions.begin() as session:
        session.get(CameraStatus, "first").observed_at = utcnow() - timedelta(seconds=20)
    assert client.get("/api/cameras/first/status").json()["stale"] is True
    assert client.get("/api/cameras/first/status").json()["state"] == "offline"
    assert client.get("/api/cameras/first/tracks").json()["current"] == []
    assert app.state.live.get("first", SESSION, 1, utcnow() + timedelta(seconds=20)) is None


def test_retention_cleanup(service):
    _, app, upstream, _, _ = service
    upstream["metadata"]["frames"] = [frame()]
    app.state.poller.poll_once()
    with app.state.sessions.begin() as session:
        row = session.scalar(select(PersonTrack))
        row.last_seen_at = utcnow() - timedelta(days=8)
    upstream["metadata"]["frames"] = []
    app.state.poller.last_cleanup = 0
    app.state.poller.poll_once()
    with app.state.sessions() as session:
        assert session.scalar(select(func.count()).select_from(PersonTrack)) == 0


def test_api_and_db_whitelist_sensitive_upstream_fields(service):
    client, app, upstream, _, engine = service
    upstream["streams"][0].update(rtsp_url="secret-marker", rtsp_env="PRIVATE_FIRST", url="secret-marker")
    upstream["streams"][0]["runtime"]["last_error"] = "secret-marker"
    app.state.poller.poll_once()
    for route in ["/api/health", "/api/cameras", "/api/cameras/first", "/api/cameras/first/status",
                  "/api/cameras/first/tracks", "/api/status", "/api/cameras/first/tracks?limit=secret-marker"]:
        body = client.get(route).text
        assert "secret-marker" not in body and "PRIVATE_FIRST" not in body
        assert "rtsp_url" not in body and "rtsp_env" not in body and "password" not in body
    for table in inspect(engine).get_table_names():
        assert not {"rtsp_url", "rtsp_env", "password", "bbox"} & {c["name"] for c in inspect(engine).get_columns(table)}


def test_database_error_is_safe_and_poller_retries(service, monkeypatch):
    client, app, _, _, _ = service
    real_sessions = app.state.sessions
    def fail(*args, **kwargs):
        raise OperationalError("SELECT", {}, Exception("secret-marker"))
    monkeypatch.setattr(app.state, "sessions", fail)
    assert client.get("/api/health").status_code == 503
    response = client.get("/api/cameras")
    assert response.status_code == 503 and "secret-marker" not in response.text
    monkeypatch.setattr(app.state, "sessions", real_sessions)
    calls = []
    def retry():
        calls.append(1)
        if len(calls) == 1:
            raise OperationalError("SELECT", {}, Exception("secret-marker"))
        app.state.poller.stop.set()
    monkeypatch.setattr(app.state.poller, "poll_once", retry)
    app.state.poller.run()
    assert len(calls) == 2


def test_poller_can_initialize_later_after_db_failure(service, monkeypatch):
    _, app, _, _, _ = service
    poller = app.state.poller
    poller.ready = False
    original = poller.initialize
    calls = []
    def initialize():
        calls.append(1)
        if len(calls) == 1:
            raise OperationalError("SELECT", {}, Exception("secret-marker"))
        original()
        poller.stop.set()
    monkeypatch.setattr(poller, "initialize", initialize)
    poller.run()
    assert len(calls) == 2 and poller.ready
