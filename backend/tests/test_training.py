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
from backend.app.training import estimate_vram_mib, inspect_archive, metric_point, required_free_mib
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
                                   'device': 'gpu', 'class_mode': 'vehicle3', 'base_model_id': 'yolo11m-coco',
                                   'train_mode': 'standard'}
    assert set(options['train_modes']) == {'standard', 'all'}
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


def finish_training(client, job_id, train_all=False, best=b'weights', names=None):
    upload(client, job_id, 'best.pt', best)
    rows = [{'epoch': 1.0, 'train/box_loss': 1.0, 'metrics/mAP50-95(B)': .3, 'val/box_loss': 1.2},
            {'epoch': 2.0, 'train/box_loss': .8, 'metrics/mAP50-95(B)': .9, 'val/box_loss': .4}]
    evaluation = ({'status': 'skipped', 'reason': 'Train All'} if train_all else
                  {'status': 'completed', 'metrics': {'images': 1, 'models': {'new': {'vehicle': {'mAP50': .5}}}}})
    response = client.post(f'/api/training/worker/jobs/{job_id}/finish', headers=WORKER, json={
        'worker_id': 'w1', 'status': 'completed', 'metrics_rows': rows, 'evaluation': evaluation,
        'run_info': {'names': names or {'2': 'car'}, 'final_batch': 4}})
    assert response.status_code == 200, response.text
    return response.json()


def test_train_all_mode_uses_every_split_and_hides_training_fit(training):
    client, app, state = training
    single = make_export(client, state, sessions=1)  # no val split: blocked for standard, allowed for Train All
    standard = client.get(f'/api/training/datasets/{single["id"]}/check').json()
    assert standard['readiness'] == 'blocked' and standard['train_mode'] == 'standard'
    check = client.get(f'/api/training/datasets/{single["id"]}/check?train_mode=all').json()
    assert check['readiness'] == 'limited' and check['errors'] == [] and check['train_mode'] == 'all'
    assert any('no independent' in item for item in check['limits'])
    listed = client.get('/api/training/datasets').json()[0]
    assert listed['readiness_by_mode'] == {'standard': 'blocked', 'all': 'limited'}
    beat(client, started=True)
    assert create(client, single, train_mode='all', class_mode='single', acknowledge_limits=True).status_code == 422
    assert create(client, single, train_mode='all').status_code == 422  # limitation must be confirmed
    assert create(client, single, train_mode='bogus', acknowledge_limits=True).status_code == 422
    job, spec = run_until_claimed(client, app, single, train_mode='all', epochs=40)
    assert spec['config']['train_mode'] == 'all' and spec['config']['kind'] == 'train'
    assert spec['config']['early_stopping'] is False and spec['config']['weights_selection'] == 'final_epoch'
    assert job['config']['train_mode'] == 'all' and job['dataset_summary']['train_mode'] == 'all'
    rows = [{'epoch': 1.0, 'train/box_loss': 1.0, 'metrics/mAP50(B)': .99, 'val/box_loss': .1}]
    beat(client, job={'id': job['id'], 'phase': 'training', 'metrics_rows': rows, 'metrics_offset': 0})
    latest = client.get(f'/api/training/jobs/{job["id"]}').json()['latest']
    assert latest['train_loss'] == 1.0 and latest['map50'] is None and latest['val_loss'] is None
    done = finish_training(client, job['id'], train_all=True)
    assert done['status'] == 'completed' and done['best']['map50_95'] is None
    model = next(m for m in client.get('/api/training/models').json() if m['kind'] == 'finetuned')
    assert model['config']['train_mode'] == 'all' and model['config']['weights_selection'] == 'final_epoch'
    assert model['config']['train_images'] == 1
    assert model['metrics']['val'] is None and model['metrics']['test'] is None
    assert model['evaluation_conclusive'] is False


def test_metric_point_drops_validation_for_train_all():
    row = {'epoch': 3.0, 'train/box_loss': 1.0, 'val/box_loss': 2.0, 'metrics/mAP50(B)': .8}
    assert metric_point(row)['map50'] == .8 and metric_point(row)['val_loss'] == 2.0
    assert metric_point(row, True)['map50'] is None and metric_point(row, True)['train_loss'] == 1.0


