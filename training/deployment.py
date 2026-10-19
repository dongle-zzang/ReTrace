"""Deployment tasks reuse training queue exclusivity and authenticated weights downloads.

The worker only exports/validates the ONNX and writes a leased request; the
DeepStream runtime (vehicle_deployment.py) builds, applies, verifies and rolls back.
"""
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vehicle_deployment import DIRECTORY, TERMINAL, public_status, read_json, write_json  # noqa: E402

TIMEOUT_SECONDS = 2400  # runtime build limit 1800 s + apply/verify (90+20+60 s) + rollback windows


def run_deployment(worker, spec, job_dir, job, error):
    """error is the worker's WorkerError (worker.py usually runs as __main__)."""
    state = read_json(DIRECTORY / 'status.json', {})
    if not public_status(DIRECTORY)['available']:
        raise error('deployment_unavailable', 'DeepStream deployment is offline or disabled (VEHICLE_DEPLOYMENT_ENABLED).')
    action = spec['config']['action']
    worker.wait_for_gpu(spec, job)
    if action == 'deploy':
        weights = worker.resolve_weights(spec, job_dir)
        cmd = [sys.executable, str(Path(__file__).resolve().parent / 'deploy_export.py'), '--weights', str(weights),
               '--job', spec['id'], '--model', spec['base_model']['id'], '--sha256', spec['base_model']['sha256'],
               '--classes', spec['config']['class_mode'], '--imgsz', str(spec['config']['imgsz'])]
        code, stopped = worker.run_child(cmd, job_dir, job, 'exporting', grace=10)
        if stopped or code:
            raise error('export_failed', 'ONNX export or compatibility check failed or was stopped; the running '
                                         'model was not changed. See the job log.')
    current = read_json(DIRECTORY / 'status.json', {})
    if current.get('session') != state.get('session') or not public_status(DIRECTORY)['available']:
        raise error('runtime_changed', 'DeepStream restarted while preparing the deployment. Retry.')
    path = DIRECTORY / spec['id'] / 'request.json'
    request = {'id': spec['id'], 'model_id': spec['base_model']['id'], 'action': action,
               'session': state['session'], 'contract': state['contract'], 'lease_at': time.time()}
    write_json(path, request)
    start = time.monotonic()
    try:
        while True:
            result = read_json(path.parent / 'result.json')
            if result and result.get('phase') in TERMINAL:
                if result['phase'] == 'completed':
                    return {'status': 'completed', 'run_info': {'deployment': result}}
                return {'status': 'failed', 'error_code': 'deployment_' + result['phase'],
                        'error': f"{result['phase']}: {result.get('error') or 'unknown'}",
                        'run_info': {'deployment': result}}
            runtime = read_json(DIRECTORY / 'status.json', {})
            # No message: the web shows the runtime phase (building, verifying, rolling_back, ...).
            reply = worker.heartbeat({**job, 'phase': runtime.get('phase') or 'deploying', 'message': None})
            request['cancel'] = bool(request.get('cancel') or reply.get('stop') or worker.shutdown)
            request['lease_at'] = time.time()
            write_json(path, request)
            if runtime.get('session') != state['session'] or not public_status(DIRECTORY)['runtime_online']:
                raise error('runtime_lost', 'DeepStream went offline; it loads the last committed model on restart.')
            if time.monotonic() - start > TIMEOUT_SECONDS:
                raise error('deployment_timeout', 'Deployment timed out; the runtime restores the committed model.')
            time.sleep(worker.args.poll)
    finally:
        # Also on HTTP/storage failures; lease expiry independently triggers runtime recovery.
        if not (path.parent / 'result.json').exists():
            request.update(cancel=True, lease_at=time.time())
            write_json(path, request)
