"""Camera-scoped CRUD, geometry, durable state, and future ingest boundary."""
from datetime import timedelta
from dataclasses import replace
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError, OperationalError

from backend.app.db import Camera, ParkingSpace, PersonTrack
from backend.app.ingest import utcnow
from backend.app.main import create_app
from backend.app.parking import update_occupancy
from test_backend import service, frame

POLYGON = [{"x": .1, "y": .2}, {"x": .8, "y": .2}, {"x": .8, "y": .9}, {"x": .1, "y": .9}]
BASE = "/api/cameras/first/parking-spaces"


def test_postgres_snapshot_selects_repeatable_read_before_cursor_and_rows(service, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    client, app, _, _, _ = service
    create(client)
    sessions = app.state.sessions
    calls = []

    class SnapshotSession:
        def __init__(self, session):
            self.session = session

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name='postgresql'))

        def connection(self, **kwargs):
            calls.append(('connection', kwargs))

        def scalar(self, query):
            calls.append(('cursor', None))
            return self.session.scalar(query)

        def scalars(self, query):
            calls.append(('rows', None))
            return self.session.scalars(query)

    @contextmanager
    def snapshot_session():
        with sessions() as session:
            yield SnapshotSession(session)

    monkeypatch.setattr(app.state, 'sessions', snapshot_session)
    response = client.get('/api/parking')
    assert response.status_code == 200 and len(response.json()['cameras']) == 1
    assert calls == [('connection', {'execution_options': {'isolation_level': 'REPEATABLE READ'}}),
                     ('cursor', None), ('rows', None)]


def create(client, camera="first", **values):
    return client.post(f"/api/cameras/{camera}/parking-spaces", json={"name": "A-01", "polygon": POLYGON, **values})


def test_crud_and_committed_storage(service):
    client, app, _, _, _ = service
    assert client.get(BASE).json() == []
    response = create(client, name="  A-01  ")
    assert response.status_code == 201
    space = response.json()
    assert space["camera_id"] == "first" and space["name"] == "A-01"
    assert space["polygon"] == POLYGON and space["occupancy"] == "unknown"
    assert space["occupancy_observed_at"] is None and space["revision"] == 1
    url = f"{BASE}/{space['space_id']}"
    assert client.get(url).json() == space
    assert client.get(BASE).json() == [space]
    with app.state.sessions() as session:
        row = session.get(ParkingSpace, space["space_id"])
        assert row.polygon == POLYGON and row.name == "A-01"
    changed = client.patch(url, json={"name": "A-02"})
    assert changed.status_code == 200
    assert changed.json()["name"] == "A-02" and changed.json()["revision"] == 1
    assert client.delete(url).status_code == 204
    assert client.get(url).status_code == 404
    assert client.get(BASE).json() == []
    with app.state.sessions() as session:
        assert session.get(ParkingSpace, space["space_id"]) is None


def test_camera_isolation_and_disabled_camera(service):
    client, _, _, _, _ = service
    first = create(client).json()
    second = create(client, "second").json()
    assert first["space_id"] != second["space_id"]
    assert create(client, "disabled").status_code == 201
    assert len(client.get(BASE).json()) == 1
    other = f"/api/cameras/second/parking-spaces/{first['space_id']}"
    assert client.get(other).status_code == 404
    assert client.patch(other, json={"name": "Wrong camera"}).status_code == 404
    assert client.delete(other).status_code == 404
    assert client.get(f"{BASE}/{first['space_id']}").json()["name"] == "A-01"
    assert client.get("/api/cameras/second/parking-summary").json()["total"] == 1