def test_new_image_evaluation_excludes_trained_images_and_sessions(training):
    client, app, state = training
    old = make_export(client, state, sessions=4)
    beat(client, started=True)
    job, _ = run_until_claimed(client, app, old, train_mode='all')
    finish_training(client, job['id'], train_all=True)
    model = next(m for m in client.get('/api/training/models').json() if m['kind'] == 'finetuned')
    # Same export: everything was trained on, nothing is new.
    plan = client.get(f'/api/training/evaluations/plan?model_id={model["id"]}&export_id={old["id"]}').json()
    assert plan['readiness'] == 'blocked' and plan['eligible'] == 0 and plan['excluded_trained'] == 4
    assert client.post('/api/training/evaluations', headers=ADMIN, json={
        'model_id': model['id'], 'export_id': old['id'], 'acknowledge_limits': True}).status_code == 422
    # New captures: 1 frame chained to the last trained session (excluded) plus 2 new sessions.
    trained = {item['id'] for item in client.get('/api/datasets/images').json()['items']}
    for hours, minute in ((3, 5), (10, 0), (12, 0)):
        state['timestamp'] = T0 + timedelta(hours=hours, minutes=minute)
        image = client.post('/api/datasets/capture', json={'camera_id': 'first'}).json()
        client.put(f'/api/datasets/images/{image["id"]}/annotations', json={'revision': image['revision'], 'status': 'reviewed',
                   'boxes': [{'id': str(uuid4()), 'class_id': 0, 'x': 5, 'y': 5, 'width': 30, 'height': 20}]})
    new = client.post('/api/datasets/exports', json={'seed': 7}).json()
    for _ in range(200):
        if next(e for e in client.get('/api/datasets/exports').json() if e['id'] == new['id'])['status'] == 'ready':
            break
        time.sleep(.02)
    plan = client.get(f'/api/training/evaluations/plan?model_id={model["id"]}&export_id={new["id"]}').json()
    assert (plan['total'], plan['eligible'], plan['excluded_trained'], plan['excluded_same_session']) == (7, 2, 4, 1)
    assert plan['readiness'] == 'limited' and plan['sessions'] == 2 and 'image_ids' not in plan
    assert client.get(f'/api/training/evaluations/plan?model_id=yolo11m-coco&export_id={new["id"]}').status_code == 409
    body = {'model_id': model['id'], 'export_id': new['id']}
    assert client.post('/api/training/evaluations', json=body).status_code == 401
    assert client.post('/api/training/evaluations', headers=ADMIN, json=body).status_code == 422
    created = client.post('/api/training/evaluations', headers=ADMIN, json={**body, 'acknowledge_limits': True})
    assert created.status_code == 201, created.text
    evaluation = created.json()
    assert evaluation['config']['kind'] == 'evaluate' and evaluation['config']['eval_images'] == 2
    assert 'eval_image_ids' not in evaluation['config'] and evaluation['base_model_id'] == model['id']
    spec = client.post('/api/training/worker/claim', headers=WORKER, json={'worker_id': 'w1'}).json()
    assert spec['config']['kind'] == 'evaluate' and len(spec['config']['eval_image_ids']) == 2
    assert spec['base_model']['id'] == model['id'] and spec['base_model']['kind'] == 'finetuned'
    assert not trained & set(spec['config']['eval_image_ids'])
    metrics = {'images': 2, 'gt_boxes': 2, 'dataset_warnings': [], 'models': {
        'baseline': {'vehicle': {'mAP50': .4}, 'operating_point': {'recall': .5}},
        'new': {'vehicle': {'mAP50': .7}, 'operating_point': {'recall': 1.0}}}}
    finish = client.post(f'/api/training/worker/jobs/{spec["id"]}/finish', headers=WORKER, json={
        'worker_id': 'w1', 'status': 'completed', 'evaluation': {'status': 'completed', 'metrics': metrics}})
    assert finish.status_code == 200 and finish.json()['status'] == 'completed' and finish.json()['model_id'] is None
    detail = client.get(f'/api/training/jobs/{spec["id"]}').json()
    assert detail['evaluation']['conclusive'] is False
    assert any('Only 2 test images' in c for c in detail['evaluation']['caveats'])
    models = client.get('/api/training/models').json()
    assert sum(m['kind'] == 'finetuned' for m in models) == 1  # evaluation never registers a model
    tuned = next(m for m in models if m['kind'] == 'finetuned')
    entry = tuned['metrics']['new_images'][-1]
    assert entry['job_id'] == spec['id'] and entry['images'] == 2 and entry['new']['vehicle']['mAP50'] == .7
    assert entry['excluded'] == {'trained': 4, 'same_session': 1, 'export_total': 7}


