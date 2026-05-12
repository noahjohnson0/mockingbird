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

# Sibling modules on the Pi — same directory as this file.
sys.path.insert(0, str(Path(__file__).parent))
import mockingbird_tracks       # noqa: E402
import mockingbird_calibration  # noqa: E402

# Tier-1 calibration: leaves advertise BLE in firmware v0.5.0+, and other
# leaves observe each other. We auto-refresh calibration_point rows for
# these inter-leaf observations every LEAF_CAL_REFRESH_S so the fit always
# has fresh ground-truth data without manual intervention.
LEAF_CAL_REFRESH_S = 60
LEAF_CAL_WINDOW_S = 60   # rolling window of inter-leaf data per refresh

# Latest fitted path-loss params (None until first /api/calibrate call).
# When set, /api/live uses multilateration in place of weighted-centroid.
CALIBRATION: mockingbird_calibration.CalibrationParams | None = None

DB_PATH = Path.home() / "mockingbird" / "observations.sqlite"
HTML_PATH = Path(__file__).parent / "dashboard.html"
PORT = 8080


_db_cache: dict[int, sqlite3.Connection] = {}
_db_initialized = False

# Known leaves + the location label they were set to via firmware /location.
# The collector also captures locations in leaf_events, but querying that
# table every request was costing 8-10s on a Pi Zero W under write load.
# These are static labels (set once via firmware NVS); just hardcode them.
# When you add a new leaf or relocate one, update this dict.
KNOWN_LEAVES = {
    "mockingbird-4ce184": "Noah room test rack",
    "mockingbird-4c0bdc": "noahs bedroom plant rack",
    "mockingbird-4c36ec": "desk",
    "mockingbird-4db204": "dresser",
    # Newly added 2026-05-11 — placement pending.
    "mockingbird-4ceb7c": None,
    "mockingbird-4d4384": None,
}

# Cache of "leaves seen streaming recently" so /api/leaves stays cheap.
# A leaf is "online" if it produced any obs row in the last ACTIVE_WINDOW_S.
# Cached for ACTIVE_CACHE_TTL because the query is a single distinct-scan
# over ~few hundred rows and we don't want to redo it on every page poll.
ACTIVE_WINDOW_S = 60
ACTIVE_CACHE_TTL = 8
_active_cache: dict = {"ts": 0.0, "leaves": set()}

def active_leaves(db: sqlite3.Connection) -> set[str]:
    global _active_cache
    now = time.time()
    if now - _active_cache["ts"] < ACTIVE_CACHE_TTL:
        return _active_cache["leaves"]
    rows = db.execute(
        "SELECT DISTINCT leaf FROM obs INDEXED BY idx_obs_ts WHERE ts >= ?",
        (now - ACTIVE_WINDOW_S,),
    ).fetchall()
    leaves = {row[0] for row in rows}
    _active_cache = {"ts": now, "leaves": leaves}
    return leaves


# Per-leaf RSSI statistics (count of distinct devices, top RSSI, median).
# Single range scan over a short window; cached for cheap reuse.
STATS_WINDOW_S = 20
STATS_CACHE_TTL = 4.0
_stats_cache: dict = {"ts": 0.0, "data": {}}

