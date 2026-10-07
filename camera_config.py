"""Public camera metadata and private RTSP resolution; no GPU dependencies."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

import yaml
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent


class CameraConfigError(ValueError):
    """Safe to display: never contains a config value or RTSP address."""


@dataclass(frozen=True)
class Camera:
    source_id: int
    camera_id: str
    floor: int | str | None
    name: str
    rtsp_url: str = field(repr=False)
    rtsp_env: str | None = None

    def public_info(self):
        return {"camera_id": self.camera_id, "floor": self.floor, "name": self.name}


def validate_rtsp(uri, label):
    try:
        parsed = urlsplit(uri)
        valid = parsed.scheme.lower() == "rtsp" and bool(parsed.hostname)
        parsed.port
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise CameraConfigError(f"{label}: invalid RTSP URI; value hidden")


def resolve_camera_rtsp(camera, env_path=ROOT / ".env", environ=None, require_env_file=False):
    """Resolve only this camera for a new attempt; never return a cached secret.

    For named cameras, an existing .env is authoritative, including missing or
    empty keys. This avoids Compose's stale inherited environment overriding a
    file edited during runtime. Environment-only setups reread os.environ.
    CLI cameras have no rtsp_env and retain their explicit URI.
    """
    label = f"camera id={camera.camera_id}"
    if camera.rtsp_env is None:
        uri = camera.rtsp_url
    else:
        env_path = Path(env_path)
        try:
            if env_path.is_file():
                values = dotenv_values(env_path, interpolate=False)
            elif require_env_file:
                raise CameraConfigError(f"{label}: private env file missing; contents hidden")
            else:
                values = os.environ if environ is None else environ
            uri = (values.get(camera.rtsp_env) or "").strip()
        except (OSError, UnicodeError):
            raise CameraConfigError(f"{label}: cannot read private env file; contents hidden") from None
        if not uri:
            raise CameraConfigError(f"{label}: missing RTSP variable {camera.rtsp_env}; value hidden")
    validate_rtsp(uri, label)
    return uri


def load_cameras(config_path=ROOT / "configs/cameras.yaml", env_path=ROOT / ".env",
                 cli_inputs=None, environ=None):
    """CLI wins; otherwise YAML defines identities, env overrides .env values.

    Missing .env is allowed when all configured secrets are in the environment.
    .env interpolation is disabled so credentials containing '$' stay literal.
    """
    if cli_inputs is not None:
        cameras = []
        for index, uri in enumerate(cli_inputs):
            validate_rtsp(uri, f"source={index}")
            cameras.append(Camera(index, f"cli_{index}", None, f"Camera {index}", uri))
        return cameras
    try:
        env_path = Path(env_path)
        private = dotenv_values(env_path, interpolate=False) if env_path.is_file() else {}
    except (OSError, UnicodeError):
        raise CameraConfigError("Cannot read private .env file; contents hidden") from None
    values = {**private, **(os.environ if environ is None else environ)}
    cameras = []
    for entry in read_camera_entries(config_path):
        camera_id = entry["id"]
        label = f"camera id={camera_id}"
        enabled = entry.get("enabled", True)
        name, variable = entry["name"], entry["rtsp_env"]
        if not enabled:
            continue
        uri = (values.get(variable) or "").strip()
        if not uri:
            raise CameraConfigError(f"{label}: Missing RTSP input: set {variable} in .env or environment")
        validate_rtsp(uri, label)
        cameras.append(Camera(len(cameras), camera_id, entry["floor"], name, uri, rtsp_env=variable))
    if not cameras:
        raise CameraConfigError("Camera YAML has no enabled cameras")
    return cameras


def read_camera_entries(config_path=ROOT / "configs/cameras.yaml"):
    """Validate public YAML without opening .env or resolving any RTSP values.

    Internal entries include rtsp_env; API consumers must use load_public_cameras.
    """
    try:
        document = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        raise CameraConfigError("Cannot read camera YAML: missing file or invalid syntax; contents hidden") from None
    if not isinstance(document, dict) or set(document) != {"cameras"} or not isinstance(document["cameras"], list):
        raise CameraConfigError("Camera YAML must contain a cameras list only")
    seen = set()
    for row, entry in enumerate(document["cameras"], 1):
        label = f"camera row={row}"
        if not isinstance(entry, dict):
            raise CameraConfigError(f"{label}: expected camera mapping")
        camera_id = entry.get("id")
        if not isinstance(camera_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", camera_id):
            raise CameraConfigError(f"{label}: id must use letters, digits, '_' or '-'")
        label += f" id={camera_id}"
        if camera_id in seen:
            raise CameraConfigError(f"{label}: duplicate camera id")
        seen.add(camera_id)
        if set(entry) - {"id", "floor", "name", "rtsp_env", "enabled"}:
            raise CameraConfigError(f"{label}: unsupported field; keep URLs in .env only")
        enabled = entry.get("enabled", True)
        if type(enabled) is not bool:
            raise CameraConfigError(f"{label}: enabled must be true or false")
        floor = entry.get("floor")
        if not (type(floor) is int or isinstance(floor, str) and re.fullmatch(r"B[1-9][0-9]*", floor)):
            raise CameraConfigError(f"{label}: floor must be an integer or basement label such as B1")
        name, variable = entry.get("name"), entry.get("rtsp_env")
        if not isinstance(name, str) or not name.strip() or "://" in name:
            raise CameraConfigError(f"{label}: name must be public camera text")
        if not isinstance(variable, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable):
            raise CameraConfigError(f"{label}: rtsp_env must be an environment variable name")
    return document["cameras"]


def load_public_cameras(config_path=ROOT / "configs/cameras.yaml"):
    """Whitelist public fields, retaining disabled cameras and source ordering."""
    source_id = 0
    public = []
    for entry in read_camera_entries(config_path):
        enabled = entry.get("enabled", True)
        public.append({"camera_id": entry["id"], "floor": entry["floor"],
                       "name": entry["name"], "enabled": enabled,
                       "source_id": source_id if enabled else None})
        source_id += int(enabled)
    return public
