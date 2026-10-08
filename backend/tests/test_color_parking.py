"""Color-only synthetic images, durable config, API and fresh-frame lifecycle."""
import time

import cv2
import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from backend.app.color_analysis import StableState, aggregate, color_score, histogram
from backend.app.main import create_app
from test_backend import service

POLYGON = [{'x': .1, 'y': .1}, {'x': .9, 'y': .1}, {'x': .9, 'y': .9}, {'x': .1, 'y': .9}]


def create(client, camera='first', space=None, **options):
    if space is None:
        space = client.post('/api/parking/spaces', json={'label': ' A-01 '}).json()['id']
    response = client.post('/api/parking/zones', json={
        'cameraId': camera, 'parkingSpaceId': space, 'polygon': POLYGON, **options})
    assert response.status_code == 201, response.text
    return response.json()


def test_mask_diversity_reference_and_small_roi():
    plain = np.full((100, 100, 3), 120, np.uint8)
    hist = histogram(plain, POLYGON)
    assert color_score(hist) == 0
    assert color_score(hist, hist) == 0
    outside = plain.copy()
    outside[:8] = [0, 0, 255]
    assert np.array_equal(hist, histogram(outside, POLYGON))
    different = np.full_like(plain, [0, 0, 255])
    assert color_score(histogram(different, POLYGON), hist) > .95
    colorful = np.random.default_rng(3).integers(0, 256, plain.shape, dtype=np.uint8)
    assert color_score(histogram(colorful, POLYGON)) > .5
    tiny = [{'x': 0., 'y': 0.}, {'x': .01, 'y': 0.}, {'x': 0., 'y': .01}]
    assert histogram(plain, tiny) is None


def test_stabilization_hysteresis_and_unknown_reset():
    state = StableState()
    assert [state.update(.8, .3, .05, 3) for _ in range(3)] == ['UNKNOWN', 'UNKNOWN', 'OCCUPIED']
    assert state.update(.3, .3, .05, 3) == 'OCCUPIED'
    assert state.update(.0, .3, .05, 3) == 'OCCUPIED'
    assert state.update(.8, .3, .05, 3) == 'OCCUPIED'
    assert [state.update(.0, .3, .05, 3) for _ in range(3)][-1] == 'EMPTY'
    assert state.update(None, .3, .05, 3) == 'UNKNOWN'


@pytest.mark.parametrize('states, expected, conflict', [
    ([], 'UNKNOWN', False), (['EMPTY', 'UNKNOWN'], 'UNKNOWN', False),
    (['EMPTY', 'EMPTY'], 'EMPTY', False), (['OCCUPIED', 'UNKNOWN'], 'OCCUPIED', False),
    (['OCCUPIED', 'EMPTY'], 'OCCUPIED', True)])
def test_aggregation(states, expected, conflict):
    assert aggregate([{'status': s} for s in states]) == (expected, conflict)


def test_crud_duplicates_validation_and_multi_camera(service):
    client, app, _, _, _ = service
    first = create(client)
    assert client.post('/api/parking/spaces', json={'label': 'A-01'}).status_code == 409
    assert client.get('/api/parking/spaces').json() == [{'id': first['parkingSpaceId'], 'label': 'A-01'}]
    second = create(client, 'second', first['parkingSpaceId'])
    assert client.get('/api/parking/zones?cameraId=first').json() == [first]
    result = client.get('/api/parking/status').json()['spaces'][0]
    assert result['status'] == 'UNKNOWN' and len(result['zones']) == 2
    url = '/api/parking/zones/' + first['id']
    for body in ({}, {'threshold': None}, {'threshold': .01}, {'confirmFrames': True},
                 {'polygon': POLYGON[:2]}, {'enabled': None}, {'cameraId': 'second'}):
        assert client.patch(url, json=body).status_code == 422
    assert client.patch(url, json={'enabled': False, 'threshold': .4}).json()['revision'] == 2
    assert client.delete(url).status_code == 204
    assert client.delete(url).status_code == 404
    assert client.get('/api/parking/zones').json() == [second]


