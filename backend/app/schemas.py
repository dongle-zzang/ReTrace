from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

State = Literal["connecting", "online", "degraded", "offline", "reconnecting"]


class StatusOut(BaseModel):
    camera_id: str
    state: State
    fps: float
    last_frame_at: datetime | None
    last_error: str | None
    reconnect_count: int
    generation: int
    runtime_session: str | None
    observed_at: datetime
    stale: bool


class CameraOut(BaseModel):
    camera_id: str
    floor: int | str
    name: str
    enabled: bool
    source_id: int | None
    preview_path: str | None
    status: StatusOut


class HealthOut(BaseModel):
    status: Literal["ok", "unavailable"]
    database: Literal["ok", "unavailable"]
    preview: Literal["reachable", "unreachable", "unknown"]


class StatusSummary(BaseModel):
    counts: dict[State, int]
    cameras: list[StatusOut]


class BBox(BaseModel):
    x: float = Field(allow_inf_nan=False)
    y: float = Field(allow_inf_nan=False)
    width: float = Field(ge=0, allow_inf_nan=False)
    height: float = Field(ge=0, allow_inf_nan=False)


class PersonIn(BaseModel):
    track_id: int | None = Field(default=None, ge=0, lt=(1 << 64) - 1)
    class_id: Literal[0]
    confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    tracker_confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    bbox: BBox


class FrameIn(BaseModel):
    camera_id: str
    source_id: int = Field(ge=0)
    generation: int = Field(ge=0)
    timestamp: datetime
    frame_number: int = Field(ge=0)
    bbox_width: int = Field(gt=0)
    bbox_height: int = Field(gt=0)
    persons: list[PersonIn] = Field(max_length=1000)

    @field_validator("timestamp")
    @classmethod
    def require_timezone(cls, value):
        if value.tzinfo is None:
            raise ValueError("Timezone required")
        return value


class LiveTrackOut(BaseModel):
    track_id: str | None
    class_id: int
    person_class: Literal["person"] = Field(default="person", serialization_alias="class")
    confidence: float | None
    tracker_confidence: float | None
    bbox: BBox


class TrackOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    camera_id: str
    runtime_session: str
    generation: int
    track_id: str
    first_seen_at: datetime
    last_seen_at: datetime
    max_confidence: float | None


class TracksOut(BaseModel):
    camera_id: str
    runtime_session: str | None
    generation: int
    timestamp: datetime | None
    bbox_width: int | None
    bbox_height: int | None
    current: list[LiveTrackOut]
    recent: list[TrackOut]
