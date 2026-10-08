from dataclasses import dataclass, field
import json
import os
from pathlib import Path

from sqlalchemy import URL
from .occupancy import ParkingPolicy


@dataclass(frozen=True, repr=False)
class Settings:
    database_url: str | URL
    cameras_path: Path
    preview_url: str = "http://retrace:40225"
    poll_interval: float = 2.0
    http_timeout: float = 1.5
    stale_after: float = 10.0
    track_retention_days: int = 7
    cors_origins: tuple[str, ...] = ()
    parking_policy: ParkingPolicy = field(default_factory=ParkingPolicy)

    color_parking_interval: float = 1.0
    color_parking_stale_after: float = 5.0

    def __post_init__(self):
        if not .5 <= self.color_parking_interval <= 60:
            raise ValueError("Color parking interval must be 0.5..60 seconds")
        if not self.color_parking_interval * 2 <= self.color_parking_stale_after <= 300:
            raise ValueError("Color parking TTL must be at least two intervals and at most 300 seconds")

    @staticmethod
    def cors_origins_from_env():
        origins = tuple(origin.strip() for origin in
                        os.environ.get("BACKEND_CORS_ORIGINS", "").split(",") if origin.strip())
        if any("*" in origin for origin in origins):
            raise RuntimeError("Backend CORS requires explicit origins")
        return origins

    @classmethod
    def from_env(cls):
        # Do not load .env: Compose injects only explicitly whitelisted settings.
        password = os.environ.get("POSTGRES_PASSWORD")
        if not password:
            raise RuntimeError("Backend database credentials are required")
        return cls(
            URL.create("postgresql+psycopg", username=os.environ.get("POSTGRES_USER", "retrace"),
                       password=password, host="postgres", port=5432,
                       database=os.environ.get("POSTGRES_DB", "retrace")),
            Path(os.environ.get("CAMERAS_PATH", "/app/configs/cameras.yaml")),
            preview_url=os.environ.get("BACKEND_PREVIEW_URL", "http://retrace:40225"),
            cors_origins=cls.cors_origins_from_env(),
            color_parking_interval=float(os.environ.get("COLOR_PARKING_INTERVAL", "1")),
            color_parking_stale_after=float(os.environ.get("COLOR_PARKING_STALE_AFTER", "5")),
            parking_policy=ParkingPolicy(**json.loads(os.environ.get("PARKING_POLICY") or "{}")),
        )
