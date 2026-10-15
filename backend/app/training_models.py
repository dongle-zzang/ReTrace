"""Additive training job / model registry tables; weights and logs live on the dataset volume."""
from datetime import datetime
from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base
from .dataset_models import now

JOB_STATUSES = ('queued', 'running', 'stopping', 'completed', 'failed', 'stopped')
ACTIVE_STATUSES = ('queued', 'running', 'stopping')


class TrainingJob(Base):
    __tablename__ = 'training_jobs'
    __table_args__ = (
        CheckConstraint("status IN ('queued','running','stopping','completed','failed','stopped')", name='training_job_status'),
        # 1 for queued/running/stopping, NULL once finished: the unique constraint is the
        # database-level "one training job at a time" guard (NULLs never collide).
        UniqueConstraint('active_slot', name='training_single_active_job'),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default='queued', index=True)
    phase: Mapped[str] = mapped_column(String(24), default='queued')
    message: Mapped[str | None] = mapped_column(String(256))
    active_slot: Mapped[int | None] = mapped_column(Integer)
    export_id: Mapped[str] = mapped_column(String(36), index=True)
    dataset_sha256: Mapped[str] = mapped_column(String(64))
    dataset_summary: Mapped[dict] = mapped_column(JSON)
    base_model_id: Mapped[str] = mapped_column(String(64))
    config: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_id: Mapped[str | None] = mapped_column(String(96))
    epoch: Mapped[int] = mapped_column(Integer, default=0)
    epochs: Mapped[int] = mapped_column(Integer)
    progress: Mapped[float] = mapped_column(Float, default=0)
    metrics: Mapped[list] = mapped_column(JSON, default=list)
    live: Mapped[dict | None] = mapped_column(JSON)
    gpu: Mapped[dict | None] = mapped_column(JSON)
    monitor: Mapped[dict | None] = mapped_column(JSON)
    log_tail: Mapped[list] = mapped_column(JSON, default=list)
    stop_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    stop_reason: Mapped[str | None] = mapped_column(String(256))
    error_code: Mapped[str | None] = mapped_column(String(40))
    error: Mapped[str | None] = mapped_column(String(512))
    run_info: Mapped[dict | None] = mapped_column(JSON)
    evaluation: Mapped[dict | None] = mapped_column(JSON)
    artifacts: Mapped[dict] = mapped_column(JSON, default=dict)
    model_id: Mapped[str | None] = mapped_column(String(64))


class TrainingModel(Base):
    __tablename__ = 'training_models'
    __table_args__ = (
        CheckConstraint("kind IN ('base','finetuned')", name='training_model_kind'),
        UniqueConstraint('name', 'version', name='training_model_version'),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))
    base_model_id: Mapped[str | None] = mapped_column(String(64))
    job_id: Mapped[str | None] = mapped_column(String(36))
    export_id: Mapped[str | None] = mapped_column(String(36))
    dataset_sha256: Mapped[str | None] = mapped_column(String(64))
    class_mode: Mapped[str] = mapped_column(String(16))
    names: Mapped[dict | None] = mapped_column(JSON)
    config: Mapped[dict | None] = mapped_column(JSON)
    metrics: Mapped[dict | None] = mapped_column(JSON)
    evaluation_conclusive: Mapped[bool] = mapped_column(Boolean, default=False)
    files: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    # Never switched automatically: DeepStream rollout needs a separate approval/validation step.
    deployment: Mapped[str] = mapped_column(String(24), default='not_deployed')
