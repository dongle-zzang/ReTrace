"""Real HTTP/API/storage/ZIP flow using generated JPEGs and an isolated SQLite DB."""
from datetime import datetime, timezone
from io import BytesIO
import json
import time
from types import SimpleNamespace
from uuid import uuid4
import zipfile

import cv2
import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.app.db import make_engine
from backend.app.dataset import split_sessions
from backend.app.dataset_models import DatasetImage


@pytest.fixture
def dataset(tmp_path):
    config = tmp_path / 'cameras.yaml'
    config.write_text('cameras: [{id: first, floor: B1, name: First, rtsp_env: PRIVATE_FIRST}, {id: disabled, floor: 1, name: Disabled, enabled: false, rtsp_env: PRIVATE_OFF}]')
    settings = Settings(f'sqlite:///{tmp_path / "test.db"}', config, dataset_dir=tmp_path / 'storage')
    state = {'counter': 0, 'clean': True, 'failed': False, 'duplicate': False, 'timestamp': None}
    def respond(request):
        if state['failed']:
            return httpx.Response(503)
        if not state['duplicate']:
            state['counter'] += 1
        image = np.full((100, 200, 3), state['counter'] % 255, np.uint8)
        _, jpeg = cv2.imencode('.jpg', image)
        return httpx.Response(200, content=jpeg.tobytes(), headers={'X-Captured-At': state['timestamp'] if state['timestamp'] is not None else datetime.now(timezone.utc).isoformat(), 'Content-Type': 'image/jpeg', 'X-Capture-Clean': 'pre-osd-v1' if state['clean'] else 'osd'})
    engine = make_engine(settings.database_url)
    transport = httpx.MockTransport(respond)
    app = create_app(settings, engine, transport, start_poller=False)
    with TestClient(app) as client:
        yield client, app, state, settings, transport, engine
    engine.dispose()


def capture(client):
    response = client.post('/api/datasets/capture', json={'camera_id': 'first'})
    assert response.status_code == 201, response.text
    return response.json()


def box(**changes):
    return {'id': str(uuid4()), 'class_id': 0, 'x': 10, 'y': 20, 'width': 40, 'height': 50, 'source': 'manual', **changes}


def save(client, image, boxes, status='reviewed', **more):
    return client.put(f'/api/datasets/images/{image["id"]}/annotations', json={'revision': image['revision'], 'boxes': boxes, 'status': status, **more})


def await_export(client):
    result = client.post('/api/datasets/exports', json={'seed': 42})
    assert result.status_code == 202, result.text
    for _ in range(200):
        job = next(item for item in client.get('/api/datasets/exports').json() if item['id'] == result.json()['id'])
        if job['status'] != 'building':
            return job
        time.sleep(.02)
    pytest.fail('Export worker did not finish')


def test_clean_capture_files_metadata_filters_duplicates_and_delete(dataset):
    client, app, state, *_ = dataset
    state['clean'] = False
    assert client.post('/api/datasets/capture', json={'camera_id': 'first'}).status_code == 503
    assert client.post('/api/datasets/capture', json={'camera_id': 'missing'}).status_code == 404
    assert client.post('/api/datasets/capture', json={'camera_id': 'disabled'}).status_code == 409
    state['clean'] = True
    first, second = capture(client), capture(client)
    assert first['capture_session'] == second['capture_session']
    assert first['width'] == 200 and first['height'] == 100
    assert client.get(first['image_url']).headers['content-type'] == 'image/jpeg'
    assert client.get(first['thumbnail_url']).status_code == 200
    state['duplicate'] = True
    assert client.post('/api/datasets/capture', json={'camera_id': 'first'}).status_code == 409
    assert len(list(app.state.datasets.path('images').glob('*.jpg'))) == 4
    listing = client.get('/api/datasets/images?camera_id=first&page_size=1').json()
    assert listing['total'] == 2 and len(listing['items']) == 1
    assert client.get('/api/datasets/images?status=reviewed').json()['total'] == 0
    detail = client.get('/api/datasets/images/' + first['id']).json()
    assert detail['next_id'] == second['id']
    assert client.delete('/api/datasets/images/' + first['id']).status_code == 204
    assert client.get(first['image_url']).status_code == 404
    assert len(list(app.state.datasets.path('images').glob('*.jpg'))) == 2
    state['failed'] = True
    assert client.post('/api/datasets/capture', json={'camera_id': 'first'}).status_code == 503