def test_retraining_from_a_model_that_saw_the_test_split_is_not_independent(training):
    client, app, state = training
    export = make_export(client, state, sessions=4)
    beat(client, started=True)
    job, _ = run_until_claimed(client, app, export, train_mode='all')
    finish_training(client, job['id'], train_all=True)
    model = next(m for m in client.get('/api/training/models').json() if m['kind'] == 'finetuned')
    retrain, _ = run_until_claimed(client, app, export, base_model_id=model['id'])
    assert retrain['dataset_summary']['test_overlap'] == retrain['dataset_summary']['counts']['test'] >= 1
    finish_training(client, retrain['id'])
    caveats = client.get(f'/api/training/jobs/{retrain["id"]}').json()['evaluation']['caveats']
    assert any('not independent' in c for c in caveats)


def runtime_beat(client, job=None, **deployment):
    status = {'available': True, 'runtime_online': True, 'active': 'production-original', 'previous': None,
              'phase': 'idle', 'session': 's1', 'verified': True, 'contract': 'private', **deployment}
    response = client.post('/api/training/worker/heartbeat', headers=WORKER, json={
        'worker_id': 'w1', 'gpu': GPU_FREE, 'job': job, 'base_weights': {}, 'disk_free_mib': 50000,
        'deployment': status})
    assert response.status_code == 200, response.text
    return response.json()


def test_deployment_requires_runtime_and_compatible_model_then_records_verified_result(training):
    client, app, state = training
    export = make_export(client, state, sessions=4)
    beat(client, started=True)
    assert client.get('/api/training/deployment').json()['available'] is False
    coco80 = {str(i): 'car' if i == 2 else f'class{i}' for i in range(80)}
    large, _ = run_until_claimed(client, app, export, imgsz=960)
    finish_training(client, large['id'], names=coco80)
    job, _ = run_until_claimed(client, app, export)
    finish_training(client, job['id'], names=coco80)
    models = {m['config']['imgsz']: m for m in client.get('/api/training/models').json() if m['kind'] == 'finetuned'}
    tuned = models[640]
    body = {'model_id': tuned['id'], 'action': 'deploy'}
    assert client.post('/api/training/deployments', json=body).status_code == 401
    assert client.post('/api/training/deployments', headers=ADMIN, json=body).status_code == 503  # runtime off
    runtime_beat(client)
    status = client.get('/api/training/deployment').json()
    assert status['available'] and status['confirmed'] and 'contract' not in status  # private field not exposed
    rejected = client.post('/api/training/deployments', headers=ADMIN, json={'model_id': models[960]['id']})
    assert rejected.status_code == 422 and '640px' in rejected.text  # resolution needs a restart: never automatic
    assert client.post('/api/training/deployments', headers=ADMIN, json={'action': 'rollback'}).status_code == 409
    created = client.post('/api/training/deployments', headers=ADMIN, json=body)
    assert created.status_code == 201, created.text
    assert created.json()['config']['kind'] == 'deploy' and created.json()['name'] == f'Deploy {tuned["label"]}'
    assert created.json()['config']['class_mode'] == 'vehicle3'
    assert client.post('/api/training/deployments', headers=ADMIN, json=body).status_code == 409  # single slot
    spec = client.post('/api/training/worker/claim', headers=WORKER, json={'worker_id': 'w1'}).json()
    assert spec['base_model']['id'] == tuned['id'] and spec['base_model']['sha256'] == hashlib.sha256(b'weights').hexdigest()
    runtime_beat(client, job={'id': spec['id'], 'phase': 'building'}, phase='building', operation=spec['id'])
    assert client.get(f'/api/training/jobs/{spec["id"]}').json()['phase'] == 'building'
    # A completed report without runtime verification is refused.
    finish = client.post(f'/api/training/worker/jobs/{spec["id"]}/finish', headers=WORKER,
                         json={'worker_id': 'w1', 'status': 'completed', 'run_info': {'deployment': {'phase': 'verifying'}}})
    assert finish.json()['status'] == 'failed' and finish.json()['error_code'] == 'deployment_unverified'
    assert sum(m['kind'] == 'finetuned' for m in client.get('/api/training/models').json()) == 2  # nothing deleted
    assert client.post('/api/training/deployments', headers=ADMIN, json=body).status_code == 409  # runtime busy
    runtime_beat(client, phase='failed', error='engine_build_failed')
    again = client.post('/api/training/deployments', headers=ADMIN, json=body).json()
    client.post('/api/training/worker/claim', headers=WORKER, json={'worker_id': 'w1'})
    result = {'phase': 'completed', 'active': tuned['id'], 'evidence': {'first': {'frames': 40, 'detections': 12}}}
    finish = client.post(f'/api/training/worker/jobs/{again["id"]}/finish', headers=WORKER,
                         json={'worker_id': 'w1', 'status': 'completed', 'run_info': {'deployment': result}})
    assert finish.json()['status'] == 'completed' and finish.json()['progress'] == 1.0
    deployed = {m['id']: m['deployment'] for m in client.get('/api/training/models').json()}
    assert deployed[tuned['id']] == 'active' and deployed['yolo11m-coco'] == 'not_deployed'
    runtime_beat(client, active=tuned['id'], previous='production-original', phase='completed')
    assert client.post('/api/training/deployments', headers=ADMIN, json=body).status_code == 409  # already active
    rollback = client.post('/api/training/deployments', headers=ADMIN, json={'action': 'rollback'})
    assert rollback.status_code == 201 and rollback.json()['base_model_id'] == 'yolo11m-coco'
    assert rollback.json()['config']['action'] == 'rollback'
    stopped = client.post(f'/api/training/jobs/{rollback.json()["id"]}/stop', headers=ADMIN).json()
    assert stopped['status'] == 'stopped'  # queued request cancelled before the runtime saw it
    # Runtime unverified after a failed recovery: active model shown as unconfirmed, new operations blocked.
    runtime_beat(client, active=tuned['id'], phase='rollback_failed', verified=False)
    status = client.get('/api/training/deployment').json()
    assert status['confirmed'] is False and status['active'] == tuned['id']
    assert client.post('/api/training/deployments', headers=ADMIN, json={'action': 'rollback'}).status_code == 409


