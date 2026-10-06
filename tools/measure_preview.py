#!/usr/bin/env python3
"""Measure existing MJPEG HTTP output; no RTSP access or media recording."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time
from urllib.parse import urljoin, urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]


def fetch(url):
    with urlopen(url, timeout=5) as response:
        return response.read()


def measure_mjpeg(url, seconds):
    started = time.monotonic()
    first = last = None
    frames = size = errors = skipped = 0
    previous_sequence = None
    max_gap_ms = 0.0
    gaps_over_100ms = 0
    try:
        with urlopen(url, timeout=5) as response:
            if "multipart/x-mixed-replace" not in response.headers.get("Content-Type", ""):
                raise ValueError("Unexpected stream content type")
            while time.monotonic() - started < seconds:
                line = response.readline(4096)
                if not line:
                    raise EOFError("Stream ended")
                if not line.startswith(b"--frame"):
                    continue
                headers = {}
                for _ in range(20):
                    line = response.readline(4096)
                    if line == b"\r\n":
                        break
                    key, value = line.decode("ascii").split(":", 1)
                    headers[key.lower()] = value.strip()
                else:
                    raise ValueError("Oversized multipart headers")
                length = int(headers["content-length"])
                if not 0 < length <= 16 * 1024 * 1024:
                    raise ValueError("Invalid JPEG size")
                jpeg = response.read(length)
                if len(jpeg) != length:
                    raise EOFError("Truncated JPEG")
                now = time.monotonic()
                if last is not None:
                    gap = (now - last) * 1000
                    max_gap_ms = max(max_gap_ms, gap)
                    gaps_over_100ms += gap > 100
                if first is None:
                    first = now
                last = now
                sequence = int(headers["x-frame-sequence"]) if "x-frame-sequence" in headers else None
                if sequence is not None and previous_sequence is not None:
                    skipped += max(0, sequence - previous_sequence - 1)
                previous_sequence = sequence
                frames += 1
                size += length
    except (OSError, ValueError, EOFError, KeyError):
        # Never print URLs, credentials, or untrusted response text.
        errors += 1
    elapsed = time.monotonic() - started
    return {
        "elapsed_s": round(elapsed, 2), "frames": frames,
        "client_fps": round((frames - 1) / (last - first), 2) if frames > 1 and last > first else 0,
        "first_payload_s": None if first is None else round(first - started, 2),
        "max_gap_ms": round(max_gap_ms, 2), "gaps_over_100ms": gaps_over_100ms,
        "skipped_frames": skipped, "received_mbps": round(size * 8 / max(elapsed, 1e-9) / 1e6, 2),
        "silence_at_end_s": None if last is None else round(time.monotonic() - last, 2),
        "errors": errors,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080/")
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--source", action="append", type=int)
    parser.add_argument("--report", type=Path, help="Optional JSON path inside the project")
    args = parser.parse_args()
    address = urlsplit(args.base_url)
    if address.scheme not in ("http", "https") or address.username is not None:
        parser.error("Use an HTTP(S) preview URL without credentials")
    if not 0 < args.seconds <= 3600:
        parser.error("seconds must be within (0, 3600]")
    if args.report is not None:
        args.report = args.report.resolve()
        if not args.report.is_relative_to(ROOT):
            parser.error("Report must be inside the project")
    try:
        streams = json.loads(fetch(urljoin(args.base_url, "/streams.json")))
    except (OSError, ValueError):
        print("Preview HTTP server unavailable or invalid /streams.json")
        return 1
    if args.source is not None:
        streams = [stream for stream in streams if stream["id"] in args.source]
        if {stream["id"] for stream in streams} != set(args.source):
            parser.error("Requested source was not advertised by the preview server")
    if not streams:
        parser.error("No streams to measure")
    def measure(stream):
        return {"source": stream["id"], "mode": "mjpeg",
                **measure_mjpeg(urljoin(args.base_url, stream["url"]), args.seconds)}

    with ThreadPoolExecutor(max_workers=len(streams)) as pool:
        results = list(pool.map(measure, streams))
    document = json.dumps(results, ensure_ascii=False, indent=2)
    print(document)
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(document + "\n", encoding="utf-8")
    return int(any(result["errors"] or not result["frames"]
                   for result in results))


if __name__ == "__main__":
    raise SystemExit(main())
