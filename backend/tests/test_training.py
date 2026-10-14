"""Training job API, worker protocol, production guard and dataset inspection (CPU, SQLite)."""
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import time
from uuid import uuid4
import zipfile

import cv2
import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.db import CameraStatus, make_engine
from backend.app.main import create_app
from backend.app.settings import Settings
from backend.app.training import estimate_vram_mib, inspect_archive, required_free_mib
from backend.app.training_models import TrainingJob

ADMIN = {'Authorization': 'Bearer ' + 'a' * 32}
WORKER = {'Authorization': 'Bearer ' + 'w' * 32}
T0 = datetime(2026, 10, 1, 8, tzinfo=timezone.utc)
GPU_FREE = {'available': True, 'index': 0, 'name': 'Tesla V100', 'total_mib': 16384, 'used_mib': 4900,
            'free_mib': 11484, 'util': 55}


@pytest.fixture
def training(tmp_path):
    config = tmp_path / 'cameras.yaml'
    config.write_text('cameras: [{id: first, floor: B1, name: B1 주차장 입구, rtsp_env: PRIVATE_FIRST}, '
                      '{id: second, floor: 1, name: Lobby, rtsp_env: PRIVATE_SECOND}]')
    settings = Settings(f'sqlite:///{tmp_path / "test.db"}', config, dataset_dir=tmp_path / 'storage',
                        training_admin_token='a' * 32, training_worker_token='w' * 32, training_guard_samples=2)
    state = {'counter': 0, 'timestamp': T0}

    def respond(request):
        state['counter'] += 1
        image = np.full((100, 200, 3), state['counter'] * 7 % 255, np.uint8)
        _, jpeg = cv2.imencode('.jpg', image)
        return httpx.Response(200, content=jpeg.tobytes(), headers={
            'X-Captured-At': state['timestamp'].isoformat(), 'Content-Type': 'image/jpeg',
            'X-Capture-Clean': 'pre-osd-v1'})

    engine = make_engine(settings.database_url)
    app = create_app(settings, engine, httpx.MockTransport(respond), start_poller=False)
    with TestClient(app) as client:
        app.state.poller.preview_state = 'reachable'
        app.state.poller.last_success = None
        yield client, app, state
    engine.dispose()


def make_export(client, state, sessions=4, per_session=1, boxes=1):
    """Each session is > 30 min apart so sessions spread over train/val/test."""
    for s in range(sessions):
        for i in range(per_session):
            state['timestamp'] = T0 + timedelta(hours=s, minutes=i)
            image = client.post('/api/datasets/capture', json={'camera_id': 'first'}).json()
            body = [{'id': str(uuid4()), 'class_id': 0, 'x': 10 + 5 * b, 'y': 20, 'width': 40, 'height': 30,
                     'source': 'manual'} for b in range(boxes)]
            saved = client.put(f'/api/datasets/images/{image["id"]}/annotations',
                               json={'revision': image['revision'], 'boxes': body, 'status': 'reviewed'})
            assert saved.status_code == 200, saved.text
    job = client.post('/api/datasets/exports', json={'seed': 42}).json()
    for _ in range(200):
        current = next(e for e in client.get('/api/datasets/exports').json() if e['id'] == job['id'])
        if current['status'] != 'building':
            assert current['status'] == 'ready', current
            return current
        time.sleep(.02)
    pytest.fail('export did not finish')


def set_fps(app, values):
    with app.state.sessions.begin() as session:
        for camera_id, fps in values.items():
            row = session.get(CameraStatus, camera_id)
            row.state, row.fps, row.observed_at = 'online', fps, datetime.now(timezone.utc)
            row.last_frame_at = datetime.now(timezone.utc)


def beat(client, worker_id='w1', job=None, started=False, gpu=GPU_FREE):
    response = client.post('/api/training/worker/heartbeat', headers=WORKER, json={
        'worker_id': worker_id, 'started': started, 'gpu': gpu, 'job': job,
        'base_weights': {'present': True, 'sha256': 'b' * 64, 'size': 10}, 'disk_free_mib': 50000})
    assert response.status_code == 200, response.text
    return response.json()


def create(client, export, **changes):
    return client.post('/api/training/jobs', headers=ADMIN, json={'export_id': export['id'], **changes})


