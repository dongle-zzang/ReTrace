"""Web-managed YOLO11m fine-tuning: jobs, model registry and the GPU worker API.

The Backend never trains, imports torch or touches the GPU. It validates the request, freezes
the selected dataset export as an immutable snapshot, stores the job in PostgreSQL and hands it
to the separate GPU worker (training/worker.py, run on the GPU host with the existing training
venv). The worker reports progress/GPU samples with a heartbeat; the Backend compares the live
Preview camera FPS against a pre-training baseline and asks the worker to stop when production
inference suffers. Contract and runbook: docs/training.md.

Access: read-only GETs follow the existing dashboard policy (no login exists); starting,
stopping and downloading require TRAINING_ADMIN_TOKEN, the worker API requires
TRAINING_WORKER_TOKEN, and Preview never forwards /api/training/worker/*.
"""
from contextlib import suppress
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import threading
from typing import Literal
from uuid import UUID, uuid4
import zipfile

import yaml
from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .db import Camera, CameraStatus
from .dataset import aware
from .dataset_models import DatasetExport, DatasetImage
from .ingest import status_out
from .training_models import ACTIVE_STATUSES, TrainingJob, TrainingModel

router = APIRouter(prefix='/api/training')

BASE_MODEL_ID = 'yolo11m-coco'
SPLITS = ('train', 'val', 'test')
# Dataset class IDs (never COCO IDs): new exports are car-only, legacy ZIPs car/truck/bus.
ACCEPTED_CLASS_MAPS = ({0: 'car'}, {0: 'car', 1: 'truck', 2: 'bus'})
CLASS_MODES = {
    'vehicle3': 'car/truck/bus (default): dataset classes are mapped by name to the pretrained '
                'COCO head (car 2, truck 7, bus 5); production 80-label layout is kept.',
    'single': 'one vehicle class (Ultralytics single_cls); new 1-class head, original labels untouched.',
}
TRAIN_MODES = {
    'standard': 'Train / Val / Test (default): train split trains the weights, val picks the best epoch and stops '
                'early, test is compared with the current YOLO11m.',
    'all': 'Train All: train, val and test images all train the weights (separate training copy; the export and '
           'its splits are unchanged). Fixed epoch count, no early stopping, final-epoch weights. No independent '
           'score in this run: evaluate on newly captured CCTV images afterwards.',
}
MIN_EVAL_IMAGES = 10
MAX_EVAL_IMAGES = 5000
# Auto-label jobs (config.kind = 'autolabel'): pending images get draft boxes from a registered model.
AUTOLABEL_MAX_IMAGES = 500
AUTOLABEL_MAX_BOXES = 300
AUTOLABEL_MIN_BOX_PX = 2
AUTOLABEL_BATCH = 50
# Exact sentences: the frontend maps them to translations.
NO_AUTOLABEL_MODEL = 'No auto-label model is available yet.'
WORKER_OFFLINE = 'The training worker is offline.'
ANOTHER_JOB_RUNNING = 'Another training job is running.'
NO_PENDING_IMAGES = 'No unlabeled pending images.'
CAMERA_ID = r'^[A-Za-z0-9_-]+$'
# Archive inspection limits (exports hold at most 5000 images + labels).
MAX_MEMBERS = 12_000
MAX_MEMBER_BYTES = 64 * 2**20
MAX_TOTAL_BYTES = 40 * 2**30
MAX_LABEL_BYTES = 1 * 2**20
# Artifacts the worker may store, with size caps. Model files are octet-stream downloads.
ARTIFACT_LIMITS = {
    'best.pt': 1 * 2**30, 'last.pt': 1 * 2**30, 'results.csv': 8 * 2**20, 'results.png': 16 * 2**20,
    'args.yaml': 1 * 2**20, 'run_info.json': 4 * 2**20, 'train.log': 64 * 2**20,
    'check_report.json': 32 * 2**20, 'eval_metrics.json': 32 * 2**20, 'eval_predictions.json': 64 * 2**20,
    'eval_report.md': 4 * 2**20,
}
LOG_LINES = 400
MONITOR_SAMPLES = 720
SAFE_PART = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}')


def utcnow():
    return datetime.now(timezone.utc)


def service(request):
    return request.app.state.training


def bearer_ok(request, token):
    header = request.headers.get('authorization', '')
    return bool(token) and hmac.compare_digest(header.encode(), f'Bearer {token}'.encode())


def require_admin(request):
    token = request.app.state.config.training_admin_token
    if not token:
        raise HTTPException(503, 'Training control is disabled. Set TRAINING_ADMIN_TOKEN on the Backend to enable it.')
    if not bearer_ok(request, token):
        raise HTTPException(401, 'Training admin token required or invalid.')


def require_worker(request):
    token = request.app.state.config.training_worker_token
    if not token:
        raise HTTPException(503, 'Training worker API is disabled (TRAINING_WORKER_TOKEN is not set).')
    if not bearer_ok(request, token):
        raise HTTPException(401, 'Invalid worker token.')


def estimate_vram_mib(imgsz, batch):
    # Fitted to training/README.md §2 (torch reserved: 640/b4 ~3.3 GiB, 960/b4 ~7.2 GiB);
    # scales with batch and image area. An estimate only: real use depends on the data.
    return round(150 + 800 * batch * (imgsz / 640) ** 2)


def required_free_mib(config, imgsz, batch):
    return max(config.training_min_free_mib, estimate_vram_mib(imgsz, batch) + config.training_gpu_reserve_mib)


def allowed_member(name):
    """Mirror of training/worker.py: only the export layout, optionally in one wrapping folder."""
    if '\\' in name or name.startswith('/') or '\0' in name:
        return False
    parts = list(PurePosixPath(name).parts)
    if not parts or any(not SAFE_PART.fullmatch(part) for part in parts):
        return False
    if parts[0] not in ('images', 'labels', 'data.yaml', 'manifest.json'):
        parts = parts[1:]
    if not parts:
        return name.endswith('/')
    if parts in (['data.yaml'], ['manifest.json']):
        return not name.endswith('/')
    if parts[0] not in ('images', 'labels') or len(parts) > 3 or (len(parts) > 1 and parts[1] not in SPLITS):
        return False
    if len(parts) < 3:
        return name.endswith('/')
    is_label = parts[2].lower().endswith('.txt')
    return (is_label == (parts[0] == 'labels') and not name.endswith('/')
            and parts[2].lower().rsplit('.', 1)[-1] in ('jpg', 'jpeg', 'png', 'txt'))