def leaf_rssi_stats(db: sqlite3.Connection, active: set[str]) -> dict[str, dict]:
    """For each active leaf, return {n_devices, top_rssi, median_rssi}.

    'n_devices' is the count of distinct MACs seen in the last
    STATS_WINDOW_S seconds; top/median are computed on each MAC's MAX(rssi)
    on that leaf in the window. Same range-scan trick as /api/live.
    """
    global _stats_cache
    now = time.time()
    if now - _stats_cache["ts"] < STATS_CACHE_TTL:
        return _stats_cache["data"]
    rows = db.execute(
        "SELECT leaf, mac, rssi FROM obs INDEXED BY idx_obs_ts "
        "WHERE ts >= ?",
        (now - STATS_WINDOW_S,),
    ).fetchall()
    # Aggregate (leaf, mac) → max rssi in Python (cheap for ≲2k rows)
    best: dict[tuple[str, str], int] = {}
    for leaf, mac, rssi in rows:
        cur = best.get((leaf, mac))
        if cur is None or rssi > cur:
            best[(leaf, mac)] = rssi
    # Pivot to per-leaf lists
    per_leaf: dict[str, list[int]] = {}
    for (leaf, _mac), r in best.items():
        per_leaf.setdefault(leaf, []).append(r)
    out: dict[str, dict] = {}
    for leaf, rssis in per_leaf.items():
        if leaf not in active:
            continue
        rssis.sort(reverse=True)
        n = len(rssis)
        out[leaf] = {
            "n_devices": n,
            "top_rssi": rssis[0],
            "median_rssi": rssis[n // 2],
        }
    _stats_cache = {"ts": now, "data": out}
    return out


# Per-leaf position estimate cache (estimate is steady-state; recompute
# every ESTIMATE_TTL only). Recomputed live so as the user keeps moving
# the leaf around, the estimate follows within ~10s.
ESTIMATE_TTL = 10.0
ESTIMATE_WINDOW_S = 30
ESTIMATE_MIN_DEVICES = 4  # below this, the centroid is too noisy to publish
_estimate_cache: dict[str, dict] = {}


def estimate_leaf_position(db: sqlite3.Connection, leaf_id: str,
                           positions: dict[str, tuple[float, float, float]]) -> dict | None:
    """Estimate (x,y,z) for an unpositioned leaf from its observed RSSI
    to devices that are also seen by ≥2 positioned leaves.

    For each such device:
      - compute its centroid via weighted RSSI across positioned leaves
      - weight that centroid by 10**(rssi_target/20) where rssi_target is
        how loud the device is to the unknown leaf
    Sum / normalize → leaf position estimate.
    """
    cached = _estimate_cache.get(leaf_id)
    now = time.time()
    if cached and now - cached["ts"] < ESTIMATE_TTL:
        return cached["estimate"]

    # 1. Devices this leaf sees recently
    target_rows = db.execute(
        "SELECT mac, MAX(rssi) FROM obs INDEXED BY idx_obs_ts "
        "WHERE ts >= ? AND leaf = ? GROUP BY mac",
        (now - ESTIMATE_WINDOW_S, leaf_id),
    ).fetchall()
    if not target_rows:
        _estimate_cache[leaf_id] = {"ts": now, "estimate": None}
        return None
    target_rssi = {mac: rssi for mac, rssi in target_rows}

    # 2. RSSI of those same MACs to each positioned leaf (in one query)
    if not positions or not target_rssi:
        _estimate_cache[leaf_id] = {"ts": now, "estimate": None}
        return None
    macs = list(target_rssi.keys())
    # SQLite max parameter count is 999; we chunk in case there are many.
    BATCH = 500
    pos_rows: list[tuple[str, str, int]] = []
    pos_leaves = list(positions.keys())
    for i in range(0, len(macs), BATCH):
        chunk = macs[i:i + BATCH]
        ph_macs = ",".join("?" * len(chunk))
        ph_leaves = ",".join("?" * len(pos_leaves))
        pos_rows.extend(db.execute(
            f"SELECT mac, leaf, MAX(rssi) FROM obs INDEXED BY idx_obs_ts "
            f"WHERE ts >= ? AND mac IN ({ph_macs}) AND leaf IN ({ph_leaves}) "
            f"GROUP BY mac, leaf",
            (now - ESTIMATE_WINDOW_S, *chunk, *pos_leaves),
        ).fetchall())

    by_mac: dict[str, list[tuple[str, int]]] = {}
    for mac, leaf, rssi in pos_rows:
        by_mac.setdefault(mac, []).append((leaf, rssi))

    # 3. Weighted-centroid over devices that have ≥2 positioned-leaf hits
    num_x = num_y = num_z = denom = 0.0
    n_devices = 0
    for mac, hits in by_mac.items():
        if len(hits) < 2:
            continue
        # Device centroid from positioned leaves (same math as /api/live)
        dnx = dny = dnz = dd = 0.0
        for leaf, rssi in hits:
            w = 10.0 ** (rssi / 20.0)
            px, py, pz = positions[leaf]
            dnx += w * px; dny += w * py; dnz += w * pz; dd += w
        dev_x, dev_y, dev_z = dnx / dd, dny / dd, dnz / dd
        # Weight this device by how loud it is to the unknown leaf
        wt = 10.0 ** (target_rssi[mac] / 20.0)
        num_x += wt * dev_x; num_y += wt * dev_y; num_z += wt * dev_z
        denom += wt
        n_devices += 1

    if n_devices < ESTIMATE_MIN_DEVICES or denom == 0:
        _estimate_cache[leaf_id] = {"ts": now, "estimate": None}
        return None
    est = {
        "x": round(num_x / denom, 2),
        "y": round(num_y / denom, 2),
        "z": round(num_z / denom, 2),
        "n_devices": n_devices,
    }
    _estimate_cache[leaf_id] = {"ts": now, "estimate": est}
    return est


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
    try:
        # When set, the multilat z-bound floor moves to 0 (no positions
        # below the floor are physically possible). User toggles in Setup.
        db.execute("ALTER TABLE room ADD COLUMN is_ground_floor INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    # Calibration anchor points — user puts a known device at a known
    # position and asks us to "remember the RSSI signature here." During
    # fitting we use these as ground truth instead of bootstrap centroids.
    db.execute("""
        CREATE TABLE IF NOT EXISTS calibration_points (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_start   REAL NOT NULL,
            ts_end     REAL NOT NULL,
            mac        TEXT NOT NULL,
            track_id   TEXT,
            track_name TEXT,
            x REAL NOT NULL, y REAL NOT NULL, z REAL NOT NULL,
            label TEXT,
            created_ts REAL DEFAULT (strftime('%s','now'))
        )
    """)


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

        if url.path == "/api/system":
            db = open_db()
            now = time.time()
            # ----- Pi stats -----
            pi: dict = {}
            try:
                with open("/proc/loadavg") as f:
                    pi["load_avg"] = [float(p) for p in f.read().split()[:3]]
            except Exception:
                pi["load_avg"] = None
            try:
                with open("/proc/uptime") as f:
                    pi["uptime_s"] = int(float(f.read().split()[0]))
            except Exception:
                pi["uptime_s"] = None
            try:
                with open("/proc/meminfo") as f:
                    mem = {}
                    for line in f:
                        if ":" not in line:
                            continue
                        k, v = line.split(":", 1)
                        mem[k.strip()] = int(v.strip().split()[0])  # in kB
                    pi["mem_total_kb"] = mem.get("MemTotal")
                    pi["mem_available_kb"] = mem.get("MemAvailable")
                    pi["mem_used_pct"] = (
                        round(100 * (1 - mem["MemAvailable"] / mem["MemTotal"]), 1)
                        if mem.get("MemTotal") else None
                    )
            except Exception:
                pass
            try:
                import shutil
                total, _used, free = shutil.disk_usage("/")
                pi["disk_total_gb"] = round(total / 1024**3, 1)
                pi["disk_free_gb"] = round(free / 1024**3, 1)
                pi["disk_used_pct"] = round(100 * (1 - free / total), 1)
            except Exception:
                pass
            # DB size (the obs file)
            try:
                pi["db_size_mb"] = round(DB_PATH.stat().st_size / 1024**2, 1)
            except Exception:
                pass
            # ----- per-leaf stats from latest heartbeat -----
            # Wide lookback (24h) so silent/dead leaves still appear in
            # the Stats tab — a leaf that stopped heartbeating is the one
            # you most need to see, not hide.
            rows = db.execute(
                "SELECT leaf, ts, info FROM leaf_events "
                "WHERE event='hb' AND ts >= ? "
                "ORDER BY ts DESC",
                (now - 86400,),
            ).fetchall()
            seen: dict[str, dict] = {}
            for leaf, ts, info_s in rows:
                if leaf in seen:
                    continue
                try:
                    info = json.loads(info_s)
                except (json.JSONDecodeError, TypeError):
                    info = {}
                seen[leaf] = {
                    "leaf": leaf,
                    "hb_age_s": round(now - ts, 1),
                    "uptime_s": info.get("up_s"),
                    "n_sent": info.get("n_sent"),
                    "n_dropped": info.get("n_dropped"),
                    "q_depth": info.get("q_depth"),
                    "heap_free": info.get("heap"),
                    "wifi_rssi": info.get("rssi"),
                }
            # Always also include known leaves and positioned leaves even
            # if they have NO heartbeat in the lookback at all — those
            # show up as fully-offline with no telemetry.
            try:
                positioned_leaves = {row[0] for row in db.execute("SELECT leaf FROM leaf_position")}
            except sqlite3.OperationalError:
                positioned_leaves = set()
            for leaf in KNOWN_LEAVES.keys() | positioned_leaves:
                if leaf not in seen:
                    seen[leaf] = {
                        "leaf": leaf, "hb_age_s": None,
                        "uptime_s": None, "n_sent": None, "n_dropped": None,
                        "q_depth": None, "heap_free": None, "wifi_rssi": None,
                    }
            # Obs rate over last 30 s
            obs_rows = db.execute(
                "SELECT leaf, COUNT(*) FROM obs INDEXED BY idx_obs_ts "
                "WHERE ts >= ? GROUP BY leaf",
                (now - 30,),
            ).fetchall()
            obs_rate = {leaf: round(n / 30.0, 1) for leaf, n in obs_rows}
            for leaf in seen:
                seen[leaf]["obs_per_s"] = obs_rate.get(leaf, 0.0)
                # Status classification
                age = seen[leaf]["hb_age_s"]
                if age is None or age > 600:
                    seen[leaf]["status"] = "offline"
                elif age > 60:
                    seen[leaf]["status"] = "stale"
                else:
                    seen[leaf]["status"] = "ok"
            # Sort: offline first (most urgent), then stale, then ok by name
            order = {"offline": 0, "stale": 1, "ok": 2}
            leaves_list = sorted(seen.values(),
                                 key=lambda x: (order.get(x["status"], 9), x["leaf"]))
            return self._json({"as_of": now, "pi": pi, "leaves": leaves_list})

        if url.path == "/api/calibration_points":
            db = open_db()
            now = time.time()
            rows = db.execute(
                "SELECT id, ts_start, ts_end, mac, track_id, track_name, "
                "x, y, z, label FROM calibration_points ORDER BY ts_start DESC"
            ).fetchall()
            points = []
            for r in rows:
                in_progress = now < r[2]
                points.append({
                    "id": r[0],
                    "ts_start": r[1], "ts_end": r[2],
                    "in_progress": in_progress,
                    "elapsed_s": round(now - r[1], 1),
                    "duration_s": round(r[2] - r[1], 1),
                    "mac": r[3], "track_id": r[4], "track_name": r[5],
                    "x": r[6], "y": r[7], "z": r[8],
                    "label": r[9],
                })
            return self._json({"points": points})

        if url.path == "/api/calibration":
            cal = CALIBRATION
            if cal is None:
                return self._json({"calibrated": False})
            return self._json({
                "calibrated": True,
                "p0": cal.p0, "n": cal.n, "rmse_dbm": cal.rmse_dbm,
                "n_points": cal.n_points, "n_devices": cal.n_devices,
                "fit_ts": cal.fit_ts, "fit_age_s": round(time.time() - cal.fit_ts, 1),
            })

        if url.path == "/api/leaves":
            db = open_db()
            positioned = {row[0]: dict(zip(["leaf","x","y","z","units","notes"], row))
                          for row in db.execute(
                              "SELECT leaf, x, y, z, units, notes FROM leaf_position"
                          )}
            # Union: hardcoded known + currently-streaming + positioned.
            # Currently-streaming surfaces newly-plugged-in leaves with
            # zero config — they appear in the sidebar automatically.
            active = active_leaves(db)
            all_leaves = set(KNOWN_LEAVES) | set(positioned) | active
            # Positions tuple for use in estimation
            pos_tuples = {l: (positioned[l]["x"], positioned[l]["y"], positioned[l]["z"])
                          for l in positioned}
            # Per-leaf RSSI stats (active leaves only, cached cheaply)
            stats = leaf_rssi_stats(db, active)
            out = []
            for leaf in sorted(all_leaves):
                row = positioned.get(leaf, {"leaf": leaf, "x": None, "y": None, "z": None, "units": "m", "notes": None})
                row.setdefault("leaf", leaf)
                row["location"] = KNOWN_LEAVES.get(leaf)
                row["online"] = leaf in active
                row["stats"] = stats.get(leaf)  # {n_devices, top_rssi, median_rssi} or None
                # Estimated position for unpositioned + active leaves.
                if leaf in active and row["x"] is None and len(pos_tuples) >= 2:
                    row["estimate"] = estimate_leaf_position(db, leaf, pos_tuples)
                else:
                    row["estimate"] = None
                out.append(row)
            return self._json(out)

        if url.path == "/api/live":
            # Live device-cluster view. Returns Apple-fingerprint devices
            # (manuf '4c00*' — iPhones, AirPods, Watches, MacBooks) seen
            # by ≥2 positioned leaves in the last LIVE_WINDOW_S seconds,
            # with a weighted-centroid position. Weights are linear-amplitude
            # RSSI (10**(rssi/20)) — stronger signal = bigger pull.
            #
            # MAC rotation note: Apple Continuity rotates random addrs ~every
            # 15 min, so the *same physical device* may appear as multiple
            # MACs over time. v1 just renders every recently-seen MAC; a
            # dot that vanishes and a new one nearby usually means rotation,
            # not movement. Future: cluster by RSSI-fingerprint similarity.
            LIVE_WINDOW_S = 8
            db = open_db()
            positions = {row[0]: (row[1], row[2], row[3]) for row in db.execute(
                "SELECT leaf, x, y, z FROM leaf_position"
            )}
            if not positions:
                return self._json({"as_of": time.time(), "devices": [], "note": "no positioned leaves yet"})
            now = time.time()
            # IMPORTANT: don't let SQLite GROUP BY on (mac, leaf) — it picks
            # the (mac, ts) covering index and full-scans the obs table
            # (~20 s on a Pi Zero W). Force the ts index for the range scan,
            # then aggregate in Python (≪1 ms for a few hundred rows).
            rows = db.execute(
                "SELECT mac, leaf, rssi, name, manuf "
                "FROM obs INDEXED BY idx_obs_ts "
                "WHERE ts >= ? AND manuf LIKE '4c00%'",
                (now - LIVE_WINDOW_S,),
            ).fetchall()
            # Aggregate (mac, leaf) → max rssi + first-seen name/manuf
            best: dict[tuple[str, str], tuple[int, str | None, str | None]] = {}
            for mac, leaf, rssi, name, manuf in rows:
                if leaf not in positions:
                    continue
                key = (mac, leaf)
                cur = best.get(key)
                if cur is None or rssi > cur[0]:
                    best[key] = (rssi, name or (cur[1] if cur else None), manuf or (cur[2] if cur else None))
            by_mac: dict[str, list[tuple[str, int, str | None, str | None]]] = {}
            for (mac, leaf), (rssi, name, manuf) in best.items():
                by_mac.setdefault(mac, []).append((leaf, rssi, name, manuf))
            devices = []
            cal = CALIBRATION
            method = "centroid"
            # Bounds for multilat sanity check.  These do NOT clip "outside
            # the room" — BLE has real range past walls, and a device 3m
            # outside a wall is legitimate signal.  These bounds only catch
            # math explosions (linearization failures producing 1km+
            # coordinates).  Set to "room + 15m on every side" — generous
            # enough that any plausible BLE pickup is allowed through, but
            # tight enough to reject the 10^3 m blowups.
            room_row = db.execute(
                "SELECT width, depth, height, is_ground_floor FROM room WHERE id = 1"
            ).fetchone()
            if room_row:
                rw, rd, rh, ground = room_row
                EXTRA = 15.0
                # Ground floor: nothing can be below z=0 (the floor). For
                # non-ground-floor, allow EXTRA below to permit detecting
                # devices on the floor downstairs.
                z_min = -0.5 if ground else -EXTRA
                multilat_bounds = (-EXTRA, rw + EXTRA, -EXTRA, rd + EXTRA, z_min, rh + EXTRA)
            else:
                multilat_bounds = (-20.0, 20.0, -20.0, 20.0, -10.0, 10.0)
            for mac, hits in by_mac.items():
                if len(hits) < 2:
                    continue  # need ≥2 positioned-leaf hits for any position confidence
                # Common metadata
                rssi_max = -999
                leaves_seen = []
                name = manuf = None
                rssi_per_leaf: dict[str, int] = {}
                for leaf, rssi, lname, lmanuf in hits:
                    rssi_per_leaf[leaf] = rssi
                    if rssi > rssi_max:
                        rssi_max = rssi
                    leaves_seen.append({"leaf": leaf, "rssi": rssi})
                    name = name or lname
                    manuf = manuf or lmanuf
                # Position: prefer multilateration when calibrated AND we
                # have ≥4 hits; else fall back to amplitude-weighted centroid.
                px = py = pz = None
                pos_method = "centroid"
                if cal is not None and len(rssi_per_leaf) >= 4:
                    res = mockingbird_calibration.multilaterate(
                        rssi_per_leaf, positions, cal, bounds=multilat_bounds,
                    )
                    if res is not None:
                        px, py, pz, _rmse = res
                        pos_method = "multilat"
                        method = "multilat"
                if px is None:
                    num_x = num_y = num_z = denom = 0.0
                    for leaf, rssi in rssi_per_leaf.items():
                        w = 10.0 ** (rssi / 20.0)
                        ax, ay, az = positions[leaf]
                        num_x += w * ax; num_y += w * ay; num_z += w * az; denom += w
                    px, py, pz = num_x / denom, num_y / denom, num_z / denom
                devices.append({
                    "mac": mac, "name": name, "manuf": manuf,
                    "x": px, "y": py, "z": pz,
                    "rssi_max": rssi_max, "n_leaves": len(hits),
                    "leaves": leaves_seen,
                    "pos_method": pos_method,
                })
            devices.sort(key=lambda d: d["rssi_max"], reverse=True)
            # Push through the track tracker. When calibrated, the
            # tracker re-positions each device via multilateration using
            # the *track's* accumulated fingerprint (which covers more
            # leaves than any single short-lived MAC's per-poll window).
            tracked = mockingbird_tracks.store.step(
                devices, now=now,
                calibration=cal, positions=positions,
                multilat_bounds=multilat_bounds,
            )
            return self._json({
                "as_of": now,
                "window_s": LIVE_WINDOW_S,
                "method": method,  # "multilat" or "centroid"
                "calibration": (None if cal is None else {
                    "p0": cal.p0, "n": cal.n, "rmse_dbm": cal.rmse_dbm,
                    "n_points": cal.n_points, "n_devices": cal.n_devices,
                    "fit_age_s": round(now - cal.fit_ts, 1),
                }),
                "devices": tracked,
            })

        if url.path == "/api/room":
            db = open_db()
            row = db.execute(
                "SELECT width, depth, height, notes, north_deg, is_ground_floor FROM room WHERE id = 1"
            ).fetchone()
            if row:
                return self._json({
                    "width": row[0], "depth": row[1], "height": row[2],
                    "notes": row[3], "north_deg": row[4] or 0,
                    "is_ground_floor": bool(row[5]),
                })
            return self._json({"width": None, "depth": None, "height": None, "notes": None,
                               "north_deg": 0, "is_ground_floor": False})

        return self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        url = urlparse(self.path)
        if url.path == "/api/autodetect_phone":
            try:
                msg = json.loads(self._read_body() or "{}")
                visits = msg.get("visits") or []
                assert isinstance(visits, list) and len(visits) >= 2
            except (json.JSONDecodeError, AssertionError):
                return self._json({"error": "need at least 2 visits"}, 400)
            db = open_db()
            # Per-MAC score: each visit, the top-5 MACs by RSSI at the
            # target leaf each earn a point. After all visits, MACs that
            # earned points at multiple distinct leaves are the most
            # likely "your phone" — they followed you between leaves.
            scores: dict[str, dict] = {}
            for idx, v in enumerate(visits):
                try:
                    leaf = v["leaf"]
                    ts_start = float(v["ts_start"])
                    ts_end = float(v["ts_end"])
                except (KeyError, TypeError, ValueError):
                    continue
                if ts_end <= ts_start:
                    continue
                rows = db.execute(
                    "SELECT mac, MAX(rssi) FROM obs INDEXED BY idx_obs_leaf_ts "
                    "WHERE leaf = ? AND ts >= ? AND ts <= ? "
                    "GROUP BY mac ORDER BY MAX(rssi) DESC LIMIT 8",
                    (leaf, ts_start, ts_end),
                ).fetchall()
                for rank, (mac, rssi) in enumerate(rows):
                    s = scores.setdefault(mac, {
                        "mac": mac, "n_visits": 0, "best_rssi": -200,
                        "per_visit": [], "leaves_seen": set(),
                    })
                    s["n_visits"] += 1
                    s["best_rssi"] = max(s["best_rssi"], rssi)
                    s["per_visit"].append({"visit_idx": idx, "leaf": leaf, "rssi": rssi, "rank": rank + 1})
                    s["leaves_seen"].add(leaf)
            # Annotate with track_name if we know one
            try:
                m2t = mockingbird_tracks.store.mac_to_track
                tracks = mockingbird_tracks.store.tracks
                for mac, s in scores.items():
                    tid = m2t.get(mac)
                    if tid and tid in tracks:
                        s["track_name"] = tracks[tid].name
                        s["track_id"] = tid
            except Exception:
                pass
            # Convert leaves_seen → distinct count, drop the set (JSON-incompatible)
            ranked = []
            for mac, s in scores.items():
                s["n_distinct_leaves"] = len(s["leaves_seen"])
                del s["leaves_seen"]
                ranked.append(s)
            # Best phones: appeared near the most distinct leaves, ranked
            # secondarily by best RSSI (closer = more likely the device).
            ranked.sort(key=lambda x: (-x["n_distinct_leaves"], -x["best_rssi"]))
            return self._json({"candidates": ranked[:10], "n_visits": len(visits)})

        if url.path == "/api/calibrate_at":
            try:
                msg = json.loads(self._read_body() or "{}")
                mac = msg["mac"]
                x = float(msg["x"]); y = float(msg["y"]); z = float(msg["z"])
                duration = float(msg.get("duration_s") or 20.0)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                return self._json({"error": "need mac + x + y + z"}, 400)
            if duration < 5 or duration > 120:
                return self._json({"error": "duration must be 5–120 s"}, 400)
            if not re.match(r"^[0-9a-fA-F:]{12,17}$", mac):
                return self._json({"error": "bad mac"}, 400)
            track_id = msg.get("track_id")
            track_name = msg.get("track_name")
            label = (msg.get("label") or "")[:80] or None
            db = open_db()
            now = time.time()
            cur = db.execute(
                "INSERT INTO calibration_points(ts_start, ts_end, mac, track_id, "
                "track_name, x, y, z, label) VALUES (?,?,?,?,?,?,?,?,?)",
                (now, now + duration, mac, track_id, track_name, x, y, z, label),
            )
            return self._json({
                "id": cur.lastrowid, "ts_start": now, "ts_end": now + duration,
                "duration_s": duration, "in_progress": True,
            })

        if url.path == "/api/calibrate":
            global CALIBRATION
            db = open_db()
            positions = {row[0]: (row[1], row[2], row[3]) for row in db.execute(
                "SELECT leaf, x, y, z FROM leaf_position"
            )}
            if len(positions) < 4:
                return self._json({"error": "need ≥4 positioned leaves for calibration"}, 400)
            cal = mockingbird_calibration.fit_pathloss(db, positions)
            if cal is None:
                return self._json({"error": "not enough data — wait a few seconds and retry"}, 400)
            CALIBRATION = cal
            return self._json({
                "calibrated": True,
                "p0": cal.p0, "n": cal.n, "rmse_dbm": cal.rmse_dbm,
                "n_points": cal.n_points, "n_devices": cal.n_devices,
                "fit_ts": cal.fit_ts,
            })

        if url.path == "/api/room":
            try:
                msg = json.loads(self._read_body() or "{}")
                w = float(msg["width"]); d = float(msg["depth"]); h = float(msg["height"])
                north = float(msg.get("north_deg") or 0) % 360
                ground = 1 if msg.get("is_ground_floor") else 0
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                return self._json({"error": "width/depth/height must be numbers"}, 400)
            notes = (msg.get("notes") or "")[:200] or None
            db = open_db()
            db.execute("""
                INSERT INTO room(id, width, depth, height, notes, north_deg, is_ground_floor, updated_ts)
                VALUES (1, ?, ?, ?, ?, ?, ?, strftime('%s','now'))
                ON CONFLICT(id) DO UPDATE SET
                    width=excluded.width, depth=excluded.depth, height=excluded.height,
                    notes=excluded.notes, north_deg=excluded.north_deg,
                    is_ground_floor=excluded.is_ground_floor,
                    updated_ts=excluded.updated_ts
            """, (w, d, h, notes, north, ground))
            return self._json({"status": "ok", "width": w, "depth": d, "height": h,
                               "north_deg": north, "is_ground_floor": bool(ground)})
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
        m = re.match(r"^/api/calibration_points/(\d+)$", url.path)
        if m:
            db = open_db()
            db.execute("DELETE FROM calibration_points WHERE id=?", (int(m.group(1)),))
            return self._json({"status": "ok"})
        return self._send(404, b"not found", "text/plain")


def leaf_calibration_loop() -> None:
    """Background task: every LEAF_CAL_REFRESH_S, sweep the obs table for
    leaf-to-leaf BLE advertisements (firmware v0.5.0+: each leaf advertises
    its hostname). For each transmitting leaf, identify the BD_ADDR it's
    using and refresh a calibration_point at the transmitter's known
    position. fit_pathloss() will then incorporate the inter-leaf RSSI
    matrix as high-quality ground-truth pairs."""
    import threading
    def run() -> None:
        while True:
            try:
                db = open_db()
                now = time.time()
                positions = {row[0]: (row[1], row[2], row[3]) for row in db.execute(
                    "SELECT leaf, x, y, z FROM leaf_position"
                )}
                # For each leaf, find the BLE BD_ADDR it's advertising under
                # (i.e. the MAC most frequently associated with name=hostname)
                for leaf, pos in positions.items():
                    row = db.execute(
                        "SELECT mac FROM obs INDEXED BY idx_obs_ts "
                        "WHERE ts >= ? AND name = ? "
                        "GROUP BY mac ORDER BY COUNT(*) DESC LIMIT 1",
                        (now - LEAF_CAL_WINDOW_S, leaf),
                    ).fetchone()
                    if not row:
                        continue
                    mac = row[0]
                    # Refresh: delete any prior leaf-advert anchor for this
                    # leaf, then insert a fresh one with the latest window.
                    db.execute(
                        "DELETE FROM calibration_points WHERE label = ?",
                        (f"leaf-advert · {leaf}",),
                    )
                    db.execute(
                        "INSERT INTO calibration_points("
                        "ts_start, ts_end, mac, track_name, x, y, z, label"
                        ") VALUES (?,?,?,?,?,?,?,?)",
                        (now - LEAF_CAL_WINDOW_S, now, mac, "leaf-self-advert",
                         pos[0], pos[1], pos[2], f"leaf-advert · {leaf}"),
                    )
            except Exception:
                import traceback; traceback.print_exc(file=sys.stderr)
            time.sleep(LEAF_CAL_REFRESH_S)
    t = threading.Thread(target=run, daemon=True, name="leaf-calibration")
    t.start()
    sys.stderr.write(f"{time.strftime('%H:%M:%S')} leaf-calibration loop started "
                     f"(refresh every {LEAF_CAL_REFRESH_S}s)\n")


def main() -> int:
    addr = ("0.0.0.0", PORT)
    server = http.server.ThreadingHTTPServer(addr, Handler)
    sys.stderr.write(f"{time.strftime('%H:%M:%S')} mockingbird-dashboard listening on {addr[0]}:{addr[1]}\n")
    leaf_calibration_loop()
    sys.stderr.write(f"  DB:   {DB_PATH}\n  HTML: {HTML_PATH}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
