#!/usr/bin/env python3
"""Verify the deployed preview inside the Linux container, without GPU imports."""

import json
import os
from pathlib import Path
import socket
from urllib.request import urlopen


def main():
    bind_ip = os.environ.get("WEB_BIND_IP", "0.0.0.0")
    port = int(os.environ.get("WEB_PORT", "40225"))
    encoded_ip = socket.inet_aton(bind_ip)[::-1].hex().upper()
    listeners = []
    for name in ("tcp", "tcp6"):
        for line in Path(f"/proc/net/{name}").read_text().splitlines()[1:]:
            fields = line.split()
            if fields[3] == "0A":  # TCP_LISTEN
                address, port_hex = fields[1].split(":")
                listeners.append((name, address, int(port_hex, 16)))
    if ("tcp", encoded_ip, port) not in listeners:
        raise SystemExit("FAIL: no IPv4 listener on configured Preview interface")
    print("PASS: configured Preview listener", flush=True)
    old_ports = sorted({value for _, _, value in listeners if value in (3000, 8080)}) if bind_ip == "0.0.0.0" else []
    if old_ports:
        raise SystemExit(f"FAIL: obsolete ports still listening: {old_ports}")
    if bind_ip == "0.0.0.0":
        print("PASS: no listeners on 3000 or 8080", flush=True)
    host = "127.0.0.1" if bind_ip == "0.0.0.0" else bind_ip
    with urlopen(f"http://{host}:{port}/streams.json", timeout=5) as response:
        if response.status != 200:
            raise SystemExit(f"FAIL: HTTP status {response.status}")
        streams = json.load(response)
        print(f"PASS: /streams.json HTTP {response.status}", flush=True)
    if not isinstance(streams, list) or not streams:
        raise SystemExit("FAIL: preview has no configured streams")
    print(f"PASS: preview API exposes {len(streams)} configured streams", flush=True)


if __name__ == "__main__":
    main()
