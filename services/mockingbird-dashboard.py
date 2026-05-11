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


_db_cache: dict[int, sqlite3.Connection] = {}
_db_initialized = False


def _init_schema(db: sqlite3.Connection) -> None:
    """Run schema setup exactly once per process. CREATE TABLE IF NOT EXISTS
    is a no-op when the table exists but still walks sqlite_master, which
    is enough overhead per-request to matter on a Pi Zero W when the DB is
    busy. So do it once at startup, not on every connection."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS leaf_position (
            leaf      TEXT PRIMARY KEY,
            x         REAL NOT NULL, y REAL NOT NULL, z REAL NOT NULL,
            units     TEXT DEFAULT 'm', notes TEXT,
            updated_ts REAL DEFAULT (strftime('%s','now'))
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS room (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            width REAL NOT NULL, depth REAL NOT NULL, height REAL NOT NULL,
            notes TEXT, updated_ts REAL DEFAULT (strftime('%s','now'))
        )
    """)
    try:
        db.execute("ALTER TABLE room ADD COLUMN north_deg REAL DEFAULT 0")
    except sqlite3.OperationalError:
        pass


def open_db() -> sqlite3.Connection:
    """Per-thread cached SQLite connection. ThreadingHTTPServer reuses
    threads via thread pool, so this is bounded. Settings tuned for
    reading alongside the collector's writes."""
    global _db_initialized
    import threading
    tid = threading.get_ident()
    db = _db_cache.get(tid)
    if db is None:
        db = sqlite3.connect(DB_PATH, isolation_level=None, check_same_thread=False)
        # Read-friendly + WAL-aware
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA busy_timeout=2000")
        _db_cache[tid] = db
        if not _db_initialized:
            _init_schema(db)
            _db_initialized = True
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
            # Pull EVERYTHING from leaf_events (~27k rows, indexed) — skip
            # the obs table entirely. The collector writes a heartbeat
            # event for each leaf every 5s, which gives both "online"
            # (last hb time) and the current location (from latest hello).
            # obs has 1.4M+ rows being constantly written; querying it
            # fights the collector for locks AND the optimizer picks the
            # wrong index. leaf_events is 50× smaller and write-quiet.
            last_event = {}      # leaf → ts of most recent event (any kind)
            last_hello = {}      # leaf → parsed info dict from most recent hello
            for leaf, event, ts, info in db.execute("""
                SELECT leaf, event, ts, info FROM leaf_events
                WHERE ts >= strftime('%s','now') - 86400
                ORDER BY ts
            """):
                if ts > last_event.get(leaf, 0): last_event[leaf] = ts
                if event == "hello" and info:
                    try:    last_hello[leaf] = json.loads(info)
                    except: pass

            all_leaves = set(positioned) | set(last_event)
            out = []
            for leaf in sorted(all_leaves):
                row = positioned.get(leaf, {"leaf": leaf, "x": None, "y": None, "z": None, "units": "m", "notes": None})
                row.setdefault("leaf", leaf)
                row["location"]  = (last_hello.get(leaf) or {}).get("location") or None
                row["last_seen"] = last_event.get(leaf)
                row["online"]    = bool(last_event.get(leaf) and (time.time() - last_event[leaf]) < 30)
                out.append(row)
            return self._json(out)

        if url.path == "/api/room":
            db = open_db()
            row = db.execute(
                "SELECT width, depth, height, notes, north_deg FROM room WHERE id = 1"
            ).fetchone()
            if row:
                return self._json({
                    "width": row[0], "depth": row[1], "height": row[2],
                    "notes": row[3], "north_deg": row[4] or 0,
                })
            return self._json({"width": None, "depth": None, "height": None, "notes": None, "north_deg": 0})

        return self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        url = urlparse(self.path)
        if url.path == "/api/room":
            try:
                msg = json.loads(self._read_body() or "{}")
                w = float(msg["width"]); d = float(msg["depth"]); h = float(msg["height"])
                north = float(msg.get("north_deg") or 0) % 360
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                return self._json({"error": "width/depth/height must be numbers"}, 400)
            notes = (msg.get("notes") or "")[:200] or None
            db = open_db()
            db.execute("""
                INSERT INTO room(id, width, depth, height, notes, north_deg, updated_ts)
                VALUES (1, ?, ?, ?, ?, ?, strftime('%s','now'))
                ON CONFLICT(id) DO UPDATE SET
                    width=excluded.width, depth=excluded.depth, height=excluded.height,
                    notes=excluded.notes, north_deg=excluded.north_deg, updated_ts=excluded.updated_ts
            """, (w, d, h, notes, north))
            return self._json({"status": "ok", "width": w, "depth": d, "height": h, "north_deg": north})
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
