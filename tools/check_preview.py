#!/usr/bin/env python3
"""Verify the deployed preview inside the Linux container, without GPU imports."""

import json
from pathlib import Path
from urllib.request import urlopen


def main():
    listeners = []
    for name in ("tcp", "tcp6"):
        for line in Path(f"/proc/net/{name}").read_text().splitlines()[1:]:
            fields = line.split()
            if fields[3] == "0A":  # TCP_LISTEN
                address, port = fields[1].split(":")
                listeners.append((name, address, int(port, 16)))
    if ("tcp", "00000000", 40225) not in listeners:
        raise SystemExit("FAIL: no IPv4 listener on 0.0.0.0:40225")
    print("PASS: 0.0.0.0:40225 LISTEN", flush=True)
    old_ports = sorted({port for _, _, port in listeners if port in (3000, 8080)})
    if old_ports:
        raise SystemExit(f"FAIL: obsolete ports still listening: {old_ports}")
    print("PASS: no listeners on 3000 or 8080", flush=True)
    with urlopen("http://127.0.0.1:40225/streams.json", timeout=5) as response:
        if response.status != 200:
            raise SystemExit(f"FAIL: HTTP status {response.status}")
        streams = json.load(response)
        print(f"PASS: /streams.json HTTP {response.status}", flush=True)
    if not isinstance(streams, list) or not streams:
        raise SystemExit("FAIL: preview has no configured streams")
    print(f"PASS: preview API exposes {len(streams)} configured streams", flush=True)


if __name__ == "__main__":
    main()
