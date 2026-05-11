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

Writes are BATCHED through `WriteBuffer`: pending INSERTs accumulate in
memory and are flushed every FLUSH_INTERVAL_S in a single transaction.
This cuts SQLite WAL fsync count by ~50× (the per-row autocommit pattern
the original collector used hit the Pi Zero W's disk-syscall budget
hard at >100 obs/s).

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

# ---- batching tunables ----
# Flush every FLUSH_INTERVAL_S regardless of size, OR when MAX_BATCH rows
# pile up. At our current 150 obs/s and 0.2s interval, batches average
# ~30 rows; at 1500 obs/s they'd cap at MAX_BATCH=500 and flush ~5×/s.
# Either way: 1 fsync per flush, not 1 fsync per row.
FLUSH_INTERVAL_S = 0.2
MAX_BATCH = 500

# Force a WAL→main checkpoint with truncation every CHECKPOINT_INTERVAL_S.
# Auto-checkpoint alone is insufficient because the dashboard holds
# persistent read connections that pin the WAL truncation point.
# wal_checkpoint(TRUNCATE) forces all readers to advance and shrinks
# the WAL file back to zero. Cheap because synchronous=NORMAL + cached
# pages.
CHECKPOINT_INTERVAL_S = 30

log = logging.getLogger("mb-collector")


def open_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, isolation_level=None, check_same_thread=False)
    # WAL → better concurrent read+write, survives crashes cleanly.
    # We do our own BEGIN/COMMIT to batch inserts, so isolation_level=None
    # (autocommit-by-default) plus explicit transactions gives us control.
    db.execute("PRAGMA journal_mode=WAL")
    # synchronous=NORMAL: WAL appends are written immediately; only fsync
    # at checkpoint time. Safe for our use case.
    db.execute("PRAGMA synchronous=NORMAL")
    # CRITICAL: keep WAL bounded. Default (1000 pages = ~4MB) was right.
    # Earlier I tried 10000 and the WAL ballooned to 2.5GB because the
    # dashboard's persistent read connections kept the truncation point
    # frozen — auto-checkpoint only copies frames the readers no longer
    # need. To force-truncate, the collector does an explicit
    # wal_checkpoint(TRUNCATE) every CHECKPOINT_INTERVAL_S below.
    db.execute("PRAGMA wal_autocheckpoint=1000")
    db.execute("PRAGMA busy_timeout=3000")
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
    for col_spec in ("obs ADD COLUMN location TEXT",
                     "leaf_events ADD COLUMN location TEXT"):
        try:
            db.execute(f"ALTER TABLE {col_spec}")
        except sqlite3.OperationalError:
            pass
    return db


class WriteBuffer:
    """Accumulate obs/event writes, flush as one transaction per period.

    Designed so 99% of the work is a list.append() on the asyncio loop;
    the actual SQLite IO happens on a worker thread via asyncio.to_thread,
    so a slow fsync never blocks readers.
    """

    OBS_SQL = (
        "INSERT INTO obs(ts,leaf,mac,rssi,addr_type,name,manuf,leaf_t_ms,location)"
        " VALUES (?,?,?,?,?,?,?,?,?)"
    )
    EVT_SQL = (
        "INSERT INTO leaf_events(ts,leaf,event,info,location) VALUES (?,?,?,?,?)"
    )

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self.obs: list[tuple] = []
        self.evt: list[tuple] = []
        self._writer_lock = asyncio.Lock()
        # diagnostics
        self.n_flushes = 0
        self.n_rows_flushed = 0
        self.last_flush_ms = 0.0
        self.last_batch_size = 0

    def add_obs(self, row: tuple) -> None:
        self.obs.append(row)

    def add_evt(self, row: tuple) -> None:
        self.evt.append(row)

    @property
    def pending(self) -> int:
        return len(self.obs) + len(self.evt)

    async def flush(self) -> int:
        """Flush whatever's pending to SQLite in one transaction.

        Returns the number of rows written. Safe to call concurrently —
        a lock serializes the actual write; subsequent calls during a
        flush will see an empty buffer and return 0.
        """
        async with self._writer_lock:
            if not self.obs and not self.evt:
                return 0
            obs_batch = self.obs
            evt_batch = self.evt
            self.obs = []
            self.evt = []
            t0 = time.perf_counter()
            await asyncio.to_thread(self._write, obs_batch, evt_batch)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            n = len(obs_batch) + len(evt_batch)
            self.n_flushes += 1
            self.n_rows_flushed += n
            self.last_flush_ms = round(elapsed_ms, 1)
            self.last_batch_size = n
            return n

    def _write(self, obs_batch: list[tuple], evt_batch: list[tuple]) -> None:
        # One BEGIN…COMMIT around both INSERTs — one fsync at COMMIT.
        # If anything goes wrong, ROLLBACK and re-raise so the systemd
        # restart can take over rather than silently losing data.
        try:
            self.db.execute("BEGIN")
            if obs_batch:
                self.db.executemany(self.OBS_SQL, obs_batch)
            if evt_batch:
                self.db.executemany(self.EVT_SQL, evt_batch)
            self.db.execute("COMMIT")
        except Exception:
            try:
                self.db.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise


