#!/usr/bin/env python3
"""mockingbird collector — TCP server on :9001 that ingests BLE observation
streams from the leaves and persists them to SQLite.

Wire protocol: newline-delimited JSON. Each line is one of:
  {"event":"hello","leaf":"<hostname>","version":"<fw>"}
  {"event":"obs","mac":"AA:BB:..","rssi":-65,"addr_type":1,
   "name":"...","manuf":"....","t_ms":12345}
  {"event":"hb","up_s":42,"n_sent":1234}

The collector tags every record with the Pi's monotonic clock at receive,
so analysis can correlate across leaves without trusting their millis()
counters (which reset on reboot).

Run as a systemd service via services/mockingbird-collector.service —
that handles auto-restart, journal logging, and starting on boot.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

DB_PATH = Path.home() / "mockingbird" / "observations.sqlite"
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 9001
MAX_LINE = 1024  # one observation; lines over this are dropped

log = logging.getLogger("mb-collector")


def open_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, isolation_level=None)
    # WAL → better concurrent read+write, survives crashes cleanly.
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS obs (
            ts        REAL    NOT NULL,
            leaf      TEXT    NOT NULL,
            mac       TEXT    NOT NULL,
            rssi      INTEGER NOT NULL,
            addr_type INTEGER,
            name      TEXT,
            manuf     TEXT,
            leaf_t_ms INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_obs_ts        ON obs(ts);
        CREATE INDEX IF NOT EXISTS idx_obs_mac_ts    ON obs(mac, ts);
        CREATE INDEX IF NOT EXISTS idx_obs_leaf_ts   ON obs(leaf, ts);

        CREATE TABLE IF NOT EXISTS leaf_events (
            ts    REAL NOT NULL,
            leaf  TEXT NOT NULL,
            event TEXT NOT NULL,
            info  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_le_ts ON leaf_events(ts);
    """)
    # Idempotent schema migrations — older deploys won't have `location`.
    for col_spec in ("obs ADD COLUMN location TEXT",
                     "leaf_events ADD COLUMN location TEXT"):
        try:
            db.execute(f"ALTER TABLE {col_spec}")
        except sqlite3.OperationalError:
            pass  # column already exists
    return db


class Counters:
    __slots__ = ("obs", "hello", "hb", "drop", "leaves", "last_log")

    def __init__(self) -> None:
        self.obs = 0
        self.hello = 0
        self.hb = 0
        self.drop = 0
        self.leaves: dict[str, dict] = {}  # hostname → {"n_obs": int, "last": float}
        self.last_log = time.time()

    def tick_log(self, force: bool = False) -> None:
        now = time.time()
        if not force and (now - self.last_log) < 10:
            return
        leaf_summary = ", ".join(
            f"{h}={v['n_obs']}" for h, v in sorted(self.leaves.items())
        ) or "(no leaves yet)"
        log.info(f"obs={self.obs} drop={self.drop} hello={self.hello} hb={self.hb} | {leaf_summary}")
        self.last_log = now


async def handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    db: sqlite3.Connection,
    ctr: Counters,
) -> None:
    peer = writer.get_extra_info("peername")
    leaf: str | None = None
    log.info(f"connection from {peer}")

    try:
        while True:
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=120)
            except asyncio.TimeoutError:
                log.warning(f"{leaf or peer}: idle timeout, closing")
                break
            if not line:
                break
            if len(line) > MAX_LINE:
                ctr.drop += 1
                continue
            try:
                msg = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                ctr.drop += 1
                continue

            now = time.time()
            evt = msg.get("event")
            leaf_location: str | None = msg.get("location") or None

            if evt == "hello":
                leaf = msg.get("leaf") or msg.get("hostname")
                if not leaf:
                    ctr.drop += 1
                    continue
                ctr.hello += 1
                ctr.leaves.setdefault(leaf, {"n_obs": 0, "last": now, "location": leaf_location})
                ctr.leaves[leaf]["location"] = leaf_location
                db.execute(
                    "INSERT INTO leaf_events(ts,leaf,event,info,location) VALUES (?,?,?,?,?)",
                    (now, leaf, "hello", json.dumps(msg), leaf_location),
                )
                log.info(f"hello: {leaf} v{msg.get('version','?')} location='{leaf_location or ''}' from {peer}")

            elif evt == "obs" and leaf:
                ctr.obs += 1
                ctr.leaves[leaf]["n_obs"] += 1
                ctr.leaves[leaf]["last"] = now
                # Prefer per-obs location if set; otherwise fall back to the
                # leaf's last-known label from hello.
                loc = leaf_location or ctr.leaves[leaf].get("location")
                db.execute(
                    "INSERT INTO obs(ts,leaf,mac,rssi,addr_type,name,manuf,leaf_t_ms,location)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        now,
                        leaf,
                        msg.get("mac", ""),
                        int(msg.get("rssi", 0)),
                        msg.get("addr_type"),
                        msg.get("name") or None,
                        msg.get("manuf") or None,
                        msg.get("t_ms"),
                        loc,
                    ),
                )

            elif evt == "hb" and leaf:
                ctr.hb += 1
                ctr.leaves[leaf]["last"] = now
                db.execute(
                    "INSERT INTO leaf_events(ts,leaf,event,info,location) VALUES (?,?,?,?,?)",
                    (now, leaf, "hb", json.dumps(msg), ctr.leaves[leaf].get("location")),
                )

            else:
                ctr.drop += 1

            ctr.tick_log()
    except Exception:
        log.exception(f"{leaf or peer}: handler error")
    finally:
        if leaf:
            db.execute(
                "INSERT INTO leaf_events(ts,leaf,event,info) VALUES (?,?,?,?)",
                (time.time(), leaf, "disconnect", None),
            )
        log.info(f"{leaf or peer}: disconnected")
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def stats_loop(ctr: Counters) -> None:
    while True:
        await asyncio.sleep(30)
        ctr.tick_log(force=True)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stderr,
    )
    db = open_db()
    ctr = Counters()
    log.info(f"DB: {DB_PATH}")
    log.info(f"listening on {LISTEN_HOST}:{LISTEN_PORT}")

    async def cb(r: asyncio.StreamReader, w: asyncio.StreamWriter):
        await handle_client(r, w, db, ctr)

    server = await asyncio.start_server(cb, LISTEN_HOST, LISTEN_PORT)
    asyncio.create_task(stats_loop(ctr))
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
