from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Boolean, create_engine
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
