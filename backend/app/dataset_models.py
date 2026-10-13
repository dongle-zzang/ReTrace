"""Additive dataset metadata; image bytes live on the dataset volume."""
from datetime import datetime, timezone
from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base


def now():
    return datetime.now(timezone.utc)


class DatasetImage(Base):
    __tablename__ = 'dataset_images'
    __table_args__ = (UniqueConstraint('camera_id', 'sha256', name='dataset_unique_frame'),
                      CheckConstraint("status IN ('pending','in_progress','reviewed')", name='dataset_image_status'))
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    camera_id: Mapped[str] = mapped_column(ForeignKey('cameras.camera_id'), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    capture_session: Mapped[str] = mapped_column(String(36), index=True)
    file_path: Mapped[str] = mapped_column(String(128))
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default='pending')
    boxes: Mapped[list] = mapped_column(JSON, default=list)
    reviewed_empty: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class DatasetExport(Base):
    __tablename__ = 'dataset_exports'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    status: Mapped[str] = mapped_column(String(16), default='building')
    seed: Mapped[int] = mapped_column(Integer)
    manifest: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(String(256))