@pytest.mark.parametrize('bad', [dict(class_id=3), dict(x=-1), dict(width=0), dict(x=180,width=40), dict(source='auto')])
def test_box_validation(dataset, bad):
    client = dataset[0]
    assert save(client, capture(client), [box(**bad)]).status_code == 422


def test_revision_empty_review_and_persistence(dataset):
    client, app, _, settings, transport, engine = dataset
    image = capture(client)
    assert save(client, image, []).status_code == 422
    draft = save(client, image, [], status='in_progress').json()
    assert draft['revision'] == 2
    assert save(client, image, [box()]).status_code == 409
    reviewed = save(client, draft, [], reviewed_empty=True).json()
    assert reviewed['status'] == 'reviewed' and reviewed['reviewed_empty']
    duplicate = box()
    assert save(client, reviewed, [duplicate, duplicate]).status_code == 422
    # A second application instance uses the persisted DB and files, no seeded UI state.
    with TestClient(create_app(settings, engine, transport, start_poller=False)) as restarted:
        detail = restarted.get('/api/datasets/images/' + image['id']).json()
        assert detail['status'] == 'reviewed' and detail['revision'] == 3
        assert restarted.get(image['image_url']).status_code == 200


def test_yolo_zip_only_reviewed_and_normalized_matching_labels(dataset):
    client = dataset[0]
    pending = capture(client)
    labeled = capture(client)
    assert save(client, labeled, [box()]).status_code == 200
    empty = capture(client)
    assert save(client, empty, [], reviewed_empty=True).status_code == 200
    job = await_export(client)
    assert job['status'] == 'ready', job
    assert sum(job['manifest']['counts'].values()) == 2
    assert job['manifest']['counts']['val'] == job['manifest']['counts']['test'] == 0
    assert len(job['manifest']['warnings']) == 2
    download = client.get(job['download_url'])
    with zipfile.ZipFile(BytesIO(download.content)) as archive:
        names = archive.namelist()
        assert f'images/train/{pending["id"]}.jpg' not in names
        label = archive.read(f'labels/train/{labeled["id"]}.txt').decode().strip()
        assert label == '0 0.15000000 0.45000000 0.20000000 0.50000000'
        assert archive.read(f'labels/train/{empty["id"]}.txt') == b''
        assert '0: car' in archive.read('data.yaml').decode()
        assert 'path:' not in archive.read('data.yaml').decode()  # Resolve relative to the extracted YAML directory.
        manifest = json.loads(archive.read('manifest.json'))
        assert len(manifest['images']) == 2
        for item in manifest['images']:
            assert f'images/{item["split"]}/{item["id"]}.jpg' in names
            assert f'labels/{item["split"]}/{item["id"]}.txt' in names


def test_export_rejects_missing_or_modified_images_and_pending_only(dataset):
    client, app, *_ = dataset
    assert client.post('/api/datasets/exports', json={}).status_code == 422
    image = capture(client)
    assert save(client, image, [box()]).status_code == 200
    with app.state.sessions() as session:
        row = session.get(DatasetImage, image['id'])
        app.state.datasets.path(row.file_path).write_bytes(b'changed')
    job = await_export(client)
    assert job['status'] == 'failed' and job['download_url'] is None
    assert client.get('/api/datasets/exports/' + job['id'] + '/download').status_code == 409
    assert not list(app.state.datasets.path('exports').glob('*.zip'))


def test_session_split_is_deterministic_and_never_splits_a_session():
    images = [SimpleNamespace(capture_session=f'session-{i // 5}') for i in range(50)]
    first = split_sessions(images, 42)
    assert first == split_sessions(images, 42)
    assert set(first.values()) == {'train','val','test'}
    assert len(first) == 10
    assert list(first.values()).count('train') == 8


def test_capture_busy_returns_conflict(dataset):
    client, app, *_ = dataset
    app.state.datasets.capture_lock.acquire()
    try:
        assert client.post('/api/datasets/capture', json={'camera_id':'first'}).status_code == 409
    finally:
        app.state.datasets.capture_lock.release()


def test_capture_timestamp_and_storage_failure_release_request_lock(dataset, monkeypatch):
    client, app, state, *_ = dataset
    state['timestamp'] = 'invalid'
    assert client.post('/api/datasets/capture', json={'camera_id': 'first'}).status_code == 503
    assert not app.state.datasets.capture_lock.locked()
    state['timestamp'] = '2026-10-12T03:04:05+00:00'
    image = capture(client)
    assert image['captured_at'] == state['timestamp']
    from pathlib import Path
    original_write = Path.write_bytes
    def fail_write(path, data):
        if path.parent.name == 'images': raise OSError('fixture')
        return original_write(path, data)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'write_bytes', fail_write)
        assert client.post('/api/datasets/capture', json={'camera_id': 'first'}).status_code == 503
    assert not app.state.datasets.capture_lock.locked()
    assert client.get('/api/datasets/images').json()['total'] == 1
    assert len(list(app.state.datasets.path('images').glob('*.jpg'))) == 2
    assert capture(client)['id'] != image['id']