def inspect_archive(path):
    """Training suitability of an export ZIP: structure, labels, classes, sessions and size.

    errors   -> training blocked (broken data, no train/val, leakage, class mismatch)
    limits   -> training allowed only after explicit acknowledgement; results are not conclusive
    readiness = blocked | limited | ready
    """
    errors, limits, notes = [], [], []
    result = {'errors': errors, 'limits': limits, 'notes': notes}
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile):
        errors.append('The dataset ZIP is missing or unreadable.')
        result['readiness'] = 'blocked'
        return result
    with archive:
        members = archive.infolist()
        if len(members) > MAX_MEMBERS:
            errors.append('The dataset archive has too many files.')
        total = sum(member.file_size for member in members)
        unsafe = [m.filename for m in members if not allowed_member(m.filename)]
        if unsafe:
            errors.append(f'Unexpected or unsafe path in the archive: {unsafe[0][:100]!r}')
        if total > MAX_TOTAL_BYTES or any(m.file_size > MAX_MEMBER_BYTES for m in members):
            errors.append('The dataset archive exceeds the size limits.')
        if any(m.compress_size and m.file_size / m.compress_size > 200 and m.file_size > 2**20 for m in members):
            errors.append('The dataset archive has a suspicious compression ratio.')
        if errors:
            result['readiness'] = 'blocked'
            return result
        files = {m.filename: m for m in members if not m.is_dir()}
        prefix = ''
        if 'data.yaml' not in files:
            wrapped = [name[:-len('data.yaml')] for name in files if name.endswith('/data.yaml') and name.count('/') == 1]
            prefix = wrapped[0] if len(wrapped) == 1 else ''

        def read(name, limit):
            member = files.get(prefix + name)
            if member is None or member.file_size > limit:
                return None
            return archive.read(member)

        raw_yaml, raw_manifest = read('data.yaml', 65536), read('manifest.json', 32 * 2**20)
        if raw_yaml is None or raw_manifest is None:
            errors.append('data.yaml or manifest.json is missing from the dataset archive.')
            result['readiness'] = 'blocked'
            return result
        try:
            data_yaml = yaml.safe_load(raw_yaml) or {}
            names = data_yaml.get('names')
            names = dict(enumerate(names)) if isinstance(names, list) else {int(k): str(v) for k, v in (names or {}).items()}
            manifest = json.loads(raw_manifest)
        except (ValueError, TypeError, AttributeError, yaml.YAMLError):
            errors.append('data.yaml or manifest.json is not valid.')
            result['readiness'] = 'blocked'
            return result
        result['classes'] = names
        if names not in ACCEPTED_CLASS_MAPS:
            errors.append(f'Unexpected dataset classes {names}; expected 0 car (or legacy 0 car / 1 truck / 2 bus).')
        nc = len(names)
        stems = {split: {} for split in SPLITS}
        for name in files:
            relative = name[len(prefix):]
            parts = relative.split('/')
            if len(parts) == 3 and parts[0] in ('images', 'labels'):
                stems[parts[1]].setdefault(parts[2].rsplit('.', 1)[0], set()).add(parts[0])
        counts, boxes, empty, tiny = {}, {}, {}, 0
        per_class = {split: {} for split in SPLITS}
        for split in SPLITS:
            counts[split], boxes[split], empty[split] = 0, 0, 0
            for stem, kinds in sorted(stems[split].items()):
                if kinds != {'images', 'labels'}:
                    errors.append(f'{split}/{stem}: image and label file must both exist.')
                    continue
                counts[split] += 1
                raw = read(f'labels/{split}/{stem}.txt', MAX_LABEL_BYTES)
                if raw is None:
                    errors.append(f'{split}/{stem}.txt: label file is too large.')
                    continue
                lines = [line for line in raw.decode('utf-8', 'replace').splitlines() if line.strip()]
                if not lines:
                    empty[split] += 1
                for number, line in enumerate(lines, 1):
                    fields = line.split()
                    try:
                        cls, xc, yc, w, h = int(fields[0]), *map(float, fields[1:5])
                        if len(fields) != 5:
                            raise ValueError
                    except (ValueError, IndexError):
                        errors.append(f'{split}/{stem}.txt:{number}: malformed label line.')
                        continue
                    if not 0 <= cls < nc:
                        errors.append(f'{split}/{stem}.txt:{number}: class {cls} is not in the dataset classes.')
                        continue
                    eps = 1e-6
                    if (w <= 0 or h <= 0 or xc - w / 2 < -eps or yc - h / 2 < -eps
                            or xc + w / 2 > 1 + eps or yc + h / 2 > 1 + eps):
                        errors.append(f'{split}/{stem}.txt:{number}: box coordinates are outside the image.')
                        continue
                    if w * h < 1e-5:
                        tiny += 1
                    boxes[split] += 1
                    label = names.get(cls, str(cls))
                    per_class[split][label] = per_class[split].get(label, 0) + 1
        del errors[20:]  # keep the response readable; the worker's check_report has everything
        images = manifest.get('images') or []
        sessions = {split: set() for split in SPLITS}
        cameras = {split: set() for split in SPLITS}
        session_splits = {}
        for entry in images:
            split = entry.get('split')
            if split in SPLITS:
                sessions[split].add(entry.get('session'))
                cameras[split].add(entry.get('camera_id'))
                session_splits.setdefault(entry.get('session'), set()).add(split)
        leaks = [session for session, splits in session_splits.items() if len(splits) > 1]
        if leaks:
            errors.append(f'{len(leaks)} capture session(s) appear in more than one split (train/val leakage).')
        if (manifest.get('counts') or {}) and any(manifest['counts'].get(s) != counts[s] for s in SPLITS):
            errors.append('manifest.json counts do not match the image/label files.')
        if counts['train'] == 0:
            errors.append('No training images.')
        elif boxes['train'] == 0:
            errors.append('Training images contain no vehicle labels.')
        # Train All (every split trains the weights) is judged on all images together; it never
        # has an independent check, which is always a limitation the user must confirm.
        total_images, total_boxes = sum(counts.values()), sum(boxes.values())
        all_limits = ['Train All uses every image (train, val and test) for training: this run has no independent '
                      'validation or test. Measure the model on newly captured CCTV images afterwards.']
        all_sessions = set().union(*sessions.values())
        if len(all_sessions) < 3:
            all_limits.append(f'All images come from only {len(all_sessions)} capture session(s).')
        if total_boxes < 100:
            all_limits.append(f'Only {total_boxes} vehicle boxes in total; fine-tuning may overfit.')
        if counts['val'] == 0:
            errors.append('No validation images: the export has no independent validation session. '
                          'Capture more sessions (other times/days) and create a new export.')
        if counts['val'] and counts['val'] < MIN_EVAL_IMAGES:
            limits.append(f'Validation has only {counts["val"]} images; model selection is unreliable.')
        if counts['val'] and len(sessions['val']) < 2:
            limits.append('Validation comes from a single capture session.')
        if counts['test'] == 0:
            limits.append('No test images: the new model cannot be compared with the current YOLO11m.')
        elif counts['test'] < MIN_EVAL_IMAGES or len(sessions['test']) < 2:
            limits.append(f'Test has {counts["test"]} images from {len(sessions["test"])} session(s); '
                          'improvement cannot be confirmed.')
        if len(sessions['train']) < 3:
            limits.append(f'Training images come from only {len(sessions["train"])} capture session(s).')
        if boxes['train'] and boxes['train'] < 100:
            limits.append(f'Only {boxes["train"]} training boxes; fine-tuning may overfit.')
        shared = []
        if total_images and sum(empty.values()) / total_images > 0.5:
            shared.append('More than half of the images have no vehicles; confirm they are truly empty.')
        if boxes['train'] and tiny / max(sum(boxes.values()), 1) > 0.1:
            shared.append('Many boxes are tiny; check the labels.')
        limits.extend(shared)
        all_limits.extend(shared)
        limits.extend(f'Export: {warning}' for warning in manifest.get('warnings') or [] if isinstance(warning, str))
        absent = [label for label in names.values() if not any(per_class[s].get(label) for s in SPLITS)]
        if absent:
            notes.append(f'No labels for: {", ".join(absent)}.')
        if names == ACCEPTED_CLASS_MAPS[0]:
            notes.append('Car-only export: in car/truck/bus mode only the car class is trained; '
                         'truck/bus keep their pretrained weights.')
        totals = {}
        for split in SPLITS:
            for label, value in per_class[split].items():
                totals[label] = totals.get(label, 0) + value
        result.update(counts={**counts, 'total': total_images}, boxes={**boxes, 'total': sum(boxes.values())},
                      labels_per_class=per_class, labels_per_class_total=totals, empty_images=empty,
                      sessions={s: len(sessions[s]) for s in SPLITS}, cameras={s: len(cameras[s]) for s in SPLITS},
                      seed=manifest.get('seed'))
    # Top-level errors/limits/readiness describe the standard Train/Val/Test run (unchanged contract).
    result['readiness'] = 'blocked' if errors else 'limited' if limits else 'ready'
    if 'counts' in result:
        all_errors = [e for e in errors if not e.startswith('No validation images')]
        result['modes'] = {
            'standard': {'errors': list(errors), 'limits': list(limits), 'readiness': result['readiness']},
            'all': {'errors': all_errors, 'limits': all_limits,
                    'readiness': 'blocked' if all_errors else 'limited' if all_limits else 'ready'},
        }
    return result


def for_mode(check, train_mode):
    """The dataset check as seen by one training mode (top level = standard)."""
    mode = (check.get('modes') or {}).get(train_mode)
    return {**check, **mode, 'train_mode': train_mode} if mode else {**check, 'train_mode': train_mode}


def metric_point(row, train_all=False):
    """Ultralytics results.csv row -> stable JSON for the chart/table.

    Train All validates (if at all) on its own training images, so validation losses and
    P/R/mAP are dropped: they are training fit and must never read as an independent score.
    """
    def value(key):
        if train_all and (key.startswith('val/') or key.startswith('metrics/')):
            return None
        v = row.get(key)
        return float(v) if isinstance(v, (int, float)) else None

    def total(prefix):
        parts = [value(f'{prefix}/{k}') for k in ('box_loss', 'cls_loss', 'dfl_loss')]
        return round(sum(p for p in parts if p is not None), 6) if any(p is not None for p in parts) else None
    return {'epoch': int(row.get('epoch', 0)), 'time': value('time'),
            'train_loss': total('train'), 'val_loss': total('val'),
            'train_box_loss': value('train/box_loss'), 'train_cls_loss': value('train/cls_loss'),
            'train_dfl_loss': value('train/dfl_loss'), 'val_box_loss': value('val/box_loss'),
            'val_cls_loss': value('val/cls_loss'), 'val_dfl_loss': value('val/dfl_loss'),
            'precision': value('metrics/precision(B)'), 'recall': value('metrics/recall(B)'),
            'map50': value('metrics/mAP50(B)'), 'map50_95': value('metrics/mAP50-95(B)')}


def best_point(points):
    scored = [p for p in points if p.get('map50_95') is not None]
    # Ultralytics fitness = mAP50-95 (8.4); best.pt is the epoch with the highest one.
    return max(scored, key=lambda p: p['map50_95']) if scored else (points[-1] if points else None)


def evaluation_summary(evaluation, dataset_summary):
    """Attach a 'conclusive' flag: improvement is only claimed with enough independent test data."""
    if not evaluation or evaluation.get('status') != 'completed':
        return evaluation
    metrics = evaluation.get('metrics') or {}
    caveats = list(metrics.get('dataset_warnings') or [])
    images = int(metrics.get('images') or 0)
    test_sessions = (dataset_summary.get('sessions') or {}).get('test', 0)
    overlap = int(dataset_summary.get('test_overlap') or 0)
    if images < MIN_EVAL_IMAGES:
        caveats.append(f'Only {images} test images; differences are not statistically meaningful.')
    if test_sessions < 2:
        caveats.append('Test images come from fewer than 2 capture sessions.')
    if overlap:
        caveats.append(f'{overlap} test image(s) were already used to train the base model of this run; '
                       'the test is not independent.')
    if dataset_summary.get('kind') == 'evaluate':
        caveats.append('Fixed cameras: new frames can still resemble training frames; prefer images from other days, '
                       'times and weather.')
    caveats.append('Offline Ultralytics inference at the training image size; not a DeepStream/TensorRT measurement.')
    conclusive = (images >= MIN_EVAL_IMAGES and test_sessions >= 2 and not overlap
                  and not metrics.get('dataset_warnings'))
    return {**evaluation, 'conclusive': conclusive, 'caveats': caveats}


def public_config(config):
    """Job config for the web: evaluation/auto-label image lists are replaced by their size."""
    config = dict(config or {})
    ids = config.pop('eval_image_ids', None)
    if ids is not None:
        config['eval_images'] = len(ids)
    targets = config.pop('targets', None)
    if targets is not None:
        config['images'] = len(targets)
    config.setdefault('kind', 'train')
    if config['kind'] == 'train':
        config.setdefault('train_mode', 'standard')
    return config


def is_train_all(job):
    return (job.config or {}).get('train_mode') == 'all'


def job_kind(job):
    return (job.config or {}).get('kind', 'train')


# ----- auto labelling -------------------------------------------------------------------
def auto_draft_only(image):
    """At least one box and every box is an untouched auto-label draft."""
    boxes = image.boxes or []
    return bool(boxes) and all(isinstance(box, dict) and box.get('source') == 'auto' for box in boxes)


def autolabel_eligible(image, include_auto_drafts):
    """Only images nobody labelled: pending without boxes, optionally earlier untouched auto drafts.

    Reviewed images, explicit empty reviews and anything with a manual box are never touched.
    """
    if image.status == 'reviewed' or image.reviewed_empty:
        return False
    if image.status == 'pending':
        return not image.boxes
    return bool(include_auto_drafts) and image.status == 'in_progress' and auto_draft_only(image)


def autolabel_plan(session, camera_id, include_auto_drafts):
    """Eligible images (oldest first) and why the others are skipped, within one camera or all."""
    query = select(DatasetImage).order_by(DatasetImage.captured_at, DatasetImage.id)
    if camera_id:
        query = query.where(DatasetImage.camera_id == camera_id)
    eligible, skipped = [], {'reviewed': 0, 'labeled': 0, 'auto_drafts': 0}
    for image in session.scalars(query):
        if autolabel_eligible(image, include_auto_drafts):
            eligible.append(image)
        elif image.status == 'reviewed':
            skipped['reviewed'] += 1
        elif image.status == 'in_progress' and auto_draft_only(image):
            skipped['auto_drafts'] += 1
        else:
            skipped['labeled'] += 1
    return eligible, skipped


def fit_extent(start, length, limit):
    """Largest length (float arithmetic included) with start + length <= limit."""
    while start + length > limit and length > 0:
        length = math.nextafter(length, 0)
    return length


