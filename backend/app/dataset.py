"""Persistent, manual-first vehicle dataset API. No model training or inference here.

Auto-label drafts (source='auto') are produced by the GPU worker through /api/training/autolabels;
they only ever land on images nobody labelled and stay 'in_progress' until a person reviews them.
"""
from contextlib import suppress
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import threading
from urllib.parse import quote
from uuid import UUID, uuid4
import zipfile

import cv2
import httpx
import numpy as np
from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from typing import Literal

from .db import Camera
from .dataset_models import DatasetImage, DatasetExport

router = APIRouter(prefix='/api/datasets')
CLASSES = {0: 'car'}  # Dataset IDs, deliberately NOT COCO detector IDs.
MAX_IMAGE_BYTES = 20 * 1024 * 1024


def utcnow():
    return datetime.now(timezone.utc)


def aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


class Box(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: UUID
    class_id: int = Field(ge=0, le=2, strict=True)
    x: float = Field(ge=0, allow_inf_nan=False)
    y: float = Field(ge=0, allow_inf_nan=False)
    width: float = Field(gt=0, allow_inf_nan=False)
    height: float = Field(gt=0, allow_inf_nan=False)
    # 'auto' = draft from the auto-label worker, kept after review for statistics; score only for 'auto'.
    source: Literal['manual', 'auto'] = 'manual'
    score: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode='after')
    def manual_boxes_have_no_score(self):
        if self.source == 'manual':
            self.score = None
        return self


class AnnotationSave(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=1, strict=True)
    boxes: list[Box] = Field(max_length=300)
    status: Literal['in_progress', 'reviewed'] = 'in_progress'
    reviewed_empty: bool = False


class CaptureRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    camera_id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    seed: int = Field(default=42, ge=0, le=2147483647, strict=True)


def validate_boxes(boxes, width, height):
    if len({str(b.id) for b in boxes}) != len(boxes):
        raise HTTPException(422, 'Box IDs must be unique')
    if any(b.x + b.width > width or b.y + b.height > height for b in boxes):
        raise HTTPException(422, 'A bounding box is outside the original image')


def image_out(image):
    return {'id': image.id, 'camera_id': image.camera_id, 'captured_at': aware(image.captured_at).isoformat(),
            'capture_session': image.capture_session, 'width': image.width, 'height': image.height,
            'status': image.status, 'revision': image.revision, 'box_count': len(image.boxes),
            'auto_box_count': sum(1 for box in image.boxes if isinstance(box, dict) and box.get('source') == 'auto'),
            'image_url': f'/api/datasets/images/{image.id}/file',
            'thumbnail_url': f'/api/datasets/images/{image.id}/thumbnail'}


def find_image(session, image_id, lock=False):
    image = session.get(DatasetImage, str(image_id), with_for_update=lock)
    if image is None:
        raise HTTPException(404, 'Dataset image not found')
    return image


def service(request):
    return request.app.state.datasets


def split_sessions(images, seed):
    groups = {}
    for image in images:
        groups.setdefault(image.capture_session, []).append(image)
    keys = sorted(groups, key=lambda key: hashlib.sha256(f'{seed}:{key}'.encode()).hexdigest())
    count = len(keys)
    ntest = max(1, int(count * .1)) if count >= 3 else 0
    nval = max(1, int(count * .1)) if count >= 2 else 0
    ntrain = count - nval - ntest
    assignment = {key: ('train' if i < ntrain else 'val' if i < ntrain + nval else 'test') for i, key in enumerate(keys)}
    return assignment