def test_worker_calibration_freshness_and_revision(service, monkeypatch):
    client, app, _, _, engine = service
    zone = create(client)
    worker = app.state.color_parking
    image = np.full((100, 100, 3), 100, np.uint8)
    counter = [0]
    monkeypatch.setattr(worker, 'fetch_frame', lambda camera: (image.copy(), f'session:1:{counter[0]}', 0))
    def tick():
        counter[0] += 1
        worker.poll_once()
    def current():
        return client.get('/api/parking/status').json()['spaces'][0]
    tick()
    for _ in range(4):
        worker.poll_once()
    assert current()['status'] == 'UNKNOWN'  # Same JPEG does not confirm.
    tick(); tick()
    assert current()['status'] == 'EMPTY'
    url = '/api/parking/zones/' + zone['id']
    assert client.post(url + '/calibrate', json={}).json()['calibrated'] is True
    assert current()['status'] == 'UNKNOWN'
    image[:] = [0, 0, 255]  # Monochrome car: reference difference detects it.
    tick(); tick(); tick()
    assert current()['status'] == 'OCCUPIED'
    # Restart preserves config/reference but requires new observations.
    other = create_app(app.state.config, engine, start_poller=False)
    with TestClient(other) as restarted:
        assert restarted.get('/api/parking/zones').json()[0]['calibrated']
        assert restarted.get('/api/parking/status').json()['spaces'][0]['status'] == 'UNKNOWN'
    with worker.lock:
        for obs in worker.observations.values():
            obs['observed'] = time.monotonic() - 10
    assert current()['status'] == 'UNKNOWN'
    tick()
    assert current()['status'] == 'UNKNOWN'  # Confirmation resets after expiry.
    assert client.patch(url, json={'polygon': [{'x': p['x'] / 2, 'y': p['y']} for p in POLYGON]}).json()['calibrated'] is False
    assert client.delete(url + '/calibrate').status_code == 204


def test_disabled_unknown_conflict_and_outage(service, monkeypatch):
    client, app, _, _, _ = service
    first = create(client, confirmFrames=1)
    second = create(client, 'second', first['parkingSpaceId'], confirmFrames=1)
    worker = app.state.color_parking
    gray = np.full((100, 100, 3), 100, np.uint8)
    colorful = np.random.default_rng(4).integers(0, 256, gray.shape, dtype=np.uint8)
    monkeypatch.setattr(worker, 'fetch_frame', lambda camera: (gray if camera == 'first' else colorful, 's:1:1', 0))
    worker.poll_once()
    result = worker.snapshot()['spaces'][0]
    assert result['status'] == 'OCCUPIED' and result['conflict']
    client.patch('/api/parking/zones/' + second['id'], json={'enabled': False})
    assert worker.snapshot()['spaces'][0]['status'] == 'EMPTY'
    def unavailable(camera):
        raise ValueError('Unavailable')
    monkeypatch.setattr(worker, 'fetch_frame', unavailable)
    worker.poll_once()
    assert worker.snapshot()['spaces'][0]['status'] == 'UNKNOWN'
    assert client.post('/api/parking/zones/' + first['id'] + '/calibrate').status_code == 409


def test_jpeg_transport_stale_and_malformed(service):
    client, app, _, _, _ = service
    worker = app.state.color_parking
    ok, encoded = cv2.imencode('.jpg', np.full((120, 800, 3), 100, np.uint8))
    assert ok
    data = {'age': '0.1', 'body': encoded.tobytes()}
    def respond(request):
        assert request.url.path == '/snapshots/first.jpg'
        return httpx.Response(200, content=data['body'], headers={'X-Frame-Age': data['age'], 'X-Frame-Identity': 's:1:1'})
    worker.client.close()
    worker.client = httpx.Client(base_url='http://test', transport=httpx.MockTransport(respond))
    image, identity, age = worker.fetch_frame('first')
    assert image.shape[1] == 640 and identity == 's:1:1' and .1 <= age < 1
    data['age'] = '20'
    with pytest.raises(ValueError):
        worker.fetch_frame('first')
    data.update(age='0', body=b'not a jpeg')
    with pytest.raises(ValueError):
        worker.fetch_frame('first')