def test_single_class_car_model_deploys_with_its_class_mode_and_rolls_back(training):
    client, app, state = training
    export = make_export(client, state, sessions=4)
    beat(client, started=True)
    job, _ = run_until_claimed(client, app, export, class_mode='single')
    upload(client, job['id'], 'best.pt', b'single')
    client.post(f'/api/training/worker/jobs/{job["id"]}/finish', headers=WORKER, json={
        'worker_id': 'w1', 'status': 'completed', 'run_info': {'names': {'0': 'item'}, 'final_batch': 4},
        'evaluation': {'status': 'skipped', 'reason': 'test'}})
    single = next(m for m in client.get('/api/training/models').json() if m['kind'] == 'finetuned')
    assert single['class_mode'] == 'single' and single['names_count'] == 1
    runtime_beat(client)
    created = client.post('/api/training/deployments', headers=ADMIN, json={'model_id': single['id']})
    assert created.status_code == 201, created.text
    assert created.json()['config']['class_mode'] == 'single'  # worker exports with the 0 -> car remap
    spec = client.post('/api/training/worker/claim', headers=WORKER, json={'worker_id': 'w1'}).json()
    assert spec['config']['class_mode'] == 'single' and spec['base_model']['id'] == single['id']
    # Failed apply: the runtime restored the COCO80 original; the registry keeps both models.
    failures = [{'code': 'verification_failed', 'phase': 'verifying', 'model_loaded': True,
                 'checks': [{'check': 'fps_below_baseline', 'camera': 'first', 'fps': 7.0, 'baseline_fps': 10.0}]}]
    result = {'phase': 'rolled_back', 'error': 'verification_failed', 'active': 'production-original',
              'failures': failures}
    failed = client.post(f'/api/training/worker/jobs/{spec["id"]}/finish', headers=WORKER, json={
        'worker_id': 'w1', 'status': 'failed', 'error_code': 'deployment_rolled_back', 'error': 'rolled_back',
        'run_info': {'deployment': result}}).json()
    assert failed['status'] == 'failed' and failed['error_code'] == 'deployment_rolled_back'
    detail = client.get(f'/api/training/jobs/{spec["id"]}').json()
    assert detail['run_info']['deployment']['failures'] == failures  # kept with the job for the web page
    assert {m['id']: m['deployment'] for m in client.get('/api/training/models').json()}[single['id']] == 'not_deployed'
    diagnosis = {'cameras': {'first': {'baseline_fps': 10.0, 'candidate_fps': 7.0, 'rollback_fps': 7.1,
                                       'verdict': 'camera_source_suspected'}}, 'service': {}}
    runtime_beat(client, phase='rolled_back', error='verification_failed', failures=failures,
                 checks=[{'check': 'service_fps_below_baseline', 'blocking': False}], service={'fps': 9.0},
                 diagnosis=diagnosis)
    status = client.get('/api/training/deployment').json()
    assert status['failures'] == failures and status['checks'][0]['blocking'] is False
    assert status['service'] == {'fps': 9.0}
    assert status['diagnosis'] == diagnosis  # rollback FPS comparison reaches the web page
    again = client.post('/api/training/deployments', headers=ADMIN, json={'model_id': single['id']}).json()
    client.post('/api/training/worker/claim', headers=WORKER, json={'worker_id': 'w1'})
    result = {'phase': 'completed', 'active': single['id'], 'evidence': {'first': {'frames': 30, 'detections': 4}}}
    client.post(f'/api/training/worker/jobs/{again["id"]}/finish', headers=WORKER,
                json={'worker_id': 'w1', 'status': 'completed', 'run_info': {'deployment': result}})
    assert {m['id']: m['deployment'] for m in client.get('/api/training/models').json()}[single['id']] == 'active'
    runtime_beat(client, active=single['id'], previous='production-original', phase='completed',
                 active_class_map={'0': 2})
    assert client.get('/api/training/deployment').json()['active_class_map'] == {'0': 2}
    rollback = client.post('/api/training/deployments', headers=ADMIN, json={'action': 'rollback'})
    assert rollback.status_code == 201 and 'class_mode' not in rollback.json()['config']