def test_delete_rolls_back_files_and_db_on_transaction_failure(dataset, monkeypatch):
    client, app, *_ = dataset
    image = capture(client)
    from sqlalchemy.orm import Session
    original_flush = Session.flush
    def fail_delete(session, *args, **kwargs):
        if any(isinstance(row, DatasetImage) for row in session.deleted):
            from sqlalchemy.exc import SQLAlchemyError
            raise SQLAlchemyError('fixture')
        return original_flush(session, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Session, 'flush', fail_delete)
        assert client.delete('/api/datasets/images/' + image['id']).status_code == 503
    assert client.get('/api/datasets/images/' + image['id']).status_code == 200
    assert client.get(image['image_url']).status_code == 200
    assert not list(app.state.datasets.path('images').glob('*.deleting'))


def test_car_only_manifest_and_legacy_export_fails_without_relabeling(dataset):
    client = dataset[0]
    assert client.get('/api/datasets/classes').json()['classes'] == {'0': 'car'}
    image = capture(client)
    assert save(client, image, [box(class_id=1)]).status_code == 200
    job = await_export(client)
    assert job['status'] == 'failed' and 'legacy' in job['error']
    assert client.get('/api/datasets/images/' + image['id']).json()['boxes'][0]['class_id'] == 1


def test_delete_export_removes_only_zip_and_record(dataset):
    client, app, *_ = dataset
    image = capture(client)
    assert save(client, image, [box()]).status_code == 200
    job = await_export(client)
    path = app.state.datasets.path(f'exports/{job["id"]}.zip')
    with zipfile.ZipFile(path) as archive:
        assert archive.read('data.yaml').decode().endswith('names:\n  0: car\n')
        assert json.loads(archive.read('manifest.json'))['classes'] == {'0': 'car'}
    assert client.delete('/api/datasets/exports/' + job['id']).status_code == 204
    assert not path.exists()
    assert all(row['id'] != job['id'] for row in client.get('/api/datasets/exports').json())
    assert client.get(job['download_url']).status_code == 404
    assert client.get('/api/datasets/images/' + image['id']).json()['box_count'] == 1
    assert client.get(image['image_url']).status_code == 200
    assert client.delete('/api/datasets/exports/' + job['id']).status_code == 404


def test_export_delete_blocks_building_and_allows_failed_records(dataset):
    from backend.app.dataset_models import DatasetExport
    client, app, *_ = dataset
    job_id = str(uuid4())
    with app.state.sessions.begin() as session:
        session.add(DatasetExport(id=job_id, seed=42, status='building'))
    assert client.delete('/api/datasets/exports/' + job_id).status_code == 409
    with app.state.sessions.begin() as session:
        session.get(DatasetExport, job_id).status = 'failed'
    assert client.delete('/api/datasets/exports/' + job_id).status_code == 204


def test_export_delete_rollback_and_restart_tombstone_recovery(dataset, monkeypatch):
    from backend.app.dataset_models import DatasetExport
    from sqlalchemy.orm import Session
    from sqlalchemy.exc import SQLAlchemyError
    client, app, *_ = dataset
    image = capture(client)
    assert save(client, image, [box()]).status_code == 200
    job = await_export(client)
    path = app.state.datasets.path(f'exports/{job["id"]}.zip')
    original_flush = Session.flush
    def fail_delete(session, *args, **kwargs):
        if any(isinstance(row, DatasetExport) for row in session.deleted):
            raise SQLAlchemyError('fixture')
        return original_flush(session, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Session, 'flush', fail_delete)
        assert client.delete('/api/datasets/exports/' + job['id']).status_code == 503
    assert path.is_file() and client.get(job['download_url']).status_code == 200
    tombstone = path.with_name(path.name + '.deleting')
    path.replace(tombstone)
    orphan = app.state.datasets.path(f'exports/{uuid4()}.zip.deleting')
    orphan.write_bytes(b'fixture')
    app.state.datasets.initialize()
    assert path.is_file() and not tombstone.exists() and not orphan.exists()
