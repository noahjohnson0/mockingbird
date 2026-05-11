#!/usr/bin/env python3
"""mockingbird-dashboard — tiny 3D room visualizer hosted on the Pi.

Listens on :8080. Renders the leaves as colored spheres in a Three.js
scene relative to a chosen origin corner, with a side panel for editing
positions. Reads/writes the leaf_position table the collector created.

Run:  python3 mockingbird-dashboard.py
Or install as a systemd service via mockingbird-dashboard.service.
"""

from __future__ import annotations

import http.server
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

DB_PATH = Path.home() / "mockingbird" / "observations.sqlite"
HTML_PATH = Path(__file__).parent / "dashboard.html"
PORT = 8080


def open_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, isolation_level=None)
    db.execute("""
        CREATE TABLE IF NOT EXISTS leaf_position (
            leaf      TEXT PRIMARY KEY,
            x         REAL NOT NULL,
            y         REAL NOT NULL,
            z         REAL NOT NULL,
            units     TEXT DEFAULT 'm',
            notes     TEXT,
            updated_ts REAL DEFAULT (strftime('%s','now'))
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS room (
            id      INTEGER PRIMARY KEY CHECK (id = 1),
            width   REAL NOT NULL,
            depth   REAL NOT NULL,
            height  REAL NOT NULL,
            notes   TEXT,
            updated_ts REAL DEFAULT (strftime('%s','now'))
        )
    """)
    return db


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "mockingbird-dashboard/0.1"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(f"{time.strftime('%H:%M:%S')} {self.address_string()} {fmt % args}\n")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        self._send(status, json.dumps(obj).encode(), "application/json")

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length) if length else b""

    def do_GET(self) -> None:
        url = urlparse(self.path)

        if url.path == "/":
            try:
                html = HTML_PATH.read_bytes()
            except FileNotFoundError:
                return self._send(500, b"dashboard.html missing", "text/plain")
            return self._send(200, html, "text/html; charset=utf-8")

        if url.path == "/api/leaves":
            db = open_db()
            positioned = {row[0]: dict(zip(["leaf","x","y","z","units","notes"], row))
                          for row in db.execute(
                              "SELECT leaf, x, y, z, units, notes FROM leaf_position"
                          )}
            # Pull recent leaves + their current location. The collector
            # stamps every obs with the leaf's location, so a single pass
            # over the last 60 s of obs gives us both leaf-online state
            # and the active label. Subqueries-per-leaf were O(750k)
            # rows each on the Pi Zero W — this is O(rows-in-60s).
            seen = list(db.execute("""
                SELECT leaf,
                       MAX(location) AS location,
                       MAX(ts)       AS last_seen
                FROM obs
                WHERE ts >= strftime('%s','now') - 60
                GROUP BY leaf
            """))
            out = []
            for leaf, location, last_seen in seen:
                row = positioned.pop(leaf, {"leaf": leaf, "x": None, "y": None, "z": None, "units": "m", "notes": None})
                row["location"] = location
                row["last_seen"] = last_seen
                out.append(row)
            # Plus any positioned leaves not currently streaming
            for leaf, row in positioned.items():
                row["location"] = None
                row["last_seen"] = None
                out.append(row)
            return self._json(out)

        if url.path == "/api/room":
            db = open_db()
            row = db.execute(
                "SELECT width, depth, height, notes FROM room WHERE id = 1"
            ).fetchone()
            if row:
                return self._json({"width": row[0], "depth": row[1], "height": row[2], "notes": row[3]})
            return self._json({"width": None, "depth": None, "height": None, "notes": None})

        return self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        url = urlparse(self.path)
        if url.path == "/api/room":
            try:
                msg = json.loads(self._read_body() or "{}")
                w = float(msg["width"]); d = float(msg["depth"]); h = float(msg["height"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                return self._json({"error": "width/depth/height must be numbers"}, 400)
            notes = (msg.get("notes") or "")[:200] or None
            db = open_db()
            db.execute("""
                INSERT INTO room(id, width, depth, height, notes, updated_ts)
                VALUES (1, ?, ?, ?, ?, strftime('%s','now'))
                ON CONFLICT(id) DO UPDATE SET
                    width=excluded.width, depth=excluded.depth, height=excluded.height,
                    notes=excluded.notes, updated_ts=excluded.updated_ts
            """, (w, d, h, notes))
            return self._json({"status": "ok", "width": w, "depth": d, "height": h})
        if url.path == "/api/leaves":
            try:
                msg = json.loads(self._read_body() or "{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad json"}, 400)
            leaf = msg.get("leaf")
            if not leaf or not re.match(r"^[\w\-.]+$", leaf):
                return self._json({"error": "leaf name missing or invalid"}, 400)
            try:
                x = float(msg["x"]); y = float(msg["y"]); z = float(msg["z"])
            except (KeyError, TypeError, ValueError):
                return self._json({"error": "x/y/z must be numbers"}, 400)
            notes = (msg.get("notes") or "")[:200] or None
            db = open_db()
            db.execute("""
                INSERT INTO leaf_position(leaf, x, y, z, notes, updated_ts)
                VALUES (?, ?, ?, ?, ?, strftime('%s','now'))
                ON CONFLICT(leaf) DO UPDATE SET
                  x=excluded.x, y=excluded.y, z=excluded.z,
                  notes=excluded.notes, updated_ts=excluded.updated_ts
            """, (leaf, x, y, z, notes))
            return self._json({"status": "ok", "leaf": leaf, "x": x, "y": y, "z": z})

        return self._send(404, b"not found", "text/plain")

    def do_DELETE(self) -> None:
        url = urlparse(self.path)
        m = re.match(r"^/api/leaves/([\w\-.]+)$", url.path)
        if m:
            db = open_db()
            db.execute("DELETE FROM leaf_position WHERE leaf=?", (m.group(1),))
            return self._json({"status": "ok"})
        return self._send(404, b"not found", "text/plain")


def main() -> int:
    addr = ("0.0.0.0", PORT)
    server = http.server.ThreadingHTTPServer(addr, Handler)
    sys.stderr.write(f"{time.strftime('%H:%M:%S')} mockingbird-dashboard listening on {addr[0]}:{addr[1]}\n")
    sys.stderr.write(f"  DB:   {DB_PATH}\n  HTML: {HTML_PATH}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