class DatasetService:
    def __init__(self, settings, sessions, transport=None):
        self.root = Path(settings.dataset_dir).resolve()
        self.sessions = sessions
        self.client = httpx.Client(base_url=settings.preview_url, timeout=7, trust_env=False,
                                   follow_redirects=False, transport=transport)
        self.capture_lock = threading.Lock()
        self.export_lock = threading.Lock()
        self.stop = threading.Event()
        self.threads = []

    def path(self, relative):
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise HTTPException(503, 'Dataset storage unavailable')
        return path

    def initialize(self):
        # Interrupted exports are never served as finished datasets.
        with self.sessions.begin() as session:
            for job in session.scalars(select(DatasetExport).where(DatasetExport.status == 'building')):
                job.status, job.error = 'failed', 'Export interrupted by a backend restart. Create a new export.'
        for tombstone in self.path('images').glob('*.jpg.deleting'):
            image_id = tombstone.name.split('.')[0]
            with self.sessions() as session:
                exists = session.get(DatasetImage, image_id) is not None
            if exists:
                tombstone.replace(tombstone.with_suffix(''))
            else:
                tombstone.unlink(missing_ok=True)

        for tombstone in self.path('exports').glob('*.deleting'):
            job_id = tombstone.name.split('.')[0]
            with self.sessions() as session:
                exists = session.get(DatasetExport, job_id) is not None
            if exists:
                tombstone.replace(tombstone.with_suffix(''))
            else:
                tombstone.unlink(missing_ok=True)

    def close(self):
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=10)
        self.client.close()

    def capture(self, camera_id):
        if not self.capture_lock.acquire(blocking=False):
            raise HTTPException(409, 'A capture is already in progress. Please wait.')
        paths = []
        try:
            with self.sessions() as session:
                camera = session.get(Camera, camera_id)
                if camera is None:
                    raise HTTPException(404, 'Camera not found')
                if not camera.enabled:
                    raise HTTPException(409, 'This camera is disabled')
            try:
                with self.client.stream('GET', '/training-snapshots/' + quote(camera_id, safe='') + '.jpg') as response:
                    if response.status_code != 200 or response.headers.get('X-Capture-Clean') != 'pre-osd-v1':
                        raise HTTPException(503, 'Clean frame capture unavailable. Check camera connection and enable dataset capture on Preview.')
                    try:
                        captured_at = datetime.fromisoformat(response.headers.get('X-Captured-At', ''))
                        if captured_at.tzinfo is None:
                            raise ValueError()
                        captured_at = captured_at.astimezone(timezone.utc)
                    except ValueError:
                        raise HTTPException(503, 'The clean frame capture has no valid capture timestamp') from None
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_IMAGE_BYTES:
                            raise HTTPException(503, 'Capture exceeds the image size limit')
            except httpx.HTTPError:
                raise HTTPException(503, 'Could not capture a clean frame from the camera') from None
            if not data.startswith(b'\xff\xd8'):
                raise HTTPException(503, 'The camera did not return a JPEG image')
            image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None or image.shape[0] * image.shape[1] > 32_000_000:
                raise HTTPException(503, 'The captured image is invalid')
            image_id = str(uuid4())
            file_path = f'images/{image_id}.jpg'
            original, thumb = self.path(file_path), self.path(f'images/{image_id}.thumb.jpg')
            original.parent.mkdir(parents=True, exist_ok=True)
            paths = [original, thumb]
            with self.sessions.begin() as session:
                last = session.scalar(select(DatasetImage).where(DatasetImage.camera_id == camera_id)
                                      .order_by(DatasetImage.captured_at.desc()).limit(1))
                capture_session = (last.capture_session if last and 0 <= (captured_at - aware(last.captured_at)).total_seconds() < 1800 else str(uuid4()))
                row = DatasetImage(id=image_id, camera_id=camera_id, captured_at=captured_at,
                    capture_session=capture_session, file_path=file_path, width=image.shape[1], height=image.shape[0],
                    sha256=hashlib.sha256(data).hexdigest(), boxes=[], status='pending', reviewed_empty=False, revision=1)
                session.add(row)
                session.flush()
                original.write_bytes(data)
                scale = min(1, 400 / image.shape[1])
                small = cv2.resize(image, (max(1, round(image.shape[1] * scale)), max(1, round(image.shape[0] * scale))))
                ok, encoded = cv2.imencode('.jpg', small, [cv2.IMWRITE_JPEG_QUALITY, 80])
                if not ok:
                    raise OSError()
                thumb.write_bytes(encoded.tobytes())
                result = image_out(row)
            paths = []
            return result
        except IntegrityError:
            raise HTTPException(409, 'This exact frame has already been captured') from None
        except OSError:
            raise HTTPException(503, 'Dataset storage is not writable') from None
        finally:
            try:
                for path in paths:
                    with suppress(OSError):
                        path.unlink(missing_ok=True)
            finally:
                self.capture_lock.release()

    def export(self, job_id):
        partial = self.path(f'exports/{job_id}.zip.partial')
        final = self.path(f'exports/{job_id}.zip')
        try:
            with self.sessions() as session:
                if session.get_bind().dialect.name == 'postgresql':
                    session.connection(execution_options={'isolation_level': 'REPEATABLE READ'})
                images = list(session.scalars(select(DatasetImage).where(DatasetImage.status == 'reviewed').order_by(DatasetImage.id).limit(5001)))
                job = session.get(DatasetExport, job_id)
                seed = job.seed
            if not images or len(images) > 5000:
                raise HTTPException(422, 'Export requires 1 to 5000 reviewed images')
            assignment = split_sessions(images, seed)
            counts = {name: 0 for name in ('train', 'val', 'test')}
            manifest = {'classes': CLASSES, 'seed': seed, 'images': [], 'session_splits': assignment, 'warnings': []}
            partial.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(partial, 'w', zipfile.ZIP_STORED, allowZip64=True) as archive:
                archive.writestr('data.yaml', 'train: images/train\nval: images/val\ntest: images/test\nnames:\n  0: car\n')
                for split in counts:
                    archive.writestr(f'images/{split}/', '')
                    archive.writestr(f'labels/{split}/', '')
                for image in images:
                    if self.stop.is_set():
                        raise HTTPException(503, 'Export interrupted. Create a new export.')
                    boxes = [Box.model_validate(box) for box in image.boxes]
                    validate_boxes(boxes, image.width, image.height)
                    if any(box.class_id != 0 for box in boxes):
                        raise HTTPException(422, 'Car-only export requires reviewing legacy truck/bus labels. Existing labels were not changed.')
                    if not boxes and not image.reviewed_empty:
                        raise HTTPException(422, 'An empty image was not explicitly reviewed as having no vehicles')
                    path = self.path(image.file_path)
                    if not path.is_file():
                        raise HTTPException(422, 'A reviewed image file is missing')
                    if hashlib.sha256(path.read_bytes()).hexdigest() != image.sha256:
                        raise HTTPException(422, 'A reviewed image file has changed')
                    split = assignment[image.capture_session]
                    lines = ['%d %.8f %.8f %.8f %.8f' % (box.class_id,
                        (box.x + box.width / 2) / image.width, (box.y + box.height / 2) / image.height,
                        box.width / image.width, box.height / image.height) for box in boxes]
                    archive.write(path, f'images/{split}/{image.id}.jpg')
                    archive.writestr(f'labels/{split}/{image.id}.txt', '\n'.join(lines) + ('\n' if lines else ''))
                    counts[split] += 1
                    manifest['images'].append({'id': image.id, 'revision': image.revision, 'camera_id': image.camera_id,
                        'captured_at': aware(image.captured_at).isoformat(), 'session': image.capture_session,
                        'split': split, 'box_count': len(boxes), 'sha256': image.sha256})
                for split in ('val', 'test'):
                    if counts[split] < 10:
                        manifest['warnings'].append(f'{split}: only {counts[split]} images; reliable evaluation needs more independent capture sessions.')
                manifest['counts'] = counts
                archive.writestr('manifest.json', json.dumps(manifest, indent=2))
            partial.replace(final)
            with self.sessions.begin() as session:
                job = session.get(DatasetExport, job_id)
                job.status, job.manifest = 'ready', manifest
        except Exception as error:
            for path in (partial, final):
                with suppress(OSError):
                    path.unlink(missing_ok=True)
            with self.sessions.begin() as session:
                job = session.get(DatasetExport, job_id)
                job.status, job.error = 'failed', (error.detail if isinstance(error, HTTPException) else 'Dataset export failed. Check storage, images and annotations.')
        finally:
            self.export_lock.release()