class Counters:
    __slots__ = ("obs", "hello", "hb", "drop", "leaves", "last_log")

    def __init__(self) -> None:
        self.obs = 0
        self.hello = 0
        self.hb = 0
        self.drop = 0
        self.leaves: dict[str, dict] = {}
        self.last_log = time.time()

    def tick_log(self, buf: WriteBuffer, force: bool = False) -> None:
        now = time.time()
        if not force and (now - self.last_log) < 10:
            return
        leaf_summary = ", ".join(
            f"{h}={v['n_obs']}" for h, v in sorted(self.leaves.items())
        ) or "(no leaves yet)"
        log.info(
            f"obs={self.obs} drop={self.drop} hello={self.hello} hb={self.hb} | "
            f"flushes={buf.n_flushes} (last {buf.last_batch_size} rows / "
            f"{buf.last_flush_ms}ms) pending={buf.pending} | {leaf_summary}"
        )
        self.last_log = now


async def handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    buf: WriteBuffer,
    ctr: Counters,
) -> None:
    peer = writer.get_extra_info("peername")
    leaf: str | None = None
    log.info(f"connection from {peer}")

    # Pi-side TCP keepalive too — symmetric with the firmware change.
    # When a leaf disappears (power, WiFi drop, crash), we don't want the
    # zombie socket sitting in ESTABLISHED for 120 s waiting on readline().
    # Kernel probes at IDLE silence, retries every INTVL, gives up after CNT.
    sock = writer.get_extra_info("socket")
    if sock is not None:
        try:
            import socket as _socket
            sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_KEEPALIVE, 1)
            sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_KEEPIDLE,  15)
            sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_KEEPINTVL, 5)
            sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_KEEPCNT,   2)
        except (OSError, AttributeError):
            pass  # macOS / older kernels may not have all options

    try:
        while True:
            try:
                # With TCP keepalive enabled (above), a dead peer is killed
                # by the kernel in ~25 s. Drop the application-layer idle
                # timeout to 45 s (still > the leaf's 5 s heartbeat interval
                # with headroom for one missed hb) so a silent leaf at the
                # application layer doesn't tie up a connection forever.
                line = await asyncio.wait_for(reader.readline(), timeout=45)
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
                buf.add_evt((now, leaf, "hello", json.dumps(msg), leaf_location))
                log.info(f"hello: {leaf} v{msg.get('version','?')} location='{leaf_location or ''}' from {peer}")

            elif evt == "obs" and leaf:
                ctr.obs += 1
                ctr.leaves[leaf]["n_obs"] += 1
                ctr.leaves[leaf]["last"] = now
                loc = leaf_location or ctr.leaves[leaf].get("location")
                buf.add_obs((
                    now, leaf,
                    msg.get("mac", ""),
                    int(msg.get("rssi", 0)),
                    msg.get("addr_type"),
                    msg.get("name") or None,
                    msg.get("manuf") or None,
                    msg.get("t_ms"),
                    loc,
                ))

            elif evt == "hb" and leaf:
                ctr.hb += 1
                ctr.leaves[leaf]["last"] = now
                buf.add_evt((now, leaf, "hb", json.dumps(msg), ctr.leaves[leaf].get("location")))

            else:
                ctr.drop += 1

            ctr.tick_log(buf)
    except Exception:
        log.exception(f"{leaf or peer}: handler error")
    finally:
        if leaf:
            buf.add_evt((time.time(), leaf, "disconnect", None, None))
        log.info(f"{leaf or peer}: disconnected")
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def flush_loop(buf: WriteBuffer) -> None:
    """Drain the buffer every FLUSH_INTERVAL_S, plus an immediate drain
    whenever pending crosses MAX_BATCH. The size-driven flush is what
    keeps memory bounded under burst load."""
    while True:
        await asyncio.sleep(FLUSH_INTERVAL_S)
        # Drain — and if there's still a big backlog (heavy load),
        # keep draining without sleeping until we're back to steady-state.
        await buf.flush()
        while buf.pending >= MAX_BATCH:
            await buf.flush()


