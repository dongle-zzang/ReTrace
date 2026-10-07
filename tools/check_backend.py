#!/usr/bin/env python3
"""Check live REST/preview JSON for secret leakage; print only safe summaries."""
import argparse
import json
from pathlib import Path
import sys
from urllib.parse import quote, unquote, urlsplit
from urllib.request import urlopen

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_KEYS = {"rtsp_url", "rtsp_env", "username", "password", "db_password", "postgres_password"}


def secret_tokens(env_path):
    values = dotenv_values(env_path, interpolate=False)
    tokens = set()
    for key, value in values.items():
        if not value:
            continue
        if key == "POSTGRES_PASSWORD":
            tokens.add(value)
        if value.lower().startswith("rtsp://"):
            tokens.add(value)
            parsed = urlsplit(value)
            tokens.update(token for token in (parsed.hostname, parsed.username, parsed.password,
                                              unquote(parsed.username or ""), unquote(parsed.password or "")) if token)
    return tokens


def safe_document(document, tokens):
    def inspect(value):
        if isinstance(value, dict):
            return all(str(key).lower() not in FORBIDDEN_KEYS and inspect(child) for key, child in value.items())
        if isinstance(value, list):
            return all(inspect(child) for child in value)
        if isinstance(value, str):
            return "rtsp://" not in value.lower() and all(token not in value for token in tokens)
        return True
    return inspect(document)


def fetch(url):
    with urlopen(url, timeout=5) as response:
        body = response.read(8 * 1024 * 1024 + 1)
        if len(body) > 8 * 1024 * 1024:
            raise ValueError("Response too large")
        return json.loads(body)


def check(base_url, preview_url, env_path):
    tokens = secret_tokens(env_path)
    cameras = fetch(base_url + "/api/cameras")
    documents = [cameras, fetch(base_url + "/api/health"), fetch(base_url + "/api/status")]
    for camera in cameras:
        path = "/api/cameras/" + quote(camera["camera_id"], safe="")
        documents.extend(fetch(base_url + path + suffix) for suffix in ("", "/status", "/tracks"))
    documents.extend(fetch(preview_url + path) for path in ("/streams.json", "/metadata.json"))
    if not all(safe_document(document, tokens) for document in documents):
        print("FAIL: Sensitive data detected; response contents withheld.", file=sys.stderr)
        return 1
    print(f"PASS: Backend/preview JSON checked; cameras={len(cameras)}; no sensitive values detected.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://backend:8000")
    parser.add_argument("--preview-url", default="http://127.0.0.1:40225")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    args = parser.parse_args()
    try:
        return check(args.base_url.rstrip("/"), args.preview_url.rstrip("/"), args.env_file)
    except Exception:
        print("FAIL: Backend/preview verification unavailable; details withheld.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