def clip_auto_boxes(boxes, width, height):
    """Worker detections -> stored auto boxes: clipped to the image, tiny ones dropped, top scores first."""
    kept = []
    for box in boxes:
        x1, y1 = min(max(0.0, round(box.x, 2)), float(width)), min(max(0.0, round(box.y, 2)), float(height))
        x2 = min(float(width), round(box.x + box.width, 2))
        y2 = min(float(height), round(box.y + box.height, 2))
        w, h = fit_extent(x1, round(x2 - x1, 2), width), fit_extent(y1, round(y2 - y1, 2), height)
        if w < AUTOLABEL_MIN_BOX_PX or h < AUTOLABEL_MIN_BOX_PX:
            continue
        kept.append({'id': str(uuid4()), 'class_id': 0, 'x': x1, 'y': y1, 'width': w, 'height': h,
                     'source': 'auto', 'score': round(box.score, 3)})
    kept.sort(key=lambda box: -box['score'])
    return kept[:AUTOLABEL_MAX_BOXES]


def job_out(job, detail=False):
    data = {'id': job.id, 'name': job.name, 'status': job.status, 'phase': job.phase, 'message': job.message,
            'export_id': job.export_id or None, 'dataset_sha256': job.dataset_sha256 or None,
            'base_model_id': job.base_model_id,
            'config': public_config(job.config), 'created_at': aware(job.created_at).isoformat(),
            'started_at': aware(job.started_at).isoformat() if job.started_at else None,
            'finished_at': aware(job.finished_at).isoformat() if job.finished_at else None,
            'heartbeat_at': aware(job.heartbeat_at).isoformat() if job.heartbeat_at else None,
            'epoch': job.epoch, 'epochs': job.epochs, 'progress': job.progress,
            'latest': (job.metrics or [None])[-1], 'best': best_point(job.metrics or []),
            'gpu': job.gpu, 'stop_requested': job.stop_requested, 'stop_reason': job.stop_reason,
            'error_code': job.error_code, 'error': job.error, 'model_id': job.model_id,
            'elapsed_seconds': elapsed(job), 'dataset_summary': job.dataset_summary}
    if detail:
        data.update(metrics=job.metrics or [], live=job.live, monitor=job.monitor, log_tail=job.log_tail or [],
                    run_info=job.run_info, evaluation=job.evaluation,
                    artifacts={name: {**meta, 'download_url': f'/api/training/jobs/{job.id}/artifacts/{name}'}
                               for name, meta in (job.artifacts or {}).items()},
                    has_comparison='eval_predictions.json' in (job.artifacts or {}))
    return data


def elapsed(job):
    if not job.started_at:
        return 0
    end = aware(job.finished_at) if job.finished_at else utcnow()
    return max(0, round((end - aware(job.started_at)).total_seconds()))


def model_out(model):
    return {'id': model.id, 'name': model.name, 'version': model.version, 'kind': model.kind,
            'label': f'{model.name} v{model.version}' if model.kind == 'finetuned' else model.name,
            'base_model_id': model.base_model_id, 'job_id': model.job_id, 'export_id': model.export_id,
            'dataset_sha256': model.dataset_sha256, 'class_mode': model.class_mode, 'names_count': len(model.names or {}),
            'config': model.config, 'metrics': model.metrics, 'evaluation_conclusive': model.evaluation_conclusive,
            'files': {name: {'size': meta.get('size'), 'sha256': meta.get('sha256'), 'location': meta.get('location'),
                             'download_url': f'/api/training/models/{model.id}/files/{name.split(".")[0]}'
                             if name in ('best.pt', 'last.pt') else None}
                      for name, meta in (model.files or {}).items()},
            'created_at': aware(model.created_at).isoformat(), 'deployment': model.deployment,
            'status': 'available' if model.kind == 'base' or 'best.pt' in (model.files or {}) else 'incomplete'}


def attach_new_image_evaluation(session, job):
    """Keep the latest new-CCTV-image results on the evaluated model (registry stays the source)."""
    model = session.get(TrainingModel, job.base_model_id, with_for_update=True)
    if model is None:
        return
    evaluation = job.evaluation or {}
    metrics = evaluation.get('metrics') or {}
    models = metrics.get('models') or {}
    summary = job.dataset_summary or {}
    entry = {'job_id': job.id, 'export_id': job.export_id, 'images': metrics.get('images'),
             'gt_boxes': metrics.get('gt_boxes'), 'sessions': (summary.get('sessions') or {}).get('test'),
             'cameras': (summary.get('cameras') or {}).get('test'),
             'excluded': summary.get('excluded'), 'conclusive': bool(evaluation.get('conclusive')),
             'new': {k: (models.get('new') or {}).get(k) for k in ('vehicle', 'operating_point')},
             'baseline': {k: (models.get('baseline') or {}).get(k) for k in ('vehicle', 'operating_point')},
             'finished_at': aware(job.finished_at).isoformat() if job.finished_at else None}
    current = dict(model.metrics or {})
    current['new_images'] = [*(current.get('new_images') or []), entry][-10:]
    model.metrics = current  # new object: JSON columns only track reassignment


class JobCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    export_id: UUID
    base_model_id: str = Field(default=BASE_MODEL_ID, min_length=1, max_length=64, pattern=r'^[A-Za-z0-9_.-]+$')
    name: str = Field(default='yolo11m-ft', min_length=1, max_length=48, pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]*$')
    class_mode: Literal['vehicle3', 'single'] = 'vehicle3'
    train_mode: Literal['standard', 'all'] = 'standard'
    epochs: int = Field(default=50, ge=1, le=300, strict=True)
    imgsz: Literal[640, 960] = 640
    batch: int = Field(default=4, ge=1, le=32, strict=True)
    seed: int = Field(default=42, ge=0, le=2147483647, strict=True)
    patience: int = Field(default=15, ge=1, le=300, strict=True)
    device: Literal['gpu'] = 'gpu'
    smoke: bool = False
    acknowledge_limits: bool = False


class EvaluationCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model_id: str = Field(min_length=1, max_length=64, pattern=r'^[A-Za-z0-9_.-]+$')
    export_id: UUID
    acknowledge_limits: bool = False


class AutolabelCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model_id: str = Field(min_length=1, max_length=64, pattern=r'^[A-Za-z0-9_.-]+$')
    camera_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=CAMERA_ID)
    conf: float = Field(default=0.35, ge=0.1, le=0.9, allow_inf_nan=False)
    include_auto_drafts: bool = False


class AutolabelBox(BaseModel):
    model_config = ConfigDict(extra='forbid')
    x: float = Field(allow_inf_nan=False)
    y: float = Field(allow_inf_nan=False)
    width: float = Field(allow_inf_nan=False)
    height: float = Field(allow_inf_nan=False)
    score: float = Field(ge=0, le=1, allow_inf_nan=False)


class AutolabelResult(BaseModel):
    model_config = ConfigDict(extra='forbid')
    image_id: UUID
    boxes: list[AutolabelBox] = Field(max_length=AUTOLABEL_MAX_BOXES)


class AutolabelResults(BaseModel):
    model_config = ConfigDict(extra='ignore')
    worker_id: str = Field(min_length=1, max_length=96)
    results: list[AutolabelResult] = Field(max_length=AUTOLABEL_BATCH)


class Beat(BaseModel):
    model_config = ConfigDict(extra='ignore')
    worker_id: str = Field(min_length=1, max_length=96)
    started: bool = False
    gpu: dict = Field(default_factory=dict)
    base_weights: dict = Field(default_factory=dict)
    disk_free_mib: int | None = None
    job: dict | None = None
    deployment: dict = Field(default_factory=dict)


class Claim(BaseModel):
    model_config = ConfigDict(extra='ignore')
    worker_id: str = Field(min_length=1, max_length=96)


class Finish(BaseModel):
    model_config = ConfigDict(extra='ignore')
    worker_id: str = Field(min_length=1, max_length=96)
    status: Literal['completed', 'failed', 'stopped']
    error_code: str | None = Field(default=None, max_length=40)
    error: str | None = Field(default=None, max_length=2000)
    metrics_rows: list[dict] = Field(default_factory=list, max_length=1000)
    run_info: dict | None = None
    evaluation: dict | None = None
    dataset_report: dict | None = None
    gpu_before: dict | None = None


