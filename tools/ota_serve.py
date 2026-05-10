#!/usr/bin/env python3
"""
Serve a freshly-built firmware binary over HTTP so the ESP32 can pull it
across the tailnet.

Usage (from the repo root):

    idf.py build
    python3 tools/ota_serve.py            # serves build/esp32-fw.bin on :8000

Then, from any machine on the tailnet:

    curl -X POST http://<device-ts-ip>/ota \\
         -H "X-OTA-Token: <secret>" \\
         -d "{\\"url\\":\\"http://<your-ts-ip>:8000/esp32-fw.bin\\"}"

The connection between the ESP32 and this server runs through WireGuard,
so plain HTTP is fine — WireGuard provides confidentiality and integrity.
"""

from __future__ import annotations

import argparse
import http.server
import pathlib
import socketserver
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "binary",
        nargs="?",
        default="build/esp32-fw.bin",
        help="path to firmware .bin (default: build/esp32-fw.bin)",
    )
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    bin_path = pathlib.Path(args.binary).resolve()
    if not bin_path.is_file():
        print(f"error: {bin_path} not found — run `idf.py build` first", file=sys.stderr)
        return 1

    serve_dir = bin_path.parent
    name = bin_path.name

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(serve_dir), **kw)

    with socketserver.TCPServer((args.host, args.port), Handler) as srv:
        print(f"serving {bin_path} as http://{args.host}:{args.port}/{name}")
        print("Ctrl-C to stop")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print()
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