def auto_labels_available(config):
    """Auto labelling needs the admin token (POST /api/training/autolabels) and the GPU worker token."""
    return bool(config.training_admin_token and config.training_worker_token)


@router.get('/classes')
def classes(request: Request):
    return {'classes': CLASSES, 'auto_labels_available': auto_labels_available(request.app.state.config)}


@router.post('/capture', status_code=201)
def capture(request: Request, body: CaptureRequest):
    return service(request).capture(body.camera_id)


@router.get('/images')
def images(request: Request, camera_id: str | None = None,
           status: Literal['pending', 'in_progress', 'reviewed'] | None = None,
           page: int = Query(1, ge=1), page_size: int = Query(24, ge=1, le=100)):
    with request.app.state.sessions() as session:
        query = select(DatasetImage)
        if camera_id:
            query = query.where(DatasetImage.camera_id == camera_id)
        if status:
            query = query.where(DatasetImage.status == status)
        total = session.scalar(select(func.count()).select_from(query.subquery()))
        rows = session.scalars(query.order_by(DatasetImage.captured_at.desc(), DatasetImage.id.desc())
            .offset((page - 1) * page_size).limit(page_size))
        return {'items': [image_out(row) for row in rows], 'total': total, 'page': page, 'page_size': page_size}


@router.get('/images/{image_id}')
def detail(request: Request, image_id: UUID):
    with request.app.state.sessions() as session:
        image = find_image(session, image_id)
        ids = list(session.scalars(select(DatasetImage.id).where(DatasetImage.camera_id == image.camera_id)
            .order_by(DatasetImage.captured_at, DatasetImage.id)))
        index = ids.index(image.id)
        return {**image_out(image), 'boxes': image.boxes, 'reviewed_empty': image.reviewed_empty,
                'previous_id': ids[index - 1] if index else None, 'next_id': ids[index + 1] if index + 1 < len(ids) else None}