@pytest.mark.parametrize("polygon", [
    [], POLYGON[:2], POLYGON + [POLYGON[0]],
    [{"x": -.01, "y": 0}, *POLYGON[1:]],
    [{"x": 1.01, "y": 0}, *POLYGON[1:]],
    [{"x": 0, "y": -.01}, *POLYGON[1:]],
    [{"x": 0, "y": 1.01}, *POLYGON[1:]],
    [{"x": "0.1", "y": .2}, *POLYGON[1:]],
    [{"x": True, "y": .2}, *POLYGON[1:]],
    [{"x": None, "y": .2}, *POLYGON[1:]],
    [{"x": 0, "y": 0}, {"x": .5, "y": .5}, {"x": 1, "y": 1}],
    [{"x": 0, "y": 0}, {"x": 1, "y": 1}, {"x": 0, "y": 1}, {"x": 1, "y": 0}],
    # Nonzero-area crossing polygon, edge touches, and adjacent edge backtracking.
    [{"x": 0, "y": 0}, {"x": 1, "y": 1}, {"x": 0, "y": 1}, {"x": .8, "y": 0}],
    [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}, {"x": .5, "y": 0}, {"x": 0, "y": 1}],
    [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": .5, "y": 0}, {"x": 1, "y": 1}, {"x": 0, "y": 1}],
    [{"x": .1, "y": .2, "extra": 1}, *POLYGON[1:]],
    POLYGON * 17,
])
def test_invalid_polygon_rejected_without_writes(service, polygon):
    client, _, _, _, _ = service
    assert create(client, polygon=polygon).status_code == 422
    assert client.get(BASE).json() == []
    space = create(client).json()
    url = f"{BASE}/{space['space_id']}"
    assert client.patch(url, json={"polygon": polygon}).status_code == 422
    assert client.get(url).json() == space