def test_single_class_model_from_mixed_vehicle_labels_is_not_deployable(training):
    client, app, state = training
    export = make_export(client, state, sessions=4)
    beat(client, started=True)
    job, _ = run_until_claimed(client, app, export, class_mode='single')
    finish_training(client, job['id'])
    with app.state.sessions.begin() as session:  # legacy car/truck/bus export: single_cls merged three classes
        row = session.get(TrainingJob, job['id'])
        row.dataset_summary = {**row.dataset_summary, 'classes': {'0': 'car', '1': 'truck', '2': 'bus'}}
    model = next(m for m in client.get('/api/training/models').json() if m['kind'] == 'finetuned')
    runtime_beat(client)
    rejected = client.post('/api/training/deployments', headers=ADMIN, json={'model_id': model['id']})
    assert rejected.status_code == 422 and 'car-only' in rejected.text


# ----- auto labelling -----------------------------------------------------------------------
PLAN = '/api/training/autolabels/plan'


def capture_at(client, state, hours, camera='first'):
    state['timestamp'] = T0 + timedelta(hours=hours)
    response = client.post('/api/datasets/capture', json={'camera_id': camera})
    assert response.status_code == 201, response.text
    return response.json()


def save_boxes(client, image, boxes, status='in_progress', **more):
    response = client.put(f'/api/datasets/images/{image["id"]}/annotations',
                          json={'revision': image['revision'], 'boxes': boxes, 'status': status, **more})
    assert response.status_code == 200, response.text
    return response.json()


def manual_box(**changes):
    return {'id': str(uuid4()), 'class_id': 0, 'x': 10, 'y': 20, 'width': 40, 'height': 30, 'source': 'manual', **changes}


def test_autolabel_plan_counts_targets_and_names_the_blocker(training):
    client, app, state = training
    assert client.get('/api/datasets/classes').json()['auto_labels_available'] is True
    assert client.get(PLAN).status_code == 422  # model_id required
    plan = client.get(PLAN, params={'model_id': 'nope'}).json()
    assert plan['ready'] is False and plan['blocker'] == 'No auto-label model is available yet.'
    plan = client.get(PLAN, params={'model_id': 'yolo11m-coco'}).json()
    assert plan['blocker'] == 'The training worker is offline.' and plan['max_images'] == 500
    beat(client, started=True)
    plan = client.get(PLAN, params={'model_id': 'yolo11m-coco'}).json()
    assert plan['blocker'] == 'No unlabeled pending images.' and plan['eligible'] == 0
    reviewed, drafted, auto, empty, first, second = (capture_at(client, state, h) for h in range(6))
    other = capture_at(client, state, 9, camera='second')
    save_boxes(client, reviewed, [manual_box()], status='reviewed')
    save_boxes(client, drafted, [manual_box()])
    save_boxes(client, auto, [manual_box(source='auto', score=.5), manual_box(x=60, source='auto', score=.4)])
    save_boxes(client, empty, [], status='reviewed', reviewed_empty=True)
    plan = client.get(PLAN, params={'model_id': 'yolo11m-coco'}).json()
    assert plan['ready'] is True and plan['blocker'] is None
    assert plan['eligible'] == 3 and plan['skipped'] == {'reviewed': 2, 'labeled': 1, 'auto_drafts': 1}
    plan = client.get(PLAN, params={'model_id': 'yolo11m-coco', 'include_auto_drafts': 'true'}).json()
    assert plan['eligible'] == 4 and plan['skipped']['auto_drafts'] == 0
    plan = client.get(PLAN, params={'model_id': 'yolo11m-coco', 'camera_id': 'second'}).json()
    assert plan['eligible'] == 1 and plan['skipped'] == {'reviewed': 0, 'labeled': 0, 'auto_drafts': 0}
    assert client.get(PLAN, params={'model_id': 'yolo11m-coco', 'camera_id': 'bad cam'}).status_code == 422
    # A base model whose weights are missing on the host is not usable.
    client.post('/api/training/worker/heartbeat', headers=WORKER, json={
        'worker_id': 'w1', 'gpu': GPU_FREE, 'base_weights': {'present': False}, 'disk_free_mib': 50000})
    assert client.get(PLAN, params={'model_id': 'yolo11m-coco'}).json()['blocker'] == 'No auto-label model is available yet.'
    beat(client)
    export = make_export(client, state, sessions=2)
    set_fps(app, {'first': 10.0, 'second': 10.0})
    assert create(client, export, acknowledge_limits=True).status_code == 201
    plan = client.get(PLAN, params={'model_id': 'yolo11m-coco'}).json()
    assert plan['blocker'] == 'Another training job is running.' and plan['eligible'] == 3