@router.get('/images/{image_id}/file')
@router.get('/images/{image_id}/thumbnail')
def image_file(request: Request, image_id: UUID):
    with request.app.state.sessions() as session:
        image = find_image(session, image_id)
        relative = image.file_path if request.url.path.endswith('/file') else f'images/{image.id}.thumb.jpg'
    path = service(request).path(relative)
    if not path.is_file():
        raise HTTPException(404, 'Image file not found')
    return FileResponse(path, media_type='image/jpeg', headers={'Cache-Control': 'no-store'})


@router.put('/images/{image_id}/annotations')
def annotations(request: Request, image_id: UUID, body: AnnotationSave):
    with request.app.state.sessions.begin() as session:
        image = find_image(session, image_id, lock=True)
        if body.revision != image.revision:
            raise HTTPException(409, 'This image was edited elsewhere. Reload before saving.')
        validate_boxes(body.boxes, image.width, image.height)
        if body.status == 'reviewed' and not body.boxes and not body.reviewed_empty:
            raise HTTPException(422, 'Confirm that this image has no vehicles before reviewing an empty label')
        image.boxes = [box.model_dump(mode='json', exclude_none=True) for box in body.boxes]
        image.status, image.reviewed_empty = body.status, body.reviewed_empty and not body.boxes
        image.revision += 1
        image.updated_at = utcnow()
        return {**image_out(image), 'boxes': image.boxes, 'reviewed_empty': image.reviewed_empty}