@pytest.mark.parametrize("coordinate", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_coordinates_are_safe(service, coordinate):
    client, _, _, _, _ = service
    body = '{"name":"secret-marker","polygon":[{"x":' + coordinate + ',"y":0},{"x":1,"y":0},{"x":0,"y":1}]}'
    response = client.post(BASE, content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 422 and "secret-marker" not in response.text


@pytest.mark.parametrize("polygon", [
    [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 0, "y": 1}],
    list(reversed(POLYGON)),
    [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": .5, "y": .5}, {"x": 1, "y": 1}, {"x": 0, "y": 1}],
])
def test_boundaries_winding_and_concave_polygon(service, polygon):
    client, _, _, _, _ = service
    assert create(client, polygon=polygon).status_code == 201


@pytest.mark.parametrize("patch", [{}, {"name": None}, {"polygon": None}, {"name": "  "},
    {"name": "a" * 129}, {"occupancy": "occupied"}, {"camera_id": "second"}, {"revision": 2}])
def test_patch_validation_and_readonly_fields(service, patch):
    client, _, _, _, _ = service
    space = create(client).json()
    url = f"{BASE}/{space['space_id']}"
    assert client.patch(url, json=patch).status_code == 422
    assert client.get(url).json() == space


def test_create_readonly_fields_and_unknown_camera(service):
    client, _, _, _, _ = service
    assert create(client, occupancy="empty").status_code == 422
    assert create(client, "missing").status_code == 404
    missing = f"/api/cameras/missing/parking-spaces/{uuid4()}"
    assert client.get("/api/cameras/missing/parking-spaces").status_code == 404
    assert client.get("/api/cameras/missing/parking-summary").status_code == 404
    assert client.get(missing).status_code == 404
    assert client.patch(missing, json={"name": "New"}).status_code == 404
    assert client.delete(missing).status_code == 404
    assert client.get(f"{BASE}/not-a-uuid").status_code == 422


def test_occupancy_counts_revision_and_ordering(service):
    client, app, _, _, _ = service
    empty = {"camera_id": "first", "total": 0, "occupied": 0, "empty": 0, "unknown": 0}
    assert client.get("/api/cameras/first/parking-summary").json() == empty
    spaces = [create(client, name=str(i)).json() for i in range(3)]
    now = utcnow() - timedelta(seconds=3)
    for space, state in zip(spaces, ("occupied", "empty", "unknown")):
        with app.state.sessions.begin() as session:
            assert update_occupancy(session, "first", space["space_id"], 1, state, now)
    assert client.get("/api/cameras/first/parking-summary").json() == {
        "camera_id": "first", "total": 3, "occupied": 1, "empty": 1, "unknown": 1}
    space_id = spaces[0]["space_id"]
    url = f"{BASE}/{space_id}"
    with app.state.sessions.begin() as session:
        assert not update_occupancy(session, "first", space_id, 1, "empty", now)
        assert not update_occupancy(session, "first", space_id, 1, "empty", now - timedelta(seconds=1))
        assert not update_occupancy(session, "second", space_id, 1, "empty", now + timedelta(seconds=1))
        assert not update_occupancy(session, "first", str(uuid4()), 1, "empty", now)
    assert client.patch(url, json={"name": "Renamed"}).json()["occupancy"] == "occupied"
    assert client.patch(url, json={"polygon": POLYGON}).json()["revision"] == 1
    changed_polygon = [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 0, "y": 1}]
    changed = client.patch(url, json={"polygon": changed_polygon}).json()
    assert changed["revision"] == 2 and changed["occupancy"] == "unknown"
    assert changed["occupancy_observed_at"] is None
    with app.state.sessions.begin() as session:
        assert not update_occupancy(session, "first", space_id, 1, "occupied", now + timedelta(seconds=1))
        assert update_occupancy(session, "first", space_id, 2, "empty", now + timedelta(seconds=2))
    assert client.get(url).json()["occupancy"] == "empty"
    assert client.get("/api/cameras/second/parking-summary").json()["total"] == 0


def test_occupancy_validation_and_db_constraint(service):
    client, app, _, _, _ = service
    space_id = create(client).json()["space_id"]
    for state, at in (("invalid", utcnow()), ("occupied", utcnow().replace(tzinfo=None))):
        with app.state.sessions.begin() as session, pytest.raises(ValueError):
            update_occupancy(session, "first", space_id, 1, state, at)
    with pytest.raises(IntegrityError), app.state.sessions.begin() as session:
        session.get(ParkingSpace, space_id).occupancy = "invalid"
    assert client.get(f"{BASE}/{space_id}").json()["occupancy"] == "unknown"


def test_restart_and_camera_sync_preserve_spaces(service):
    client, app, _, path, engine = service
    space = create(client).json()
    with app.state.sessions.begin() as session:
        update_occupancy(session, "first", space["space_id"], 1, "occupied", utcnow())
    path.write_text("cameras: [{id: second, floor: 3, name: Second, rtsp_env: PRIVATE_SECOND}]")
    # New process-local app/session factory using the same durable DB file.
    restarted = create_app(app.state.config, engine, start_poller=False)
    with TestClient(restarted) as other:
        row = other.get(f"{BASE}/{space['space_id']}").json()
        assert row["polygon"] == POLYGON and row["occupancy"] == "unknown"
        assert other.get("/api/cameras/first").json()["enabled"] is False


def test_additive_initialization_preserves_legacy_tables(service):
    _, app, upstream, _, engine = service
    upstream["metadata"]["frames"] = [frame()]
    app.state.poller.poll_once()
    ParkingSpace.__table__.drop(engine)  # Simulate pre-feature schema in isolated fixture DB.
    assert "parking_spaces" not in inspect(engine).get_table_names()
    for _ in range(2):
        app.state.poller.initialize()
    with app.state.sessions() as session:
        assert len(list(session.scalars(select(Camera)))) == 3
        assert len(list(session.scalars(select(PersonTrack)))) == 1
    assert "parking_spaces" in inspect(engine).get_table_names()


def test_crud_cors_with_json_preflight(service):
    _, existing, _, _, engine = service
    app = create_app(replace(existing.state.config, cors_origins=("http://localhost:3000",)),
                     engine, start_poller=False)
    with TestClient(app) as client:
        for method in ("POST", "PATCH", "DELETE"):
            headers = {"Origin": "http://localhost:3000", "Access-Control-Request-Method": method,
                       "Access-Control-Request-Headers": "Content-Type"}
            assert client.options(BASE, headers=headers).status_code == 200
            headers["Origin"] = "https://untrusted.example"
            assert client.options(BASE, headers=headers).status_code == 400
        response = client.post(BASE, json={"name": "A", "polygon": POLYGON},
                               headers={"Origin": "http://localhost:3000"})
        assert response.status_code == 201
        assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
        assert "access-control-allow-credentials" not in response.headers


def test_parking_db_errors_are_safe(service, monkeypatch):
    client, app, _, _, _ = service
    space = create(client).json()
    class FailedSessions:
        def __call__(self):
            raise OperationalError("SELECT", {}, Exception("secret-marker"))
        begin = __call__
    monkeypatch.setattr(app.state, "sessions", FailedSessions())
    url = f"{BASE}/{space['space_id']}"
    responses = [client.get(BASE), client.get(url), client.get("/api/cameras/first/parking-summary"),
                 create(client), client.patch(url, json={"name": "A"}), client.delete(url)]
    assert all(response.status_code == 503 and "secret-marker" not in response.text for response in responses)