class TrainingService:
    def __init__(self, settings, sessions, poller):
        self.settings, self.sessions, self.poller = settings, sessions, poller
        self.root = (Path(settings.dataset_dir) / 'training').resolve()
        self.dataset_root = Path(settings.dataset_dir).resolve()
        self.worker = None  # last heartbeat (memory only; the worker beats every few seconds)
        self.worker_lock = threading.Lock()
        self.snapshot_lock = threading.Lock()
        self.inspections = {}
        self.started = utcnow()

    def path(self, *parts):
        path = self.root.joinpath(*parts).resolve()
        if not path.is_relative_to(self.root):
            raise HTTPException(503, 'Training storage unavailable')
        return path

    def initialize(self):
        with self.sessions.begin() as session:
            if session.get(TrainingModel, BASE_MODEL_ID) is None:
                session.add(TrainingModel(id=BASE_MODEL_ID, name='YOLO11m COCO (production)', version=0, kind='base',
                                          class_mode='coco80', config={'weights': 'training/weights/yolo11m.pt'},
                                          files={}, metrics=None))
        for leftover in self.root.glob('**/*.partial') if self.root.is_dir() else []:
            with suppress(OSError):
                leftover.unlink()

    # ----- worker / GPU / production state ----------------------------------------------
    def worker_state(self):
        with self.worker_lock:
            state = dict(self.worker) if self.worker else None
        if state is None:
            return {'online': False, 'last_seen': None}
        age = (utcnow() - state['seen_at']).total_seconds()
        return {'online': age <= self.settings.training_worker_stale_seconds, 'worker_id': state['worker_id'],
                'last_seen': state['seen_at'].isoformat(), 'age_seconds': round(age, 1), 'gpu': state['gpu'],
                'base_weights': state['base_weights'], 'disk_free_mib': state['disk_free_mib'],
                'deployment': state.get('deployment', {})}

    def production(self, session):
        """Live Preview/DeepStream view from the existing poller tables (no GPU access here)."""
        now = utcnow()
        cameras = list(session.scalars(select(Camera).where(Camera.enabled.is_(True))))
        per_camera, online, max_age = {}, 0, None
        for camera in cameras:
            row = session.get(CameraStatus, camera.camera_id)
            if row is None:
                continue
            status = status_out(row, self.settings.stale_after, now)
            if status.state in ('online', 'degraded'):
                online += 1
                per_camera[camera.camera_id] = round(status.fps, 2)
                if status.last_frame_at:
                    age = (now - aware(status.last_frame_at)).total_seconds()
                    max_age = age if max_age is None else max(max_age, age)
        parking = {c.camera_id for c in cameras if '주차장' in c.name}
        return {'preview': self.poller.preview_health() if self.poller else 'unknown', 'cameras_enabled': len(cameras),
                'cameras_online': online, 'total_fps': round(sum(per_camera.values()), 1),
                'parking_cameras_online': len(parking & per_camera.keys()),
                'parking_fps': round(sum(v for k, v in per_camera.items() if k in parking), 1),
                'max_frame_age_seconds': round(max_age, 1) if max_age is not None else None,
                'per_camera_fps': per_camera, 'observed_at': now.isoformat()}

    def gpu_check(self, worker, imgsz, batch):
        """None when the GPU can take this job now, else the human-readable reason."""
        need = required_free_mib(self.settings, imgsz, batch)
        if not worker['online']:
            return 'The training worker is offline. Start training/worker.py on the GPU server.'
        gpu = worker.get('gpu') or {}
        if not gpu.get('available'):
            return 'CUDA/GPU is not available on the training host (nvidia-smi failed).'
        if gpu['free_mib'] < need:
            return (f'Not enough free GPU memory: {gpu["free_mib"]} MiB free, {need} MiB needed '
                    f'(estimated training use + {self.settings.training_gpu_reserve_mib} MiB reserved for DeepStream). '
                    'Lower batch/image size or wait.')
        if gpu['util'] > self.settings.training_max_gpu_util:
            return f'GPU is busy: {gpu["util"]}% utilisation (limit {self.settings.training_max_gpu_util}%).'
        return None

    def expire_stale(self, session):
        """Jobs whose worker stopped reporting are failed (worker death also kills train.py)."""
        stale = self.settings.training_worker_stale_seconds
        now = utcnow()
        if (now - self.started).total_seconds() < stale:
            return  # Backend just (re)started: give a live worker time to report again
        limit = now - timedelta(seconds=stale)
        for job in session.scalars(select(TrainingJob).where(TrainingJob.status.in_(('running', 'stopping')))
                                   .with_for_update()):
            if job.heartbeat_at is None or aware(job.heartbeat_at) < limit:
                self.close_job(job, 'failed', 'worker_lost', 'The training worker stopped reporting (worker or GPU '
                               'host restarted, or it crashed). The training process stops with the worker.')

    @staticmethod
    def close_job(job, status, code=None, error=None):
        job.status, job.active_slot, job.finished_at = status, None, utcnow()
        job.phase = status
        job.error_code, job.error = code, (error[:512] if error else None)

    # ----- dataset ----------------------------------------------------------------------
    def export_zip(self, export_id):
        return (self.dataset_root / 'exports' / f'{export_id}.zip').resolve()

    def inspection(self, export_id):
        path = self.export_zip(export_id)
        try:
            st = path.stat()
            key = (st.st_size, st.st_mtime_ns)
        except OSError:
            return {'readiness': 'blocked', 'errors': ['The dataset ZIP file is missing.'], 'limits': [], 'notes': []}
        cached = self.inspections.get(export_id)
        if cached and cached[0] == key:
            return cached[1]
        result = inspect_archive(path)
        self.inspections[export_id] = (key, result)
        return result

    def snapshot(self, export_id):
        """Immutable copy (hardlink on the same volume) that survives deleting the export."""
        target = self.path('snapshots', f'{export_id}.zip')
        sidecar = target.with_name(target.name + '.sha256')
        with self.snapshot_lock:
            if target.is_file() and sidecar.is_file():
                return target, sidecar.read_text().strip()
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_name(target.name + '.partial')
            partial.unlink(missing_ok=True)
            source = self.export_zip(export_id)
            try:
                os.link(source, partial)
            except OSError:
                shutil.copyfile(source, partial)
            digest = hashlib.sha256()
            with open(partial, 'rb') as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b''):
                    digest.update(chunk)
            partial.replace(target)
            sidecar.write_text(digest.hexdigest())
            return target, digest.hexdigest()

    def manifest_images(self, export_id, frozen=True):
        """manifest.json image entries of a frozen snapshot (or the live export); None if unreadable."""
        path = self.path('snapshots', f'{export_id}.zip') if frozen else self.export_zip(export_id)
        try:
            st = path.stat()
            key = ('manifest', str(path), st.st_size, st.st_mtime_ns)
            cached = self.inspections.get(key)
            if cached is not None:
                return cached
            with zipfile.ZipFile(path) as archive:
                names = [n for n in archive.namelist() if n == 'manifest.json'
                         or (n.endswith('/manifest.json') and n.count('/') == 1)]
                if len(names) != 1 or archive.getinfo(names[0]).file_size > 32 * 2**20:
                    return None
                images = json.loads(archive.read(names[0])).get('images') or []
        except (OSError, ValueError, zipfile.BadZipFile, HTTPException):
            return None
        images = [entry for entry in images if isinstance(entry, dict) and entry.get('id')]
        self.inspections[key] = images
        return images

    def lineage_seen(self, session, model_id):
        """Image ids and capture sessions that shaped a model, through its whole fine-tuning chain.

        Standard runs: train (weights) + val (epoch selection / early stop). Train All: every split.
        complete=False when a snapshot cannot be read, so independence cannot be guaranteed.
        """
        ids, sessions, complete, visited = set(), set(), True, set()
        while model_id and model_id not in visited:
            visited.add(model_id)
            model = session.get(TrainingModel, model_id)
            if model is None or model.kind != 'finetuned':
                break
            job = session.get(TrainingJob, model.job_id) if model.job_id else None
            images = self.manifest_images(job.export_id) if job else None
            if images is None:
                complete = False
            else:
                used = SPLITS if is_train_all(job) else ('train', 'val')
                for entry in images:
                    if entry.get('split') in used:
                        ids.add(str(entry['id']))
                        if entry.get('session'):
                            sessions.add(str(entry['session']))
            model_id = model.base_model_id
        return ids, sessions, complete

    def evaluation_plan(self, session, model, export_id):
        """Which images of an export can score `model` independently (never trained on, other sessions)."""
        seen_ids, seen_sessions, complete = self.lineage_seen(session, model.id)
        check = self.inspection(export_id)
        errors = [e for e in for_mode(check, 'all').get('errors', [])]
        images = self.manifest_images(export_id, frozen=False)
        if images is None:
            errors.append('The dataset manifest cannot be read.')
            images = []
        if not complete:
            errors.append('The training data of this model (or its base) cannot be read, so new images cannot be '
                          'separated from training images.')
        trained = [e for e in images if str(e['id']) in seen_ids]
        same_session = [e for e in images if str(e['id']) not in seen_ids and str(e.get('session')) in seen_sessions]
        eligible = [e for e in images if str(e['id']) not in seen_ids and str(e.get('session')) not in seen_sessions]
        sessions = {str(e.get('session')) for e in eligible}
        cameras = {str(e.get('camera_id')) for e in eligible}
        limits = []
        if not eligible:
            errors.append('No new images: every image of this dataset was used to train the model or comes from the '
                          'same capture sessions. Capture and label new CCTV images, then create a new export.')
        elif len(eligible) > MAX_EVAL_IMAGES:
            errors.append(f'More than {MAX_EVAL_IMAGES} new images; create a smaller export.')
        else:
            if len(eligible) < MIN_EVAL_IMAGES:
                limits.append(f'Only {len(eligible)} new images; differences are not statistically meaningful.')
            if len(sessions) < 2:
                limits.append('New images come from fewer than 2 capture sessions.')
        return {'model_id': model.id, 'export_id': export_id, 'imgsz': (model.config or {}).get('imgsz', 640),
                'total': len(images), 'eligible': len(eligible), 'excluded_trained': len(trained),
                'excluded_same_session': len(same_session), 'sessions': len(sessions), 'cameras': len(cameras),
                'boxes': sum(int(e.get('box_count') or 0) for e in eligible),
                'errors': errors, 'limits': limits,
                'readiness': 'blocked' if errors else 'limited' if limits else 'ready',
                'image_ids': sorted(str(e['id']) for e in eligible)}

    # ----- guard ------------------------------------------------------------------------
    def guard(self, session, job, gpu):
        """Compare live Preview FPS (cameras online at baseline) and GPU headroom; maybe stop."""
        monitor = dict(job.monitor or {})
        baseline = monitor.get('baseline') or {}
        current = self.production(session)
        reasons = []
        base_fps = baseline.get('per_camera_fps') or {}
        common = [cid for cid in base_fps if cid in current['per_camera_fps']]
        before = sum(base_fps[cid] for cid in common)
        after = sum(current['per_camera_fps'][cid] for cid in common)
        ratio = after / before if before > 0 else None
        if ratio is not None and ratio < 1 - self.settings.training_max_fps_drop:
            reasons.append(f'Preview FPS dropped {round((1 - ratio) * 100)}% (limit '
                           f'{round(self.settings.training_max_fps_drop * 100)}%) on cameras online before training')
        if baseline.get('preview') == 'reachable' and current['preview'] == 'unreachable':
            reasons.append('Preview (DeepStream) stopped responding')
        if gpu.get('available') and gpu.get('free_mib') is not None and gpu['free_mib'] < self.settings.training_gpu_reserve_mib:
            reasons.append(f'GPU free memory {gpu["free_mib"]} MiB is below the DeepStream reserve '
                           f'{self.settings.training_gpu_reserve_mib} MiB')
        violations = monitor.get('violations', 0) + 1 if reasons else 0
        samples = list(monitor.get('samples') or [])
        samples.append({'t': current['observed_at'], 'fps_ratio': round(ratio, 3) if ratio is not None else None,
                        'total_fps': current['total_fps'], 'parking_fps': current['parking_fps'],
                        'cameras_online': current['cameras_online'], 'max_frame_age': current['max_frame_age_seconds'],
                        'gpu_used_mib': gpu.get('used_mib'), 'gpu_util': gpu.get('util'),
                        'training_mib': gpu.get('training_mib')})
        monitor.update(samples=samples[-MONITOR_SAMPLES:], violations=violations, last_reasons=reasons,
                       current={k: v for k, v in current.items() if k != 'per_camera_fps'})
        if violations >= self.settings.training_guard_samples and not job.stop_requested:
            job.stop_requested, job.status = True, 'stopping'
            job.stop_reason = ('Production protection: ' + '; '.join(reasons))[:256]
            monitor['protection_stop'] = True
        job.monitor = monitor


def jobs_query():
    return select(TrainingJob).order_by(TrainingJob.created_at.desc(), TrainingJob.id.desc())


def find_job(session, job_id, lock=False):
    job = session.get(TrainingJob, str(job_id), with_for_update=lock)
    if job is None:
        raise HTTPException(404, 'Training job not found')
    return job