@router.delete('/images/{image_id}', status_code=204)
def delete_image(request: Request, image_id: UUID):
    moved = []
    try:
        with request.app.state.sessions.begin() as session:
            image = find_image(session, image_id, lock=True)
            for relative in (image.file_path, f'images/{image.id}.thumb.jpg'):
                path = service(request).path(relative)
                if path.is_file():
                    tombstone = path.with_name(path.name + '.deleting')
                    path.replace(tombstone)
                    moved.append((path, tombstone))
            session.delete(image)
    except Exception as error:
        for path, tombstone in moved:
            with suppress(OSError):
                if tombstone.exists():
                    tombstone.replace(path)
        if isinstance(error, OSError):
            raise HTTPException(503, 'Dataset storage is not writable') from None
        raise
    for _, tombstone in moved:
        # DB commit already succeeded; startup recovery removes leftover tombstones.
        with suppress(OSError):
            tombstone.unlink(missing_ok=True)
    return Response(status_code=204)


def export_out(job):
    return {'id': job.id, 'status': job.status, 'created_at': aware(job.created_at).isoformat(),
            'error': job.error, 'manifest': ({key: value for key, value in job.manifest.items() if key in ('classes', 'seed', 'counts', 'warnings')} if job.manifest else None),
            'download_url': f'/api/datasets/exports/{job.id}/download' if job.status == 'ready' else None}


@router.get('/exports')
def exports(request: Request):
    with request.app.state.sessions() as session:
        return [export_out(job) for job in session.scalars(select(DatasetExport).order_by(DatasetExport.created_at.desc()).limit(20))]


@router.post('/exports', status_code=202)
def create_export(request: Request, body: ExportRequest):
    worker = service(request)
    if not worker.export_lock.acquire(blocking=False):
        raise HTTPException(409, 'A dataset export is already being built')
    try:
        with request.app.state.sessions.begin() as session:
            count = session.scalar(select(func.count()).select_from(DatasetImage).where(DatasetImage.status == 'reviewed'))
            if not 1 <= count <= 5000:
                raise HTTPException(422, 'Export requires 1 to 5000 reviewed images')
            job = DatasetExport(id=str(uuid4()), seed=body.seed)
            session.add(job)
            session.flush()
            result = export_out(job)
        thread = threading.Thread(target=worker.export, args=(job.id,), daemon=True, name='dataset-export')
        worker.threads = [t for t in worker.threads if t.is_alive()]
        worker.threads.append(thread)
        thread.start()
        return result
    except Exception:
        worker.export_lock.release()
        raise


@router.delete('/exports/{job_id}', status_code=204)
def delete_export(request: Request, job_id: UUID):
    moved = []
    try:
        with request.app.state.sessions.begin() as session:
            job = session.scalar(select(DatasetExport).where(DatasetExport.id == str(job_id)).with_for_update())
            if job is None:
                raise HTTPException(404, 'Dataset export not found')
            if job.status == 'building':
                raise HTTPException(409, 'Wait for the dataset export to finish before deleting it')
            for relative in (f'exports/{job_id}.zip', f'exports/{job_id}.zip.partial'):
                path = service(request).path(relative)
                if path.is_file():
                    tombstone = path.with_name(path.name + '.deleting')
                    path.replace(tombstone)
                    moved.append((path, tombstone))
            session.delete(job)
    except Exception as error:
        for path, tombstone in moved:
            with suppress(OSError):
                if tombstone.exists():
                    tombstone.replace(path)
        if isinstance(error, OSError):
            raise HTTPException(503, 'Dataset storage is not writable') from None
        raise
    for _, tombstone in moved:
        with suppress(OSError):
            tombstone.unlink(missing_ok=True)
    return Response(status_code=204)


@router.get('/exports/{job_id}/download')
def download(request: Request, job_id: UUID):
    with request.app.state.sessions() as session:
        job = session.get(DatasetExport, str(job_id))
        if job is None:
            raise HTTPException(404, 'Dataset export not found')
        if job.status != 'ready':
            raise HTTPException(409, 'Dataset export is not ready')
    path = service(request).path(f'exports/{job_id}.zip')
    if not path.is_file():
        raise HTTPException(404, 'Dataset archive not found')
    return FileResponse(path, media_type='application/zip', filename=f'dataset_{job_id}.zip', headers={'Cache-Control': 'no-store'})
