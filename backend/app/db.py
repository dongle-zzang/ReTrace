from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import JSON, CheckConstraint, DateTime, Float, ForeignKey, Integer, String, Boolean, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class Camera(Base):
    __tablename__ = "cameras"
    camera_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    floor: Mapped[int | str] = mapped_column(JSON)
    name: Mapped[str] = mapped_column(String(256))
    enabled: Mapped[bool] = mapped_column(Boolean)
    source_id: Mapped[int | None] = mapped_column(Integer)


class CameraStatus(Base):
    __tablename__ = "camera_status"
    camera_id: Mapped[str] = mapped_column(ForeignKey("cameras.camera_id"), primary_key=True)
    state: Mapped[str] = mapped_column(String(16), default="offline")
    fps: Mapped[float] = mapped_column(Float, default=0)
    last_frame_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(40))
    reconnect_count: Mapped[int] = mapped_column(Integer, default=0)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    runtime_session: Mapped[str | None] = mapped_column(String(32))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PersonTrack(Base):
    __tablename__ = "person_tracks"
    camera_id: Mapped[str] = mapped_column(ForeignKey("cameras.camera_id"), primary_key=True)
    runtime_session: Mapped[str] = mapped_column(String(32), primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, primary_key=True)
    # NvDCF IDs are unsigned uint64; PostgreSQL BIGINT is signed. Decimal text is lossless.
    track_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    max_confidence: Mapped[float | None] = mapped_column(Float)


class ParkingSpace(Base):
    __tablename__ = "parking_spaces"
    __table_args__ = (
        CheckConstraint("occupancy IN ('occupied', 'empty', 'unknown')", name="parking_occupancy_valid"),
        CheckConstraint("revision >= 1", name="parking_revision_positive"),
    )
    space_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    camera_id: Mapped[str] = mapped_column(ForeignKey("cameras.camera_id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    # Unclosed, simple polygon in normalized full-image coordinates; no pixel dimensions.
    polygon: Mapped[list] = mapped_column(JSON)
    occupancy: Mapped[str] = mapped_column(String(16), default="unknown", server_default="unknown")
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    occupancy_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class ParkingEvent(Base):
    """Transitions only; survive a space deletion without a dangling FK."""
    __tablename__ = "parking_events"
    __table_args__ = {"sqlite_autoincrement": True}
    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    camera_id: Mapped[str] = mapped_column(ForeignKey("cameras.camera_id"), index=True)
    space_id: Mapped[str] = mapped_column(String(36))
    revision: Mapped[int] = mapped_column(Integer)
    previous_occupancy: Mapped[str] = mapped_column(String(16))
    occupancy: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(40))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    evidence: Mapped[dict] = mapped_column(JSON)


def make_engine(url):
    options = dict(pool_pre_ping=True, hide_parameters=True, echo=False)
    if str(url).startswith("sqlite"):
        options["connect_args"] = {"check_same_thread": False}
    else:
        options.update(pool_timeout=2, connect_args={"connect_timeout": 2,
                       "options": "-c statement_timeout=3000 -c lock_timeout=2000"})
    return create_engine(url, **options)


def sessions_for(engine):
    return sessionmaker(engine, expire_on_commit=False)


class ColorParkingSpace(Base):
    """Logical spaces, independent of the legacy camera-scoped vehicle detector."""
    __tablename__ = "color_parking_spaces"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    label: Mapped[str] = mapped_column(String(128), unique=True)


class ParkingZone(Base):
    __tablename__ = "parking_zones"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    camera_id: Mapped[str] = mapped_column(ForeignKey("cameras.camera_id"), index=True)
    parking_space_id: Mapped[str] = mapped_column(ForeignKey("color_parking_spaces.id"), index=True)
    polygon: Mapped[list] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    threshold: Mapped[float] = mapped_column(Float, default=0.3)
    hysteresis: Mapped[float] = mapped_column(Float, default=0.05)
    confirm_frames: Mapped[int] = mapped_column(Integer, default=3)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    baseline: Mapped[list | None] = mapped_column(JSON)
