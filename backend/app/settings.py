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

    dataset_dir: Path = Path("dataset-storage")

    color_parking_interval: float = 1.0
    color_parking_stale_after: float = 5.0

    # Web training (backend/app/training.py). Empty tokens disable training control / the worker API.
    training_admin_token: str = ""
    training_worker_token: str = ""
    # Shared V100: defaults come from the 2026-10-13 measurement in training/README.md §2
    # (Preview alone ~4.8 GiB; 640/b4 adds ~3.3 GiB, 960/b4 ~7.2 GiB). Re-measure and override.
    training_min_free_mib: int = 6000
    training_gpu_reserve_mib: int = 2048
    training_max_gpu_util: int = 85
    training_max_fps_drop: float = 0.15
    training_guard_samples: int = 6
    training_worker_stale_seconds: float = 60.0
    training_gpu_wait_seconds: int = 1800
    training_min_storage_mib: int = 2048
    training_require_preview: bool = True

    def __post_init__(self):
        if not .5 <= self.color_parking_interval <= 60:
            raise ValueError("Color parking interval must be 0.5..60 seconds")
        if not self.color_parking_interval * 2 <= self.color_parking_stale_after <= 300:
            raise ValueError("Color parking TTL must be at least two intervals and at most 300 seconds")
        for token in (self.training_admin_token, self.training_worker_token):
            if token and len(token) < 24:
                raise ValueError("Training tokens must be at least 24 characters")
        if self.training_admin_token and self.training_admin_token == self.training_worker_token:
            raise ValueError("Training admin and worker tokens must differ")
        if not (0 <= self.training_min_free_mib <= 65536 and 0 <= self.training_gpu_reserve_mib <= 65536
                and 1 <= self.training_max_gpu_util <= 100 and 0 < self.training_max_fps_drop < 1
                and 1 <= self.training_guard_samples <= 120 and 15 <= self.training_worker_stale_seconds <= 3600
                and 0 <= self.training_gpu_wait_seconds <= 86400 and 0 <= self.training_min_storage_mib):
            raise ValueError("Invalid training resource limits")

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
            dataset_dir=Path(os.environ.get("DATASET_STORAGE_DIR", "/app/dataset-storage")),
            preview_url=os.environ.get("BACKEND_PREVIEW_URL", "http://retrace:40225"),
            cors_origins=cls.cors_origins_from_env(),
            color_parking_interval=float(os.environ.get("COLOR_PARKING_INTERVAL", "1")),
            color_parking_stale_after=float(os.environ.get("COLOR_PARKING_STALE_AFTER", "5")),
            parking_policy=ParkingPolicy(**json.loads(os.environ.get("PARKING_POLICY") or "{}")),
            training_admin_token=os.environ.get("TRAINING_ADMIN_TOKEN", ""),
            training_worker_token=os.environ.get("TRAINING_WORKER_TOKEN", ""),
            training_min_free_mib=int(os.environ.get("TRAINING_MIN_FREE_MIB") or 6000),
            training_gpu_reserve_mib=int(os.environ.get("TRAINING_GPU_RESERVE_MIB") or 2048),
            training_max_gpu_util=int(os.environ.get("TRAINING_MAX_GPU_UTIL") or 85),
            training_max_fps_drop=float(os.environ.get("TRAINING_MAX_FPS_DROP") or 0.15),
            training_guard_samples=int(os.environ.get("TRAINING_GUARD_SAMPLES") or 6),
            training_worker_stale_seconds=float(os.environ.get("TRAINING_WORKER_STALE_SECONDS") or 60),
            training_gpu_wait_seconds=int(os.environ.get("TRAINING_GPU_WAIT_SECONDS") or 1800),
            training_min_storage_mib=int(os.environ.get("TRAINING_MIN_STORAGE_MIB") or 2048),
            training_require_preview=os.environ.get("TRAINING_REQUIRE_PREVIEW", "1") != "0",
        )