def test_options_status_and_vram_policy(training):
    client, app, _ = training
    options = client.get('/api/training/options').json()
    assert options['defaults'] == {'epochs': 50, 'imgsz': 640, 'batch': 4, 'seed': 42, 'patience': 15,
                                   'device': 'gpu', 'class_mode': 'vehicle3', 'base_model_id': 'yolo11m-coco'}
    assert options['control_enabled'] and options['max_concurrent_jobs'] == 1
    status = client.get('/api/training/status').json()
    assert status['worker']['online'] is False and status['gpu_ready'] is False
    assert 'offline' in status['gpu_blocker']
    beat(client, started=True)
    status = client.get('/api/training/status?imgsz=960&batch=4').json()
    assert status['worker']['online'] and status['gpu_ready'], status
    assert status['estimate']['training_mib'] == estimate_vram_mib(960, 4) == 7350
    assert required_free_mib(app.state.config, 640, 4) == 6000  # min free wins for the default
    busy = {**GPU_FREE, 'used_mib': 12000, 'free_mib': 4384}
    beat(client, gpu=busy)
    assert 'Not enough free GPU memory' in client.get('/api/training/status').json()['gpu_blocker']
    beat(client, gpu={'available': False, 'index': 0, 'error': 'nvidia-smi failed'})
    assert 'CUDA' in client.get('/api/training/status').json()['gpu_blocker']
    models = client.get('/api/training/models').json()
    assert [m['id'] for m in models] == ['yolo11m-coco'] and models[0]['kind'] == 'base'
    assert models[0]['files']['yolo11m.pt']['download_url'] is None


def test_auth_is_required_and_disabled_without_tokens(training, tmp_path):
    client, app, state = training
    export = make_export(client, state)
    assert client.post('/api/training/jobs', json={'export_id': export['id']}).status_code == 401
    assert client.post('/api/training/jobs', headers={'Authorization': 'Bearer wrong'},
                       json={'export_id': export['id']}).status_code == 401
    assert client.post('/api/training/auth', headers=ADMIN).status_code == 204
    assert client.post('/api/training/worker/claim', headers=ADMIN, json={'worker_id': 'x'}).status_code == 401
    assert client.post('/api/training/worker/claim', json={'worker_id': 'x'}).status_code == 401
    with pytest.raises(ValueError):
        Settings('sqlite://', tmp_path, training_admin_token='short')
    with pytest.raises(ValueError):
        Settings('sqlite://', tmp_path, training_admin_token='x' * 30, training_worker_token='x' * 30)
    app.state.config = Settings('sqlite://', tmp_path)
    assert client.post('/api/training/auth', headers=ADMIN).status_code == 503
    assert client.post('/api/training/worker/heartbeat', headers=WORKER, json={'worker_id': 'w'}).status_code == 503


def test_dataset_listing_and_suitability(training):
    client, app, state = training
    export = make_export(client, state, sessions=4)
    listed = client.get('/api/training/datasets').json()
    assert listed[0]['id'] == export['id'] and listed[0]['readiness'] == 'limited'
    check = client.get(f'/api/training/datasets/{export["id"]}/check').json()
    assert check['counts']['total'] == 4 and check['counts']['val'] >= 1
    assert check['classes'] == {'0': 'car'} or check['classes'] == {0: 'car'}
    assert check['labels_per_class_total'] == {'car': 4}
    assert any('Validation has only' in item for item in check['limits'])
    assert check['errors'] == []


def test_export_without_validation_is_blocked(training):
    client, app, state = training
    single = make_export(client, state, sessions=1)  # one session: train only, no val
    check = client.get(f'/api/training/datasets/{single["id"]}/check').json()
    assert check['readiness'] == 'blocked' and any('No validation images' in e for e in check['errors'])
    beat(client, started=True)
    refused = create(client, single, acknowledge_limits=True)
    assert refused.status_code == 422 and 'validation' in refused.json()['detail']


