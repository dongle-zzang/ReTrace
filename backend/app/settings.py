from dataclasses import dataclass
import os
from pathlib import Path

from sqlalchemy import URL


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
    preview_mode: str = "webrtc"

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
        mode = os.environ.get("PREVIEW_MODE", "webrtc")
        if mode not in ("webrtc", "mjpeg"):
            raise RuntimeError("Invalid PREVIEW_MODE")
        return cls(
            URL.create("postgresql+psycopg", username=os.environ.get("POSTGRES_USER", "retrace"),
                       password=password, host="postgres", port=5432,
                       database=os.environ.get("POSTGRES_DB", "retrace")),
            Path(os.environ.get("CAMERAS_PATH", "/app/configs/cameras.yaml")),
            preview_url=os.environ.get("BACKEND_PREVIEW_URL", "http://retrace:40225"),
            preview_mode=mode,
            cors_origins=cls.cors_origins_from_env(),
        )