# ----- public read-only -----------------------------------------------------------------
@router.get('/options')
def options(request: Request):
    config = request.app.state.config
    return {'defaults': {'epochs': 50, 'imgsz': 640, 'batch': 4, 'seed': 42, 'patience': 15, 'device': 'gpu',
                         'class_mode': 'vehicle3', 'base_model_id': BASE_MODEL_ID, 'train_mode': 'standard'},
            'limits': {'epochs': [1, 300], 'batch': [1, 32], 'seed': [0, 2147483647], 'patience': [1, 300],
                       'imgsz': [640, 960]},
            'class_modes': CLASS_MODES, 'train_modes': TRAIN_MODES,
            'gpu_policy': {'min_free_mib': config.training_min_free_mib, 'reserve_mib': config.training_gpu_reserve_mib,
                           'max_gpu_util': config.training_max_gpu_util, 'max_fps_drop': config.training_max_fps_drop,
                           'guard_samples': config.training_guard_samples,
                           'require_preview': config.training_require_preview,
                           'vram_estimate': {'base_mib': 150, 'per_image_mib_at_640': 800}},
            'control_enabled': bool(config.training_admin_token), 'worker_enabled': bool(config.training_worker_token),
            'max_concurrent_jobs': 1}


@router.get('/status')
def status(request: Request, imgsz: int = Query(640), batch: int = Query(4, ge=1, le=32)):
    if imgsz not in (640, 960):
        raise HTTPException(422, 'Image size must be 640 or 960')
    worker = service(request).worker_state()
    with request.app.state.sessions.begin() as session:
        service(request).expire_stale(session)
        production = service(request).production(session)
        active = session.scalar(select(TrainingJob).where(TrainingJob.status.in_(ACTIVE_STATUSES))
                                .order_by(TrainingJob.created_at.desc()).limit(1))
        active_out = job_out(active) if active else None
    production.pop('per_camera_fps')
    blocker = service(request).gpu_check(worker, imgsz, batch)
    return {'worker': worker, 'production': production, 'active_job': active_out,
            'estimate': {'imgsz': imgsz, 'batch': batch, 'training_mib': estimate_vram_mib(imgsz, batch),
                         'required_free_mib': required_free_mib(request.app.state.config, imgsz, batch)},
            'gpu_ready': blocker is None, 'gpu_blocker': blocker}


@router.get('/datasets')
def datasets(request: Request):
    with request.app.state.sessions() as session:
        exports = list(session.scalars(select(DatasetExport).where(DatasetExport.status == 'ready')
                                       .order_by(DatasetExport.created_at.desc()).limit(50)))
        rows = [(job.id, aware(job.created_at).isoformat(), job.seed) for job in exports]
    result = []
    for export_id, created_at, seed in rows:
        check = service(request).inspection(export_id)
        result.append({'id': export_id, 'name': f'dataset_{export_id[:8]}', 'created_at': created_at, 'seed': seed,
                       **{k: check.get(k) for k in ('readiness', 'counts', 'boxes', 'labels_per_class_total',
                                                    'classes', 'sessions')},
                       'readiness_by_mode': {mode: value['readiness'] for mode, value in (check.get('modes') or {}).items()},
                       'issue_count': len(check.get('errors', [])) + len(check.get('limits', []))})
    return result


@router.get('/datasets/{export_id}/check')
def dataset_check(request: Request, export_id: UUID, train_mode: Literal['standard', 'all'] = 'standard'):
    with request.app.state.sessions() as session:
        job = session.get(DatasetExport, str(export_id))
        if job is None:
            raise HTTPException(404, 'Dataset export not found')
        if job.status != 'ready':
            raise HTTPException(409, 'Dataset export is not ready')
        created_at = aware(job.created_at).isoformat()
    return {'id': str(export_id), 'name': f'dataset_{str(export_id)[:8]}', 'created_at': created_at,
            **for_mode(service(request).inspection(str(export_id)), train_mode)}


@router.get('/jobs')
def jobs(request: Request, limit: int = Query(30, ge=1, le=100)):
    """Training history. Auto-label jobs are left out (they have no epochs/metrics); /datasets polls
    them through /status active_job and /jobs/{id}."""
    with request.app.state.sessions.begin() as session:
        service(request).expire_stale(session)
        rows = []
        for job in session.scalars(jobs_query()):
            if job_kind(job) == 'autolabel':
                continue
            rows.append(job_out(job))
            if len(rows) >= limit:
                break
        return rows


@router.get('/jobs/{job_id}')
def job_detail(request: Request, job_id: UUID):
    with request.app.state.sessions.begin() as session:
        service(request).expire_stale(session)
        return job_out(find_job(session, job_id), detail=True)


@router.get('/jobs/{job_id}/comparison')
def comparison(request: Request, job_id: UUID, offset: int = Query(0, ge=0), limit: int = Query(12, ge=1, le=50),
               only_errors: bool = False):
    with request.app.state.sessions() as session:
        job = find_job(session, job_id)
        if 'eval_predictions.json' not in (job.artifacts or {}):
            raise HTTPException(404, 'No test-set comparison for this job')
    path = service(request).path('jobs', str(job_id), 'eval_predictions.json')
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        raise HTTPException(404, 'Comparison file is missing') from None
    images = data.get('images') or []
    def errors(item):
        return sum(m.get('fn', 0) + m.get('fp', 0) for m in item.get('models', {}).values())
    images.sort(key=lambda item: (-errors(item), item.get('id', '')))
    if only_errors:
        images = [item for item in images if errors(item)]
    page = images[offset:offset + limit]
    ids = [str(item.get('id')) for item in page]
    with request.app.state.sessions() as session:
        present = set(session.scalars(select(DatasetImage.id).where(DatasetImage.id.in_(ids)))) if ids else set()
    for item in page:
        item['image_url'] = f'/api/datasets/images/{item["id"]}/file' if item.get('id') in present else None
    return {'split': data.get('split'), 'imgsz': data.get('imgsz'), 'conf': data.get('conf'),
            'nms_iou': data.get('nms_iou'), 'total': len(images), 'offset': offset, 'images': page}


@router.get('/models')
def models(request: Request):
    with request.app.state.sessions() as session:
        rows = list(session.scalars(select(TrainingModel).order_by(TrainingModel.kind, TrainingModel.created_at.desc())))
        return [model_out(model) for model in rows]