def test_autolabel_job_applies_drafts_only_to_untouched_images(training):
    client, app, state = training
    beat(client, started=True)
    set_fps(app, {'first': 10.0, 'second': 10.0})
    body = {'model_id': 'yolo11m-coco', 'conf': 0.35}
    assert client.post('/api/training/autolabels', json=body).status_code == 401
    assert client.post('/api/training/autolabels', headers=ADMIN, json=body).status_code == 422  # no pending images
    images = [capture_at(client, state, h) for h in range(3)]
    edited, untouched, skipped = images
    reviewed = capture_at(client, state, 5)
    save_boxes(client, reviewed, [manual_box()], status='reviewed')
    for bad in ({'conf': 0.05}, {'conf': 0.95}, {'conf': 'x'}, {'camera_id': 'bad cam'}, {'extra': 1}):
        assert client.post('/api/training/autolabels', headers=ADMIN, json={**body, **bad}).status_code == 422
    assert client.post('/api/training/autolabels', headers=ADMIN, json={**body, 'model_id': 'nope'}).status_code == 404
    assert client.post('/api/training/autolabels', headers=ADMIN, json={**body, 'camera_id': 'nope'}).status_code == 404
    response = client.post('/api/training/autolabels', headers=ADMIN, json=body)
    assert response.status_code == 201, response.text
    job = response.json()
    assert job['name'] == 'autolabel-YOLO11m COCO (production)' and job['status'] == 'queued' and job['epochs'] == 1
    assert job['config']['kind'] == 'autolabel' and job['config']['images'] == 3 and 'targets' not in job['config']
    assert job['config']['conf'] == 0.35 and job['config']['class_mode'] == 'coco80' and job['export_id'] is None
    assert job['dataset_summary'] == {'kind': 'autolabel', 'camera_id': None, 'model_label': 'YOLO11m COCO (production)',
                                      'conf': 0.35, 'include_auto_drafts': False, 'total': 3, 'processed': 0,
                                      'labeled': 0, 'boxes': 0, 'no_detections': 0, 'skipped_changed': 0}
    assert client.post('/api/training/autolabels', headers=ADMIN, json=body).status_code == 409  # single slot
    assert client.get('/api/training/jobs').json() == []  # hidden from the training history
    assert client.get('/api/training/status').json()['active_job']['id'] == job['id']
    assert client.get(f'/api/training/jobs/{job["id"]}').json()['config']['images'] == 3
    spec = client.post('/api/training/worker/claim', headers=WORKER, json={'worker_id': 'w1'}).json()
    assert spec['id'] == job['id'] and spec['base_model']['kind'] == 'base' and spec['required_free_mib'] == 6000
    assert [t['id'] for t in spec['config']['targets']] == [i['id'] for i in images]
    assert all(t['revision'] == 1 for t in spec['config']['targets'])

    image_url = f'/api/training/worker/jobs/{job["id"]}/images/'
    assert client.get(image_url + untouched['id']).status_code == 401
    download = client.get(image_url + untouched['id'], headers=WORKER)
    assert download.status_code == 200 and download.headers['content-type'] == 'image/jpeg'
    assert download.content.startswith(b'\xff\xd8') and download.headers['cache-control'] == 'no-store'
    assert client.get(image_url + reviewed['id'], headers=WORKER).status_code == 404  # not a target

    # A person labels one target while the worker runs: its frozen revision no longer matches.
    save_boxes(client, edited, [manual_box()])
    results_url = f'/api/training/worker/jobs/{job["id"]}/autolabels'
    results = [
        {'image_id': untouched['id'], 'boxes': [
            {'x': 150, 'y': 80, 'width': 100, 'height': 60, 'score': 0.5},   # clipped to 200x100
            {'x': 10, 'y': 10, 'width': 1, 'height': 30, 'score': 0.9},      # < 2 px wide: dropped
            {'x': -5, 'y': 10, 'width': 30.004, 'height': 20, 'score': 0.87}]},
        {'image_id': skipped['id'], 'boxes': []},
        {'image_id': edited['id'], 'boxes': [{'x': 1, 'y': 1, 'width': 10, 'height': 10, 'score': 0.8}]},
        {'image_id': reviewed['id'], 'boxes': [{'x': 1, 'y': 1, 'width': 10, 'height': 10, 'score': 0.8}]},
    ]
    assert client.post(results_url, headers=WORKER, json={'worker_id': 'other', 'results': results}).status_code == 409
    assert client.post(results_url, headers=WORKER, json={'worker_id': 'w1', 'results': [
        {'image_id': untouched['id'], 'boxes': [{'x': 1, 'y': 1, 'width': 10, 'height': 10, 'score': 1.5}]}]}).status_code == 422
    summary = client.post(results_url, headers=WORKER, json={'worker_id': 'w1', 'results': results}).json()
    assert summary == {'kind': 'autolabel', 'camera_id': None, 'model_label': 'YOLO11m COCO (production)', 'conf': 0.35,
                       'include_auto_drafts': False, 'total': 3, 'processed': 4, 'labeled': 1, 'boxes': 2,
                       'no_detections': 1, 'skipped_changed': 2}
    detail = client.get(f'/api/datasets/images/{untouched["id"]}').json()
    assert detail['status'] == 'in_progress' and detail['revision'] == 2 and detail['auto_box_count'] == 2
    assert [b['score'] for b in detail['boxes']] == [0.87, 0.5]
    assert all(b['source'] == 'auto' and b['class_id'] == 0 and b['id'] for b in detail['boxes'])
    assert detail['boxes'][0]['x'] == 0 and detail['boxes'][0]['width'] == 25
    assert detail['boxes'][1] == {**detail['boxes'][1], 'x': 150, 'y': 80, 'width': 50, 'height': 20}
    assert all(b['x'] + b['width'] <= 200 and b['y'] + b['height'] <= 100 for b in detail['boxes'])
    pending = client.get(f'/api/datasets/images/{skipped["id"]}').json()
    assert pending['status'] == 'pending' and pending['revision'] == 1 and pending['boxes'] == []
    human = client.get(f'/api/datasets/images/{edited["id"]}').json()
    assert human['revision'] == 2 and human['boxes'][0]['source'] == 'manual' and human['auto_box_count'] == 0
    assert client.get(f'/api/datasets/images/{reviewed["id"]}').json()['status'] == 'reviewed'
    # Resending an image is idempotent: its revision moved on, so it is skipped.
    again = client.post(results_url, headers=WORKER, json={'worker_id': 'w1', 'results': results[:1]}).json()
    assert again['skipped_changed'] == 3 and again['labeled'] == 1 and again['processed'] == 5
    assert client.get(f'/api/datasets/images/{untouched["id"]}').json()['revision'] == 2
    detail = client.get(f'/api/training/jobs/{job["id"]}').json()
    assert detail['progress'] == 1.0 and detail['dataset_summary']['labeled'] == 1
    assert detail['message'].startswith('Drafted 1 of 5/3 images')
    beat(client, job={'id': job['id'], 'phase': 'labeling', 'message': 'Labeling images: 3/3'})
    detail = client.get(f'/api/training/jobs/{job["id"]}').json()
    assert detail['progress'] == 1.0 and detail['phase'] == 'labeling'  # heartbeats never reset the count
    assert detail['monitor']['samples']  # production guard runs while labelling

    finish = client.post(f'/api/training/worker/jobs/{job["id"]}/finish', headers=WORKER,
                         json={'worker_id': 'w1', 'status': 'completed', 'run_info': {'kind': 'autolabel', 'images': 3}})
    assert finish.status_code == 200, finish.text
    assert finish.json()['status'] == 'completed' and finish.json()['progress'] == 1.0 and finish.json()['model_id'] is None
    assert client.post(results_url, headers=WORKER, json={'worker_id': 'w1', 'results': results[:1]}).status_code == 409
    assert len(client.get('/api/training/models').json()) == 1  # nothing registered
    assert client.get('/api/training/jobs').json() == []
    assert client.get('/api/training/status').json()['active_job'] is None

    # The draft stays out of exports until a person reviews it; auto boxes then export like any box.
    plan = client.get(PLAN, params={'model_id': 'yolo11m-coco'}).json()
    assert plan['eligible'] == 1 and plan['skipped'] == {'reviewed': 1, 'labeled': 1, 'auto_drafts': 1}
    assert client.get(PLAN, params={'model_id': 'yolo11m-coco', 'include_auto_drafts': 'true'}).json()['eligible'] == 2
    draft = client.get(f'/api/datasets/images/{untouched["id"]}').json()
    kept = [{**draft['boxes'][0]}, {**draft['boxes'][1], 'x': 140, 'source': 'manual'}]
    kept[1].pop('score')
    saved = save_boxes(client, draft, kept, status='reviewed')
    assert saved['auto_box_count'] == 1 and saved['boxes'][0]['score'] == 0.87 and 'score' not in saved['boxes'][1]
    export = client.post('/api/datasets/exports', json={'seed': 42}).json()
    for _ in range(200):
        current = next(e for e in client.get('/api/datasets/exports').json() if e['id'] == export['id'])
        if current['status'] != 'building':
            break
        time.sleep(.02)
    assert current['status'] == 'ready', current
    archive = zipfile.ZipFile(io.BytesIO(client.get(f'/api/datasets/exports/{export["id"]}/download').content))
    label = next(n for n in archive.namelist() if n.endswith(f'{untouched["id"]}.txt'))
    lines = archive.read(label).decode().splitlines()
    assert len(lines) == 2 and all(line.startswith('0 ') for line in lines)