def write_zip(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer


GOOD_YAML = 'train: images/train\nval: images/val\ntest: images/test\nnames:\n  0: car\n'


def manifest(*items):
    return json.dumps({'images': [{'id': i, 'split': s, 'session': ses, 'camera_id': 'c'} for i, s, ses in items],
                       'counts': {s: sum(1 for _, x, _ in items if x == s) for s in ('train', 'val', 'test')},
                       'warnings': []})


def test_inspect_archive_rejects_unsafe_and_invalid_archives(tmp_path):
    path = tmp_path / 'a.zip'
    path.write_bytes(write_zip({'../evil.txt': 'x', 'data.yaml': GOOD_YAML}).getvalue())
    assert inspect_archive(path)['readiness'] == 'blocked'
    path.write_bytes(write_zip({'data.yaml': GOOD_YAML, 'manifest.json': manifest(('a', 'train', 's1'), ('b', 'val', 's2')),
                                'images/train/a.jpg': 'x', 'labels/train/a.txt': '0 0.9 0.5 0.4 0.2\n',
                                'images/val/b.jpg': 'x', 'labels/val/b.txt': '1 0.5 0.5 0.1 0.1\n'}).getvalue())
    result = inspect_archive(path)
    assert result['readiness'] == 'blocked'
    assert any('outside the image' in e for e in result['errors'])
    assert any('class 1 is not in the dataset classes' in e for e in result['errors'])
    path.write_bytes(write_zip({'data.yaml': GOOD_YAML.replace('0: car', '0: person'),
                                'manifest.json': manifest(('a', 'train', 's1'), ('b', 'val', 's1')),
                                'images/train/a.jpg': 'x', 'labels/train/a.txt': '0 0.5 0.5 0.2 0.2\n',
                                'images/val/b.jpg': 'x'}).getvalue())
    result = inspect_archive(path)
    assert any('Unexpected dataset classes' in e for e in result['errors'])
    assert any('more than one split' in e for e in result['errors'])
    assert any('must both exist' in e for e in result['errors'])
    path.write_bytes(write_zip({'dataset/data.yaml': GOOD_YAML, 'dataset/manifest.json': manifest(
        ('a', 'train', 's1'), ('b', 'val', 's2'), ('c', 'test', 's3')),
        'dataset/images/train/a.jpg': 'x', 'dataset/labels/train/a.txt': '0 0.5 0.5 0.2 0.2\n',
        'dataset/images/val/b.jpg': 'x', 'dataset/labels/val/b.txt': '',
        'dataset/images/test/c.jpg': 'x', 'dataset/labels/test/c.txt': '0 0.5 0.5 0.2 0.2\n'}).getvalue())
    result = inspect_archive(path)
    assert result['errors'] == [] and result['readiness'] == 'limited' and result['counts']['total'] == 3


def run_until_claimed(client, app, export, **changes):
    set_fps(app, {'first': 10.0, 'second': 10.0})
    response = create(client, export, acknowledge_limits=True, **changes)
    assert response.status_code == 201, response.text
    job = response.json()
    spec = client.post('/api/training/worker/claim', headers=WORKER, json={'worker_id': 'w1'})
    assert spec.status_code == 200, spec.text
    return job, spec.json()


def test_job_lifecycle_progress_stop_and_single_slot(training):
    client, app, state = training
    export = make_export(client, state)
    assert create(client, export, acknowledge_limits=True).status_code == 503  # worker offline
    beat(client, started=True)
    limited = create(client, export)
    assert limited.status_code == 422 and 'limitations' in limited.json()['detail']
    assert create(client, export, epochs=0, acknowledge_limits=True).status_code == 422
    assert create(client, export, imgsz=800, acknowledge_limits=True).status_code == 422
    assert create(client, export, batch='4', acknowledge_limits=True).status_code == 422
    assert create(client, export, device='cpu', acknowledge_limits=True).status_code == 422
    app.state.poller.preview_state = 'unreachable'
    assert 'Preview' in create(client, export, acknowledge_limits=True).json()['detail']
    app.state.poller.preview_state = 'reachable'
    job, spec = run_until_claimed(client, app, export)
    assert job['status'] == 'queued' and job['config']['epochs'] == 50 and job['config']['batch'] == 4
    assert create(client, export, acknowledge_limits=True).status_code == 409  # one job at a time
    assert spec['id'] == job['id'] and spec['config']['class_mode'] == 'vehicle3'
    assert spec['base_model'] == {'id': 'yolo11m-coco', 'kind': 'base', 'job_id': None, 'sha256': 'b' * 64}
    assert client.post('/api/training/worker/claim', headers=WORKER, json={'worker_id': 'w1'}).status_code == 204
    download = client.get(f'/api/training/worker/jobs/{job["id"]}/dataset', headers=WORKER)
    assert download.status_code == 200 and hashlib.sha256(download.content).hexdigest() == spec['dataset_sha256']
    assert client.get(f'/api/training/worker/jobs/{job["id"]}/dataset').status_code == 401
    rows = [{'epoch': 1.0, 'time': 30.0, 'train/box_loss': 1.0, 'train/cls_loss': .5, 'train/dfl_loss': .5,
             'metrics/precision(B)': .6, 'metrics/recall(B)': .5, 'metrics/mAP50(B)': .55, 'metrics/mAP50-95(B)': .3,
             'val/box_loss': 1.2, 'val/cls_loss': .6, 'val/dfl_loss': .7}]
    reply = beat(client, job={'id': job['id'], 'phase': 'training', 'progress': {'epoch': 2, 'epochs': 50, 'batch': 5,
                                                                                   'batches': 10, 'train_loss': {'train/box_loss': .9}},
                              'metrics_rows': rows, 'metrics_offset': 0, 'log_lines': ['epoch 1 done']},
                 gpu={**GPU_FREE, 'training_mib': 3300})
    assert reply == {'ok': True, 'stop': False, 'stop_reason': None}
    detail = client.get(f'/api/training/jobs/{job["id"]}').json()
    assert detail['status'] == 'running' and detail['epoch'] == 2 and detail['progress'] == pytest.approx(1.5 / 50)
    assert detail['latest']['train_loss'] == 2.0 and detail['latest']['val_loss'] == 2.5 and detail['latest']['map50'] == .55
    assert detail['log_tail'] == ['epoch 1 done'] and detail['gpu']['training_mib'] == 3300
    assert detail['monitor']['baseline']['total_fps'] == 20.0 and detail['monitor']['samples'][-1]['fps_ratio'] == 1.0
    assert client.post(f'/api/training/jobs/{job["id"]}/stop').status_code == 401
    stopped = client.post(f'/api/training/jobs/{job["id"]}/stop', headers=ADMIN).json()
    assert stopped['status'] == 'stopping' and stopped['stop_requested']
    reply = beat(client, job={'id': job['id'], 'phase': 'training'})
    assert reply['stop'] is True and reply['stop_reason'] == 'Stopped by the user'
    finish = client.post(f'/api/training/worker/jobs/{job["id"]}/finish', headers=WORKER,
                         json={'worker_id': 'w1', 'status': 'stopped', 'error': 'Stopped by the user', 'metrics_rows': rows})
    assert finish.status_code == 200 and finish.json()['status'] == 'stopped' and finish.json()['model_id'] is None
    again = client.post(f'/api/training/worker/jobs/{job["id"]}/finish', headers=WORKER,
                        json={'worker_id': 'w1', 'status': 'completed'})
    assert again.status_code == 409
    second = create(client, export, acknowledge_limits=True)  # slot released
    assert second.status_code == 201
    queued_stop = client.post(f'/api/training/jobs/{second.json()["id"]}/stop', headers=ADMIN).json()
    assert queued_stop['status'] == 'stopped'
    assert client.post(f'/api/training/jobs/{second.json()["id"]}/stop', headers=ADMIN).status_code == 409


def upload(client, job_id, name, data):
    return client.put(f'/api/training/worker/jobs/{job_id}/artifacts/{name}', headers={
        **WORKER, 'Content-Type': 'application/octet-stream'}, content=data)


def test_completed_job_registers_model_and_serves_results(training):
    client, app, state = training
    export = make_export(client, state, sessions=4)
    beat(client, started=True)
    job, spec = run_until_claimed(client, app, export, name='parking-ft', class_mode='single', imgsz=960)
    assert upload(client, job['id'], '../etc/passwd', b'x').status_code == 404
    assert upload(client, job['id'], 'evil.py', b'x').status_code == 404
    assert upload(client, job['id'], 'best.pt', b'weights').json()['sha256'] == hashlib.sha256(b'weights').hexdigest()
    upload(client, job['id'], 'last.pt', b'last')
    first_image = client.get('/api/datasets/images').json()['items'][0]['id']
    predictions = {'split': 'test', 'imgsz': 960, 'conf': .25, 'nms_iou': .45, 'images': [
        {'id': first_image, 'width': 200, 'height': 100, 'gt': [[10, 20, 50, 50]],
         'models': {'baseline': {'tp': 0, 'fp': 1, 'fn': 1, 'pred_tp': [], 'pred_fp': [[1, 1, 5, 5]], 'gt_fn': [[10, 20, 50, 50]]},
                    'new': {'tp': 1, 'fp': 0, 'fn': 0, 'pred_tp': [[10, 20, 50, 50]], 'pred_fp': [], 'gt_fn': []}}},
        {'id': 'deleted', 'width': 200, 'height': 100, 'gt': [], 'models': {'new': {'tp': 0, 'fp': 0, 'fn': 0}}}]}
    upload(client, job['id'], 'eval_predictions.json', json.dumps(predictions).encode())
    metrics = {'images': 1, 'dataset_warnings': [], 'models': {
        'baseline': {'vehicle': {'mAP50': .4}, 'operating_point': {'precision': .5, 'recall': .4}},
        'new': {'vehicle': {'mAP50': .6}, 'operating_point': {'precision': .7, 'recall': .6}}}}
    finish = client.post(f'/api/training/worker/jobs/{job["id"]}/finish', headers=WORKER, json={
        'worker_id': 'w1', 'status': 'completed', 'run_info': {'names': {'0': 'item'}, 'final_batch': 4},
        'metrics_rows': [{'epoch': 1.0, 'metrics/mAP50-95(B)': .3}, {'epoch': 2.0, 'metrics/mAP50-95(B)': .35}],
        'evaluation': {'status': 'completed', 'metrics': metrics}})
    assert finish.status_code == 200, finish.text
    done = client.get(f'/api/training/jobs/{job["id"]}').json()
    assert done['status'] == 'completed' and done['progress'] == 1.0 and done['best']['epoch'] == 2
    assert done['evaluation']['conclusive'] is False and any('Only 1 test images' in c for c in done['evaluation']['caveats'])
    assert done['has_comparison'] and set(done['artifacts']) == {'best.pt', 'last.pt', 'eval_predictions.json'}
    models = client.get('/api/training/models').json()
    tuned = next(m for m in models if m['kind'] == 'finetuned')
    assert tuned['label'] == 'parking-ft v1' and tuned['class_mode'] == 'single' and tuned['config']['imgsz'] == 960
    assert tuned['deployment'] == 'not_deployed' and tuned['metrics']['test']['vehicle']['mAP50'] == .6
    assert tuned['evaluation_conclusive'] is False and tuned['status'] == 'available'
    assert client.get(tuned['files']['best.pt']['download_url']).status_code == 401
    file = client.get(tuned['files']['best.pt']['download_url'], headers=ADMIN)
    assert file.status_code == 200 and file.content == b'weights' and file.headers['content-type'] == 'application/octet-stream'
    assert client.get(f'/api/training/jobs/{job["id"]}/artifacts/last.pt', headers=ADMIN).content == b'last'
    comparison = client.get(f'/api/training/jobs/{job["id"]}/comparison').json()
    assert comparison['total'] == 2 and comparison['images'][0]['image_url'] == f'/api/datasets/images/{first_image}/file'
    assert comparison['images'][1]['image_url'] is None
    assert client.get(f'/api/training/jobs/{job["id"]}/comparison?only_errors=true').json()['total'] == 1
    compare = client.get(f'/api/training/models/compare?ids=yolo11m-coco,{tuned["id"]}').json()
    assert compare['comparable'] and len(compare['models']) == 2
    # Retraining from the new model: the worker gets its verified sha and fetches the weights.
    retrain = create(client, export, base_model_id=tuned['id'], class_mode='single', acknowledge_limits=True)
    assert retrain.status_code == 201
    spec = client.post('/api/training/worker/claim', headers=WORKER, json={'worker_id': 'w1'}).json()
    assert spec['base_model'] == {'id': tuned['id'], 'kind': 'finetuned', 'job_id': job['id'],
                                  'sha256': hashlib.sha256(b'weights').hexdigest()}
    weights = client.get(f'/api/training/worker/models/{tuned["id"]}/weights', headers=WORKER)
    assert weights.content == b'weights'
    client.post(f'/api/training/worker/jobs/{spec["id"]}/finish', headers=WORKER,
                json={'worker_id': 'w1', 'status': 'failed', 'error_code': 'gpu_oom', 'error': 'GPU memory ran out'})
    failed = client.get(f'/api/training/jobs/{spec["id"]}').json()
    assert failed['status'] == 'failed' and failed['error_code'] == 'gpu_oom'
    incompatible = create(client, export, base_model_id=tuned['id'], class_mode='vehicle3', acknowledge_limits=True)
    assert incompatible.status_code == 422


def test_completed_without_best_pt_fails(training):
    client, app, state = training
    export = make_export(client, state)
    beat(client, started=True)
    job, _ = run_until_claimed(client, app, export)
    response = client.post(f'/api/training/worker/jobs/{job["id"]}/finish', headers=WORKER,
                           json={'worker_id': 'w1', 'status': 'completed'})
    assert response.json()['status'] == 'failed' and response.json()['error_code'] == 'model_missing'
    assert all(m['kind'] == 'base' for m in client.get('/api/training/models').json())


def test_production_guard_stops_training_when_preview_fps_drops(training):
    client, app, state = training
    export = make_export(client, state)
    beat(client, started=True)
    job, _ = run_until_claimed(client, app, export)
    set_fps(app, {'first': 7.0, 'second': 10.0})  # 15% drop on the cameras online at baseline
    assert beat(client, job={'id': job['id'], 'phase': 'training'})['stop'] is False
    set_fps(app, {'first': 5.0, 'second': 10.0})  # 25%
    assert beat(client, job={'id': job['id'], 'phase': 'training'})['stop'] is False  # 1 of 2 samples
    reply = beat(client, job={'id': job['id'], 'phase': 'training'})
    assert reply['stop'] is True and reply['stop_reason'].startswith('Production protection: Preview FPS dropped 25%')
    client.post(f'/api/training/worker/jobs/{job["id"]}/finish', headers=WORKER,
                json={'worker_id': 'w1', 'status': 'stopped', 'error': reply['stop_reason']})
    detail = client.get(f'/api/training/jobs/{job["id"]}').json()
    assert detail['status'] == 'stopped' and detail['error_code'] == 'production_protection'
    # GPU headroom below the DeepStream reserve also counts as a violation.
    set_fps(app, {'first': 10.0, 'second': 10.0})
    job, _ = run_until_claimed(client, app, export)
    low = {**GPU_FREE, 'free_mib': 1000, 'used_mib': 15384}
    beat(client, job={'id': job['id'], 'phase': 'training'}, gpu=low)
    assert 'below the DeepStream reserve' in beat(client, job={'id': job['id'], 'phase': 'training'}, gpu=low)['stop_reason']


def test_worker_restart_and_lost_heartbeat_fail_running_jobs(training):
    client, app, state = training
    export = make_export(client, state)
    beat(client, started=True)
    job, _ = run_until_claimed(client, app, export)
    beat(client, worker_id='w2', started=True)
    detail = client.get(f'/api/training/jobs/{job["id"]}').json()
    assert detail['status'] == 'failed' and detail['error_code'] == 'worker_restarted'
    assert beat(client, worker_id='w1', job={'id': job['id'], 'phase': 'training'})['stop'] is True
    beat(client, worker_id='w1', started=True)
    job, _ = run_until_claimed(client, app, export)
    with app.state.sessions.begin() as session:
        session.get(TrainingJob, job['id']).heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    assert client.get(f'/api/training/jobs/{job["id"]}').json()['status'] == 'running'  # Backend start grace
    app.state.training.started -= timedelta(minutes=10)
    detail = client.get(f'/api/training/jobs/{job["id"]}').json()
    assert detail['status'] == 'failed' and detail['error_code'] == 'worker_lost'
    with app.state.sessions() as session:
        assert session.scalar(select(TrainingJob.active_slot).where(TrainingJob.id == job['id'])) is None


def test_snapshot_survives_export_deletion(training):
    client, app, state = training
    export = make_export(client, state)
    beat(client, started=True)
    job, spec = run_until_claimed(client, app, export)
    assert client.delete(f'/api/datasets/exports/{export["id"]}').status_code == 204
    download = client.get(f'/api/training/worker/jobs/{job["id"]}/dataset', headers=WORKER)
    assert download.status_code == 200 and hashlib.sha256(download.content).hexdigest() == spec['dataset_sha256']