@router.get('/models/compare')
def compare(request: Request, ids: str = Query(min_length=1, max_length=400)):
    wanted = [i for i in ids.split(',') if i][:6]
    if any(not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', i) for i in wanted):
        raise HTTPException(422, 'Invalid model id')
    with request.app.state.sessions() as session:
        rows = {m.id: m for m in session.scalars(select(TrainingModel).where(TrainingModel.id.in_(wanted)))}
    items = [model_out(rows[i]) for i in wanted if i in rows]
    tuned = [m for m in items if m['kind'] == 'finetuned']
    keys = {(m['export_id'], (m['config'] or {}).get('imgsz')) for m in tuned}
    return {'models': items, 'comparable': len(keys) <= 1,
            'note': None if len(keys) <= 1 else 'Models were evaluated on different test sets or image sizes; '
                                                 'compare each against its own baseline instead.'}


@router.get('/models/{model_id}')
def model_detail(request: Request, model_id: str):
    with request.app.state.sessions() as session:
        model = session.get(TrainingModel, model_id)
        if model is None:
            raise HTTPException(404, 'Model not found')
        return model_out(model)


# ----- protected control ----------------------------------------------------------------
@router.post('/auth', status_code=204)
def auth(request: Request):
    require_admin(request)
    return Response(status_code=204)


@router.post('/jobs', status_code=201)
def create_job(request: Request, body: JobCreate):
    require_admin(request)
    config, svc = request.app.state.config, service(request)
    export_id = str(body.export_id)
    with request.app.state.sessions() as session:
        export = session.get(DatasetExport, export_id)
        if export is None:
            raise HTTPException(404, 'Dataset export not found')
        if export.status != 'ready':
            raise HTTPException(409, 'Dataset export is not ready')
        base = session.get(TrainingModel, body.base_model_id)
        if base is None:
            raise HTTPException(404, 'Base model not found')
        base_info = model_out(base)
    check = for_mode(svc.inspection(export_id), body.train_mode)
    if check['readiness'] == 'blocked':
        raise HTTPException(422, 'Dataset cannot be used for training: ' + ' '.join(check['errors'][:3]))
    if check['readiness'] == 'limited' and not body.acknowledge_limits:
        raise HTTPException(422, 'This dataset has training limitations. Review them and confirm to continue.')
    if body.train_mode == 'all' and body.class_mode != 'vehicle3':
        raise HTTPException(422, 'Train All keeps the pretrained COCO 80-class head: use car/truck/bus mode.')
    if base_info['kind'] == 'finetuned':
        if base_info['status'] != 'available':
            raise HTTPException(409, 'The selected model has no stored best.pt.')
        if body.class_mode == 'vehicle3' and base.class_mode != 'vehicle3':
            raise HTTPException(422, 'car/truck/bus training needs YOLO11m or a model trained in car/truck/bus mode.')
    worker = svc.worker_state()
    if base_info['kind'] == 'base' and worker['online'] and not (worker.get('base_weights') or {}).get('present'):
        raise HTTPException(409, 'training/weights/yolo11m.pt is missing on the training host.')
    blocker = svc.gpu_check(worker, body.imgsz, body.batch)
    if blocker:
        raise HTTPException(503 if not worker['online'] else 409, blocker)
    with request.app.state.sessions() as session:
        production = svc.production(session)
    if config.training_require_preview and production['preview'] != 'reachable':
        raise HTTPException(409, 'Preview (DeepStream) is not reachable, so its FPS cannot be protected during '
                                 'training. Start training when the production stream is healthy.')
    free = shutil.disk_usage(svc.dataset_root if svc.dataset_root.exists() else Path('.')).free // 2**20
    if free < config.training_min_storage_mib:
        raise HTTPException(507, f'Only {free} MiB free on the dataset volume; training results need '
                                 f'{config.training_min_storage_mib} MiB.')
    try:
        _, digest = svc.snapshot(export_id)
    except OSError:
        raise HTTPException(503, 'Could not freeze the dataset snapshot (storage error).') from None
    epochs = 1 if body.smoke else body.epochs
    train_all = body.train_mode == 'all'
    job_config = {'kind': 'train', 'train_mode': body.train_mode, 'epochs': epochs, 'imgsz': body.imgsz,
                  'batch': body.batch, 'seed': body.seed, 'patience': body.patience, 'class_mode': body.class_mode,
                  'device': 'gpu', 'smoke': body.smoke,
                  'early_stopping': not train_all, 'weights_selection': 'final_epoch' if train_all else 'best_val',
                  'acknowledged_limits': check['limits'] if body.acknowledge_limits else [],
                  'required_free_mib': required_free_mib(config, body.imgsz, body.batch),
                  'estimated_training_mib': estimate_vram_mib(body.imgsz, body.batch)}
    summary = {k: check.get(k) for k in ('readiness', 'counts', 'boxes', 'labels_per_class_total', 'classes',
                                         'sessions', 'limits', 'notes', 'train_mode')}
    if base_info['kind'] == 'finetuned' and not train_all:
        # A base model that already trained on this export's test images makes the test non-independent.
        with request.app.state.sessions() as session:
            seen_ids, _, complete = svc.lineage_seen(session, body.base_model_id)
        test_ids = {str(e['id']) for e in svc.manifest_images(export_id, frozen=True) or [] if e.get('split') == 'test'}
        summary['test_overlap'] = len(test_ids & seen_ids) if complete else len(test_ids)
    try:
        with request.app.state.sessions.begin() as session:
            svc.expire_stale(session)
            job = TrainingJob(id=str(uuid4()), name=body.name, status='queued', phase='queued',
                              message='Waiting for the GPU worker', active_slot=1, export_id=export_id,
                              dataset_sha256=digest, dataset_summary=summary, base_model_id=body.base_model_id,
                              config=job_config, epochs=epochs, metrics=[], log_tail=[], artifacts={})
            session.add(job)
            session.flush()
            return job_out(job, detail=True)
    except IntegrityError:
        raise HTTPException(409, 'Another training job is already queued or running. Wait for it or stop it first.') from None


def find_evaluable_model(session, model_id):
    model = session.get(TrainingModel, model_id)
    if model is None:
        raise HTTPException(404, 'Model not found')
    if model.kind != 'finetuned' or 'best.pt' not in (model.files or {}):
        raise HTTPException(409, 'Only a fine-tuned model with stored best.pt can be evaluated on new images.')
    return model


def ready_export(session, export_id):
    export = session.get(DatasetExport, export_id)
    if export is None:
        raise HTTPException(404, 'Dataset export not found')
    if export.status != 'ready':
        raise HTTPException(409, 'Dataset export is not ready')
    return export


@router.get('/evaluations/plan')
def evaluation_plan(request: Request, model_id: str = Query(min_length=1, max_length=64, pattern=r'^[A-Za-z0-9_.-]+$'),
                    export_id: UUID = Query()):
    """Which images of a dataset export can evaluate a model independently (read-only preview)."""
    with request.app.state.sessions() as session:
        model = find_evaluable_model(session, model_id)
        ready_export(session, str(export_id))
        plan = service(request).evaluation_plan(session, model, str(export_id))
    plan.pop('image_ids')
    return plan


@router.post('/evaluations', status_code=201)
def create_evaluation(request: Request, body: EvaluationCreate):
    """Queue a GPU job that scores a model on new CCTV images (never trained on) against the COCO base."""
    require_admin(request)
    config, svc = request.app.state.config, service(request)
    export_id = str(body.export_id)
    with request.app.state.sessions() as session:
        model = find_evaluable_model(session, body.model_id)
        ready_export(session, export_id)
        try:
            svc.snapshot(export_id)
        except OSError:
            raise HTTPException(503, 'Could not freeze the dataset snapshot (storage error).') from None
        plan = svc.evaluation_plan(session, model, export_id)
        name, imgsz = f'{model.name}-v{model.version}'[:48], plan['imgsz']
    if plan['readiness'] == 'blocked':
        raise HTTPException(422, ' '.join(plan['errors'][:3]))
    if plan['readiness'] == 'limited' and not body.acknowledge_limits:
        raise HTTPException(422, 'This evaluation has limitations. Review them and confirm to continue.')
    if imgsz not in (640, 960):
        imgsz = 640
    blocker = svc.gpu_check(svc.worker_state(), imgsz, 4)
    if blocker:
        raise HTTPException(503 if not svc.worker_state()['online'] else 409, blocker)
    digest = svc.snapshot(export_id)[1]
    # The frozen snapshot must hold exactly the planned images (exports are immutable; guard anyway).
    frozen = {str(e['id']) for e in svc.manifest_images(export_id) or []}
    if not set(plan['image_ids']) <= frozen:
        raise HTTPException(409, 'The dataset snapshot does not match the export. Create a new export.')
    job_config = {'kind': 'evaluate', 'imgsz': imgsz, 'batch': 4, 'device': 'gpu', 'smoke': False,
                  'class_mode': model.class_mode, 'eval_image_ids': plan['image_ids'],
                  'acknowledged_limits': plan['limits'] if body.acknowledge_limits else [],
                  'required_free_mib': required_free_mib(config, imgsz, 4),
                  'estimated_training_mib': estimate_vram_mib(imgsz, 4)}
    summary = {'kind': 'evaluate', 'readiness': plan['readiness'], 'limits': plan['limits'],
               'counts': {'train': 0, 'val': 0, 'test': plan['eligible'], 'total': plan['eligible']},
               'boxes': {'train': 0, 'val': 0, 'test': plan['boxes'], 'total': plan['boxes']},
               'sessions': {'train': 0, 'val': 0, 'test': plan['sessions']}, 'cameras': {'test': plan['cameras']},
               'excluded': {'trained': plan['excluded_trained'], 'same_session': plan['excluded_same_session'],
                            'export_total': plan['total']}}
    try:
        with request.app.state.sessions.begin() as session:
            svc.expire_stale(session)
            job = TrainingJob(id=str(uuid4()), name=name, status='queued', phase='queued',
                              message='Waiting for the GPU worker', active_slot=1, export_id=export_id,
                              dataset_sha256=digest, dataset_summary=summary, base_model_id=body.model_id,
                              config=job_config, epochs=1, metrics=[], log_tail=[], artifacts={})
            session.add(job)
            session.flush()
            return job_out(job, detail=True)
    except IntegrityError:
        raise HTTPException(409, 'Another training job is already queued or running. Wait for it or stop it first.') from None


@router.get('/autolabels/plan')
def autolabel_plan_view(request: Request, model_id: str = Query(min_length=1, max_length=64, pattern=r'^[A-Za-z0-9_.-]+$'),
                        camera_id: str | None = Query(None, min_length=1, max_length=128, pattern=CAMERA_ID),
                        include_auto_drafts: bool = False):
    """How many unlabelled images an auto-label job would draft, and what currently blocks starting one."""
    svc = service(request)
    with request.app.state.sessions.begin() as session:
        svc.expire_stale(session)
        model = session.get(TrainingModel, model_id)
        model_ok = model is not None and model_out(model)['status'] == 'available'
        model_kind = model.kind if model else None
        active = session.scalar(select(TrainingJob.id).where(TrainingJob.status.in_(ACTIVE_STATUSES)).limit(1)) is not None
        eligible, skipped = autolabel_plan(session, camera_id, include_auto_drafts)
    worker = svc.worker_state()
    if model_kind == 'base' and worker['online'] and not (worker.get('base_weights') or {}).get('present'):
        model_ok = False
    blocker = (NO_AUTOLABEL_MODEL if not model_ok else WORKER_OFFLINE if not worker['online']
               else ANOTHER_JOB_RUNNING if active else NO_PENDING_IMAGES if not eligible else None)
    return {'model_id': model_id, 'camera_id': camera_id, 'include_auto_drafts': include_auto_drafts,
            'eligible': len(eligible), 'skipped': skipped, 'max_images': AUTOLABEL_MAX_IMAGES,
            'ready': blocker is None, 'blocker': blocker}


@router.post('/autolabels', status_code=201)
def create_autolabel(request: Request, body: AutolabelCreate):
    """Queue a GPU job that drafts boxes on unlabelled pending images; a person still reviews each one."""
    require_admin(request)
    config, svc = request.app.state.config, service(request)
    worker = svc.worker_state()
    with request.app.state.sessions() as session:
        model = session.get(TrainingModel, body.model_id)
        if model is None:
            raise HTTPException(404, 'Model not found')
        info = model_out(model)
        if info['status'] != 'available':
            raise HTTPException(409, 'The selected model has no stored best.pt.')
        if body.camera_id and session.get(Camera, body.camera_id) is None:
            raise HTTPException(404, 'Camera not found')
        eligible, _ = autolabel_plan(session, body.camera_id, body.include_auto_drafts)
        targets = [{'id': image.id, 'revision': image.revision} for image in eligible[:AUTOLABEL_MAX_IMAGES]]
        label, class_mode = info['label'], model.class_mode or 'coco80'
    if not targets:
        raise HTTPException(422, NO_PENDING_IMAGES)
    if info['kind'] == 'base' and worker['online'] and not (worker.get('base_weights') or {}).get('present'):
        raise HTTPException(409, 'training/weights/yolo11m.pt is missing on the training host.')
    blocker = svc.gpu_check(worker, 640, 1)
    if blocker:
        raise HTTPException(503 if not worker['online'] else 409, blocker)
    job_config = {'kind': 'autolabel', 'imgsz': 640, 'conf': body.conf, 'class_mode': class_mode,
                  'camera_id': body.camera_id, 'include_auto_drafts': body.include_auto_drafts, 'device': 'gpu',
                  'targets': targets, 'required_free_mib': required_free_mib(config, 640, 1),
                  'estimated_training_mib': estimate_vram_mib(640, 1)}
    summary = {'kind': 'autolabel', 'camera_id': body.camera_id, 'model_label': label, 'conf': body.conf,
               'include_auto_drafts': body.include_auto_drafts, 'total': len(targets), 'processed': 0,
               'labeled': 0, 'boxes': 0, 'no_detections': 0, 'skipped_changed': 0}
    try:
        with request.app.state.sessions.begin() as session:
            svc.expire_stale(session)
            job = TrainingJob(id=str(uuid4()), name=f'autolabel-{label}'[:48], status='queued', phase='queued',
                              message='Waiting for the GPU worker', active_slot=1, export_id='', dataset_sha256='',
                              dataset_summary=summary, base_model_id=body.model_id, config=job_config, epochs=1,
                              metrics=[], log_tail=[], artifacts={})
            session.add(job)
            session.flush()
            return job_out(job, detail=True)
    except IntegrityError:
        raise HTTPException(409, 'Another training job is already queued or running. Wait for it or stop it first.') from None


@router.post('/jobs/{job_id}/stop')
def stop_job(request: Request, job_id: UUID):
    require_admin(request)
    with request.app.state.sessions.begin() as session:
        job = find_job(session, job_id, lock=True)
        if job.status == 'queued':
            TrainingService.close_job(job, 'stopped', None, 'Stopped before it started.')
        elif job.status == 'running':
            job.status, job.stop_requested, job.stop_reason = 'stopping', True, 'Stopped by the user'
            job.message = 'Stop requested: finishing the current batch and saving checkpoints'
        elif job.status != 'stopping':
            raise HTTPException(409, 'This training job has already finished')
        return job_out(job)


def artifact_response(path, filename):
    if not path.is_file():
        raise HTTPException(404, 'File not found')
    return FileResponse(path, media_type='application/octet-stream', filename=filename,
                        headers={'Cache-Control': 'no-store'})


@router.get('/jobs/{job_id}/artifacts/{name}')
def job_artifact(request: Request, job_id: UUID, name: str):
    require_admin(request)
    if name not in ARTIFACT_LIMITS:
        raise HTTPException(404, 'Unknown artifact')
    with request.app.state.sessions() as session:
        job = find_job(session, job_id)
        if name not in (job.artifacts or {}):
            raise HTTPException(404, 'Artifact not stored for this job')
    return artifact_response(service(request).path('jobs', str(job_id), name), f'{str(job_id)[:8]}_{name}')


@router.get('/models/{model_id}/files/{kind}')
def model_file(request: Request, model_id: str, kind: Literal['best', 'last']):
    require_admin(request)
    with request.app.state.sessions() as session:
        model = session.get(TrainingModel, model_id)
        if model is None or f'{kind}.pt' not in (model.files or {}):
            raise HTTPException(404, 'Model file not found')
        filename, job_id = f'{model.name}-v{model.version}-{kind}.pt', model.job_id
    return artifact_response(service(request).path('jobs', job_id, f'{kind}.pt'), filename)


class DeploymentCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model_id: str | None = Field(default=None, min_length=1, max_length=64, pattern=r'^[A-Za-z0-9_.-]+$')
    action: Literal['deploy', 'rollback'] = 'deploy'


@router.get('/deployment')
def deployment_status(request: Request):
    worker = service(request).worker_state()
    runtime = dict(worker.get('deployment') or {})
    runtime['available'] = bool(worker['online'] and runtime.get('available'))
    runtime['runtime_online'] = bool(worker['online'] and runtime.get('runtime_online'))
    # Retain the last known ID for diagnosis, explicitly mark it unconfirmed when offline.
    runtime['confirmed'] = bool(runtime['runtime_online'] and runtime.get('verified', True))
    return runtime


@router.post('/deployments', status_code=201)
def create_deployment(request: Request, body: DeploymentCreate):
    require_admin(request)
    svc = service(request)
    runtime = deployment_status(request)
    if not runtime['available']:
        raise HTTPException(503, 'DeepStream deployment is disabled or offline.')
    if runtime.get('phase') not in ('idle', 'completed', 'failed', 'rolled_back'):
        raise HTTPException(409, 'DeepStream deployment or recovery is still in progress.')
    if body.action == 'rollback':
        if not runtime.get('previous'):
            raise HTTPException(409, 'No previous verified model is available.')
        model_id = BASE_MODEL_ID  # rollback uses the runtime journal, never downloads base weights
    else:
        if not body.model_id:
            raise HTTPException(422, 'model_id is required.')
        model_id = body.model_id
    try:
        with request.app.state.sessions.begin() as session:
            svc.expire_stale(session)
            model = session.get(TrainingModel, model_id)
            if model is None:
                raise HTTPException(404, 'Model not found.')
            if body.action == 'deploy':
                if model.kind != 'finetuned' or 'best.pt' not in (model.files or {}):
                    raise HTTPException(409, 'A completed model with registered best.pt is required.')
                source = session.get(TrainingJob, model.job_id)
                if not source or source.status != 'completed':
                    raise HTTPException(409, 'The source training job did not complete.')
                if (model.config or {}).get('imgsz') != 640:
                    # nvinfer's on-the-fly update keeps the network resolution; other sizes need a restart
                    # with a new config and new FPS/VRAM measurements, so they are never applied automatically.
                    raise HTTPException(422, 'Only 640px models can be hot-deployed.')
                names = [str(v).lower() for v in (model.names or {}).values()]
                if model.class_mode == 'vehicle3':
                    if names and len(names) != 80:
                        raise HTTPException(422, 'A COCO80 model must keep all 80 classes.')
                elif model.class_mode == 'single':
                    # single_cls merges every dataset class; it is "car" only if the dataset was car-only.
                    classes = {str(v).lower() for v in ((source.dataset_summary or {}).get('classes') or {}).values()}
                    if len(names) != 1 or names[0] not in ('car', 'item') or classes != {'car'}:
                        raise HTTPException(422, 'A single-class model must be trained on car-only labels.')
                else:
                    raise HTTPException(422, 'Unsupported class mode for deployment.')
                if runtime.get('active') == model.id:
                    raise HTTPException(409, 'This model is already active.')
            name = (f'Deploy {model.name} v{model.version}' if body.action == 'deploy'
                    else f'Roll back to {runtime["previous"]}')[:64]
            job = TrainingJob(id=str(uuid4()), name=name, active_slot=1,
                              status='queued', phase='queued', message='Waiting for deployment worker',
                              export_id=model.export_id or '', dataset_sha256=model.dataset_sha256 or '',
                              dataset_summary={}, base_model_id=model_id, epochs=1,
                              config={'kind': 'deploy', 'action': body.action, 'imgsz': 640,
                                      **({'class_mode': model.class_mode} if body.action == 'deploy' else {}),
                                      'required_free_mib': required_free_mib(request.app.state.config, 640, 4)},
                              metrics=[], log_tail=[], artifacts={})
            session.add(job)
            session.flush()
            return job_out(job, detail=True)
    except IntegrityError:
        raise HTTPException(409, 'Another training, evaluation or deployment job is active.') from None


# ----- GPU worker API (loopback; Preview never forwards these) -------------------------------
@router.post('/worker/heartbeat')
def worker_heartbeat(request: Request, body: Beat):
    require_worker(request)
    svc = service(request)
    gpu = {k: v for k, v in body.gpu.items() if k in ('available', 'index', 'name', 'total_mib', 'used_mib', 'free_mib',
                                                      'util', 'temperature', 'training_mib', 'error')}
    with svc.worker_lock:
        svc.worker = {'worker_id': body.worker_id, 'seen_at': utcnow(), 'gpu': gpu,
                      'base_weights': {k: body.base_weights.get(k) for k in ('present', 'sha256', 'size')},
                      'disk_free_mib': body.disk_free_mib,
                      'deployment': {k: body.deployment.get(k) for k in
                                     ('available', 'runtime_online', 'active', 'previous', 'operation', 'phase',
                                      'error', 'evidence', 'service', 'checks', 'failures', 'session', 'verified',
                                      'active_class_map', 'diagnosis')}}
    with request.app.state.sessions.begin() as session:
        if body.started:
            for job in session.scalars(select(TrainingJob).where(TrainingJob.status.in_(('running', 'stopping')),
                                                                 TrainingJob.worker_id != body.worker_id).with_for_update()):
                svc.close_job(job, 'failed', 'worker_restarted', 'The training worker restarted; the running '
                              'training process was terminated with it. Start a new job.')
            base = session.get(TrainingModel, BASE_MODEL_ID)
            if base and body.base_weights.get('sha256'):
                files = {'yolo11m.pt': {'sha256': body.base_weights['sha256'], 'size': body.base_weights.get('size'),
                                        'location': 'training host'}}
                if base.files != files:
                    base.files = files
        if not body.job:
            return {'ok': True, 'stop': False}
        job = session.get(TrainingJob, str(body.job.get('id', '')), with_for_update=True)
        if job is None or job.worker_id != body.worker_id or job.status not in ('running', 'stopping'):
            return {'ok': True, 'stop': True, 'stop_reason': 'This job is no longer active on the Backend.'}
        update = body.job
        job.heartbeat_at = utcnow()
        if isinstance(update.get('phase'), str) and update['phase'][:24]:
            job.phase = update['phase'][:24]
        if job.status != 'stopping':
            job.message = str(update['message'])[:256] if update.get('message') else None
        progress = update.get('progress') if isinstance(update.get('progress'), dict) else {}
        if progress:
            job.live = {k: progress.get(k) for k in ('epoch', 'epochs', 'batch', 'batches', 'batch_size',
                                                    'train_loss', 'torch_reserved_mib', 'time')}
        rows = update.get('metrics_rows') if isinstance(update.get('metrics_rows'), list) else []
        offset = update.get('metrics_offset') if isinstance(update.get('metrics_offset'), int) else None
        if rows and offset is not None and 0 <= offset <= len(job.metrics or []):
            job.metrics = (job.metrics or [])[:offset] + [metric_point(r, is_train_all(job)) for r in rows[:1000]
                                                          if isinstance(r, dict)]
        done = len(job.metrics or [])
        epoch = progress.get('epoch') if isinstance(progress.get('epoch'), int) else done
        batch, batches = progress.get('batch'), progress.get('batches')
        job.epoch = max(done, min(int(epoch or 0), job.epochs)) if job.phase == 'training' else max(job.epoch, done)
        fraction = done / job.epochs
        if job.phase == 'training' and isinstance(batch, int) and isinstance(batches, int) and batches > 0 and epoch:
            fraction = max(fraction, (min(int(epoch), job.epochs) - 1 + min(batch, batches) / batches) / job.epochs)
        if job_kind(job) != 'autolabel':  # auto-label progress comes from the applied result batches
            job.progress = round(min(fraction, 1.0), 4)
        lines = [str(line)[:500] for line in (update.get('log_lines') or [])[-200:]]
        if lines:
            job.log_tail = ((job.log_tail or []) + lines)[-LOG_LINES:]
        job.gpu = gpu
        if job.phase in ('training', 'labeling'):
            svc.guard(session, job, gpu)
        return {'ok': True, 'stop': job.stop_requested, 'stop_reason': job.stop_reason}


@router.post('/worker/claim')
def worker_claim(request: Request, body: Claim):
    require_worker(request)
    svc, config = service(request), request.app.state.config
    with request.app.state.sessions.begin() as session:
        svc.expire_stale(session)
        if session.scalar(select(TrainingJob.id).where(TrainingJob.status.in_(('running', 'stopping'))).limit(1)):
            return Response(status_code=204)
        job = session.scalar(select(TrainingJob).where(TrainingJob.status == 'queued')
                             .order_by(TrainingJob.created_at).limit(1).with_for_update())
        if job is None:
            return Response(status_code=204)
        base = session.get(TrainingModel, job.base_model_id)
        if base is None or (base.kind == 'finetuned' and 'best.pt' not in (base.files or {})):
            svc.close_job(job, 'failed', 'base_model_missing', 'The selected base model is no longer available.')
            return Response(status_code=204)
        job.status, job.phase, job.message = 'running', 'preparing', 'Worker accepted the job'
        job.worker_id, job.started_at, job.heartbeat_at = body.worker_id, utcnow(), utcnow()
        production = svc.production(session)
        job.monitor = {'baseline': production, 'samples': [], 'violations': 0,
                       'limits': {'max_fps_drop': config.training_max_fps_drop,
                                  'reserve_mib': config.training_gpu_reserve_mib,
                                  'guard_samples': config.training_guard_samples}}
        base_sha = (base.files or {}).get('best.pt' if base.kind == 'finetuned' else 'yolo11m.pt', {}).get('sha256')
        return {'id': job.id, 'config': job.config, 'dataset_sha256': job.dataset_sha256,
                'base_model': {'id': base.id, 'kind': base.kind, 'job_id': base.job_id, 'sha256': base_sha},
                'required_free_mib': job.config['required_free_mib'], 'max_gpu_util': config.training_max_gpu_util,
                'gpu_wait_seconds': config.training_gpu_wait_seconds}


@router.get('/worker/jobs/{job_id}/dataset')
def worker_dataset(request: Request, job_id: UUID):
    require_worker(request)
    with request.app.state.sessions() as session:
        job = find_job(session, job_id)
        if job.status not in ('running', 'stopping'):
            raise HTTPException(409, 'Job is not running')
        export_id = job.export_id
    path = service(request).path('snapshots', f'{export_id}.zip')
    if not path.is_file():
        raise HTTPException(404, 'Dataset snapshot is missing')
    return FileResponse(path, media_type='application/zip', headers={'Cache-Control': 'no-store'})


@router.get('/worker/models/{model_id}/weights')
def worker_weights(request: Request, model_id: str):
    require_worker(request)
    with request.app.state.sessions() as session:
        model = session.get(TrainingModel, model_id)
        if model is None or model.kind != 'finetuned' or 'best.pt' not in (model.files or {}):
            raise HTTPException(404, 'Model weights not found')
        job_id = model.job_id
    return artifact_response(service(request).path('jobs', job_id, 'best.pt'), 'best.pt')


@router.get('/worker/jobs/{job_id}/images/{image_id}')
def worker_image(request: Request, job_id: UUID, image_id: UUID):
    """Original JPEG of one target image of a running auto-label job."""
    require_worker(request)
    with request.app.state.sessions() as session:
        job = find_job(session, job_id)
        if job.status not in ('running', 'stopping'):
            raise HTTPException(409, 'Job is not running')
        if job_kind(job) != 'autolabel' or not any(t.get('id') == str(image_id) for t in job.config.get('targets') or []):
            raise HTTPException(404, 'Image is not part of this job')
        image = session.get(DatasetImage, str(image_id))
        if image is None:
            raise HTTPException(404, 'Dataset image not found')
        relative = image.file_path
    path = request.app.state.datasets.path(relative)
    if not path.is_file():
        raise HTTPException(404, 'Image file not found')
    return FileResponse(path, media_type='image/jpeg', headers={'Cache-Control': 'no-store'})


@router.post('/worker/jobs/{job_id}/autolabels')
def worker_autolabels(request: Request, job_id: UUID, body: AutolabelResults):
    """Apply a batch of detections as auto drafts. One transaction; every image is row-locked and only
    changed when its revision still equals the one frozen at job creation and nobody labelled it since."""
    require_worker(request)
    with request.app.state.sessions.begin() as session:
        job = find_job(session, job_id, lock=True)
        if job.worker_id != body.worker_id:
            raise HTTPException(409, 'Job belongs to another worker')
        if job.status not in ('running', 'stopping'):
            raise HTTPException(409, 'Job already finished')
        if job_kind(job) != 'autolabel':
            raise HTTPException(409, 'Not an auto-label job')
        targets = {t['id']: t['revision'] for t in job.config.get('targets') or []}
        include = bool(job.config.get('include_auto_drafts'))
        summary = dict(job.dataset_summary or {})
        counts = {k: int(summary.get(k) or 0) for k in ('processed', 'labeled', 'boxes', 'no_detections', 'skipped_changed')}
        now = utcnow()
        for result in body.results:
            image_id = str(result.image_id)
            counts['processed'] += 1
            image = session.get(DatasetImage, image_id, with_for_update=True) if image_id in targets else None
            if image is None or image.revision != targets[image_id] or not autolabel_eligible(image, include):
                counts['skipped_changed'] += 1
                continue
            boxes = clip_auto_boxes(result.boxes, image.width, image.height)
            if not boxes:
                counts['no_detections'] += 1  # stays pending, nothing written
                continue
            image.boxes, image.status, image.reviewed_empty = boxes, 'in_progress', False
            image.revision += 1
            image.updated_at = now
            counts['labeled'] += 1
            counts['boxes'] += len(boxes)
        total = max(1, int(summary.get('total') or len(targets)))
        summary.update(counts)
        job.dataset_summary = summary
        job.progress = round(min(counts['processed'] / total, 1.0), 4)
        job.heartbeat_at = now
        if job.status != 'stopping':
            job.message = (f"Drafted {counts['labeled']} of {counts['processed']}/{total} images "
                           f"({counts['boxes']} boxes)")[:256]
        return summary


@router.put('/worker/jobs/{job_id}/artifacts/{name}')
async def worker_artifact(request: Request, job_id: UUID, name: str):
    require_worker(request)
    if name not in ARTIFACT_LIMITS:
        raise HTTPException(404, 'Unknown artifact')
    lengths = request.headers.getlist('content-length')
    if len(lengths) != 1 or not lengths[0].isdigit():
        raise HTTPException(411, 'Content-Length required')
    length = int(lengths[0])
    if length > ARTIFACT_LIMITS[name]:
        raise HTTPException(413, f'{name} exceeds its size limit')
    svc, config = service(request), request.app.state.config
    with request.app.state.sessions() as session:
        job = find_job(session, job_id)
        if job.status not in ('running', 'stopping'):
            raise HTTPException(409, 'Job is not running')
    target = svc.path('jobs', str(job_id), name)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(target.parent).free
    except OSError:
        raise HTTPException(503, 'Training storage unavailable') from None
    if free - length < config.training_min_storage_mib * 2**20:
        raise HTTPException(507, 'Not enough storage for training results')
    partial = target.with_name(target.name + '.partial')
    digest, size = hashlib.sha256(), 0
    try:
        with open(partial, 'wb') as out:
            async for chunk in request.stream():
                size += len(chunk)
                if size > length:
                    raise HTTPException(400, 'Body longer than Content-Length')
                digest.update(chunk)
                out.write(chunk)
        if size != length:
            raise HTTPException(400, 'Incomplete upload')
        partial.replace(target)
    except OSError:
        partial.unlink(missing_ok=True)
        raise HTTPException(507, 'Could not write training results (storage full or unavailable)') from None
    except HTTPException:
        partial.unlink(missing_ok=True)
        raise
    meta = {'size': size, 'sha256': digest.hexdigest()}
    with request.app.state.sessions.begin() as session:
        job = find_job(session, job_id, lock=True)
        job.artifacts = {**(job.artifacts or {}), name: meta}
    return {'name': name, **meta}


@router.post('/worker/jobs/{job_id}/finish')
def worker_finish(request: Request, job_id: UUID, body: Finish):
    require_worker(request)
    with request.app.state.sessions.begin() as session:
        job = find_job(session, job_id, lock=True)
        if job.worker_id != body.worker_id:
            raise HTTPException(409, 'Job belongs to another worker')
        if job.status not in ('running', 'stopping'):
            raise HTTPException(409, 'Job already finished')
        if body.metrics_rows:
            job.metrics = [metric_point(row, is_train_all(job)) for row in body.metrics_rows]
        job.epoch = len(job.metrics or [])
        job.run_info = body.run_info
        summary = dict(job.dataset_summary or {})
        if body.dataset_report:
            summary['worker_check'] = body.dataset_report
            job.dataset_summary = summary
        job.evaluation = evaluation_summary(body.evaluation, summary)
        status, code, error = body.status, body.error_code, body.error
        if status == 'stopped' and (job.monitor or {}).get('protection_stop'):
            code = 'production_protection'
        if job_kind(job) == 'autolabel':
            # No model registration, best.pt or evaluation: the counts were accumulated per result batch.
            TrainingService.close_job(job, status, code, error)
            if status == 'completed':
                job.progress = 1.0
            return job_out(job)
        if job.config.get('kind') == 'deploy':
            result = (body.run_info or {}).get('deployment') or {}
            if status == 'completed' and (result.get('phase') != 'completed' or not result.get('active')):
                status, code, error = 'failed', 'deployment_unverified', 'Runtime inference verification is missing.'
            TrainingService.close_job(job, status, code, error)
            if status == 'completed':
                job.progress = 1.0
            # Runtime heartbeat is authoritative; registry flags are only the last verified record.
            if result.get('active') and result.get('phase') in ('completed', 'rolled_back'):
                for model in session.scalars(select(TrainingModel)):
                    model.deployment = 'active' if model.id == result['active'] else 'not_deployed'
            return job_out(job)
        evaluating = job.config.get('kind') == 'evaluate'
        if status == 'completed' and not evaluating and 'best.pt' not in (job.artifacts or {}):
            status, code, error = 'failed', 'model_missing', 'best.pt was not stored on the Backend.'
        if status == 'completed' and evaluating and (job.evaluation or {}).get('status') != 'completed':
            status, code, error = 'failed', 'evaluation_failed', 'The evaluation produced no metrics.'
        if status == 'completed':
            job.progress = 1.0
        TrainingService.close_job(job, status, code, error)
        if status == 'completed' and evaluating:
            attach_new_image_evaluation(session, job)
        elif status == 'completed' and not job.config.get('smoke'):
            version = 1 + max(session.scalars(select(TrainingModel.version).where(TrainingModel.name == job.name)),
                              default=0)
            evaluation = job.evaluation or {}
            new = ((evaluation.get('metrics') or {}).get('models') or {}).get('new')
            model = TrainingModel(
                id=str(uuid4()), name=job.name, version=version, kind='finetuned', base_model_id=job.base_model_id,
                job_id=job.id, export_id=job.export_id, dataset_sha256=job.dataset_sha256,
                class_mode=job.config['class_mode'], names=(body.run_info or {}).get('names'),
                config={k: job.config.get(k) for k in ('epochs', 'imgsz', 'batch', 'seed', 'patience', 'class_mode')}
                | {'epochs_run': job.epoch, 'final_batch': (body.run_info or {}).get('final_batch'),
                   'train_mode': job.config.get('train_mode', 'standard'),
                   'weights_selection': job.config.get('weights_selection', 'best_val'),
                   'train_images': (job.dataset_summary or {}).get('counts', {}).get('total') if is_train_all(job)
                   else (job.dataset_summary or {}).get('counts', {}).get('train')},
                # Train All has no independent split: no val/test numbers are recorded for it.
                metrics={'val': None if is_train_all(job) else best_point(job.metrics or []), 'test': new,
                         'test_baseline': ((evaluation.get('metrics') or {}).get('models') or {}).get('baseline'),
                         'test_images': (evaluation.get('metrics') or {}).get('images'),
                         'evaluation_status': evaluation.get('status'), 'new_images': []},
                evaluation_conclusive=bool(evaluation.get('conclusive')),
                files={name: job.artifacts[name] for name in ('best.pt', 'last.pt') if name in job.artifacts})
            session.add(model)
            job.model_id = model.id
        return job_out(job)