async def stats_loop(buf: WriteBuffer, ctr: Counters) -> None:
    while True:
        await asyncio.sleep(30)
        ctr.tick_log(buf, force=True)


async def checkpoint_loop(buf: WriteBuffer) -> None:
    """Force-truncate the WAL every CHECKPOINT_INTERVAL_S.

    Uses a SEPARATE SQLite connection inside the writer lock so it can't
    race the writer over the shared connection's internal mutex. (Two
    threads using the same sqlite3.Connection concurrently is supported
    but each operation serializes — so a slow checkpoint could starve
    a flush, or vice versa, and we hit it in practice as a stuck
    flush_loop.)

    The TRUNCATE variant forces all readers past their pin point and
    shrinks the WAL file back to zero — needed because the dashboard
    holds persistent reader connections that block normal auto-checkpoint
    from truncating.
    """
    while True:
        await asyncio.sleep(CHECKPOINT_INTERVAL_S)
        try:
            t0 = time.perf_counter()
            # Use the writer's lock so we don't race with a flush.
            async with buf._writer_lock:
                result = await asyncio.to_thread(_checkpoint_via_new_conn)
            log.info(
                f"checkpoint: busy={result[0]} log={result[1]} "
                f"checkpointed={result[2]} in {(time.perf_counter()-t0)*1000:.0f}ms"
            )
        except Exception:
            log.exception("checkpoint failed")


def _checkpoint_via_new_conn():
    """Open a private connection, checkpoint, close. Avoids any chance of
    cross-thread sharing of the main writer connection."""
    c = sqlite3.connect(DB_PATH, isolation_level=None)
    try:
        c.execute("PRAGMA busy_timeout=3000")
        row = c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return row
    finally:
        c.close()


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stderr,
    )
    db = open_db()
    buf = WriteBuffer(db)
    ctr = Counters()
    log.info(f"DB: {DB_PATH}")
    log.info(f"listening on {LISTEN_HOST}:{LISTEN_PORT} (batched flush every {FLUSH_INTERVAL_S}s, max {MAX_BATCH} rows)")

    async def cb(r: asyncio.StreamReader, w: asyncio.StreamWriter):
        await handle_client(r, w, buf, ctr)

    server = await asyncio.start_server(cb, LISTEN_HOST, LISTEN_PORT)
    asyncio.create_task(flush_loop(buf))
    asyncio.create_task(stats_loop(buf, ctr))
    asyncio.create_task(checkpoint_loop(buf))
    async with server:
        try:
            await server.serve_forever()
        finally:
            # Last-ditch drain on graceful shutdown.
            try:
                await buf.flush()
            except Exception:
                log.exception("final flush failed")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