def test_autolabel_stop_failure_and_worker_loss(training):
    client, app, state = training
    beat(client, started=True)
    set_fps(app, {'first': 10.0, 'second': 10.0})
    image = capture_at(client, state, 0)
    job = client.post('/api/training/autolabels', headers=ADMIN, json={'model_id': 'yolo11m-coco', 'include_auto_drafts': True}).json()
    assert job['config']['include_auto_drafts'] is True
    client.post('/api/training/worker/claim', headers=WORKER, json={'worker_id': 'w1'})
    stopped = client.post(f'/api/training/jobs/{job["id"]}/stop', headers=ADMIN).json()
    assert stopped['status'] == 'stopping'
    # Results that arrive while stopping are still applied (the worker flushes finished batches).
    summary = client.post(f'/api/training/worker/jobs/{job["id"]}/autolabels', headers=WORKER, json={
        'worker_id': 'w1', 'results': [{'image_id': image['id'], 'boxes': [{'x': 1, 'y': 1, 'width': 10, 'height': 10, 'score': .6}]}]}).json()
    assert summary['labeled'] == 1
    finish = client.post(f'/api/training/worker/jobs/{job["id"]}/finish', headers=WORKER,
                         json={'worker_id': 'w1', 'status': 'stopped', 'error': 'Stopped by the user'}).json()
    assert finish['status'] == 'stopped' and finish['progress'] == 1.0
    assert client.get(f'/api/datasets/images/{image["id"]}').json()['status'] == 'in_progress'
    # Existing untouched drafts are re-labelled only with include_auto_drafts; a failure keeps them.
    job = client.post('/api/training/autolabels', headers=ADMIN, json={'model_id': 'yolo11m-coco', 'include_auto_drafts': True}).json()
    assert job['config']['images'] == 1
    client.post('/api/training/worker/claim', headers=WORKER, json={'worker_id': 'w1'})
    finish = client.post(f'/api/training/worker/jobs/{job["id"]}/finish', headers=WORKER,
                         json={'worker_id': 'w1', 'status': 'failed', 'error_code': 'autolabel_failed', 'error': 'boom'}).json()
    assert finish['status'] == 'failed' and finish['error_code'] == 'autolabel_failed'
    assert client.get(f'/api/datasets/images/{image["id"]}').json()['revision'] == 2
    assert client.post('/api/training/autolabels', headers=ADMIN, json={'model_id': 'yolo11m-coco'}).status_code == 422
    job = client.post('/api/training/autolabels', headers=ADMIN, json={'model_id': 'yolo11m-coco', 'include_auto_drafts': True}).json()
    client.post('/api/training/worker/claim', headers=WORKER, json={'worker_id': 'w1'})
    with app.state.sessions.begin() as session:
        session.get(TrainingJob, job['id']).heartbeat_at = datetime.now(timezone.utc) - timedelta(hours=1)
    app.state.training.started -= timedelta(hours=1)
    assert client.get(f'/api/training/jobs/{job["id"]}').json()['error_code'] == 'worker_lost'
