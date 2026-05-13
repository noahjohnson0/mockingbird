# Collector + Tracks Stack: Performance & Correctness Review

**Ticket:** ETH-1
**Author:** Ethan
**Date:** 2026-05-13
**Scope:** `services/mockingbird-collector.py`, `services/mockingbird-dashboard.py`,
`services/mockingbird_calibration.py`, `services/mockingbird_tracks.py`,
`firmware/esp32-wroom-mockingbird/src/main.cpp`

This is a static review — no profiler, no flamegraph. Findings are
reasoned from the code paths and known load (~700 obs/s aggregate from
8 leaves into a Pi Zero W single-core ARMv6). Where I quote numbers, I
flag them as "estimated" until measured.

---

## TL;DR — top findings

| # | Severity | What | Where |
|---|---|---|---|
| 1 | **P0** | `/api/live` is dead in pending dashboard rewrite — producer thread never started | dashboard.py (uncommitted) |
| 2 | **P0** | Per-thread DB cache leaks SQLite connections, pins WAL frames | dashboard.py:455-473 |
| 3 | **P1** | Wrong index forced on per-leaf range scans (idx_obs_ts vs idx_obs_leaf_ts) | dashboard.py:342, calibration.py |
| 4 | **P1** | `TrackStore.step()` holds coarse lock across MLE + clustering + fusion | tracks.py:859-972 |
| 5 | **P1** | Entity clustering is O(N²) over all tracks every 2 s, inside the lock | tracks.py:687-693 |
| 6 | **P2** | `_compute_live_snapshot` runs heavy work on the live-snapshot thread without bounding cost | dashboard.py:97-203 |
| 7 | **P2** | Firmware: `String deviceHostname()` heap-allocates on every call | main.cpp:58-64 |
| 8 | **P2** | Collector serializes hb/hello msgs back to JSON for the event log (re-allocation) | collector.py:275, 297 |
| 9 | **P2** | `_compute_live_snapshot` recomputes leaf-mac filter set on every tick | dashboard.py:110-114 |
| 10 | **P3** | KF/MLE pure-Python triple-nested loops dominate per-track latency on Pi Zero | tracks.py:276-293, calibration.py:702-709 |

**Shipped in this commit:** review doc only. Finding #1 is in
uncommitted working-tree changes I shouldn't author into a merge of
my own — flagged for the author of those changes to fix before they
land. Everything else is recommendation.

---

## 1. Hot paths

The collector is asyncio + a single batched SQLite writer thread. The
dashboard is `ThreadingHTTPServer` (new thread per request) plus two
background workers.

### Collector (`mockingbird-collector.py`)
- `handle_client.readline()` loop, dominated by `json.loads` of every
  observation line (~700/s aggregate). `json.loads` allocates a fresh
  dict per call — ~10× the size of the `(tuple)` we shove into the
  buffer. Pure CPython, no C accelerator on armhf? Worth confirming.
- `WriteBuffer._write` is called every 200 ms or every 500 rows.
  `executemany` + one COMMIT is the right shape — one fsync per flush.
  Confirmed in code: WAL + synchronous=NORMAL + busy_timeout=3000. Good.
- `checkpoint_loop` correctly opens a *separate* connection for
  `wal_checkpoint(TRUNCATE)` to avoid thread-sharing the main connection
  (line 365-374). This is the right pattern; the comment captures why.

### Dashboard (`mockingbird-dashboard.py`)
- `/api/system` is the most expensive sync endpoint: full table scan of
  `leaf_events` over the last 24 h. At ~10 hb/leaf/min × 8 leaves × 1440
  min = 115k rows, then a sort. Caching would help if this is hot.
- `/api/leaves` is hot (dashboard polls it). Already cached
  (`active_leaves`, `leaf_rssi_stats`). Verify `estimate_leaf_position`
  isn't called for *every* known leaf each request — it's gated to
  active+unpositioned which keeps it bounded, but the chunked IN
  (BATCH=500 MACs) query is the cost when it runs.
- `_compute_live_snapshot` is the heaviest single path (multilat over
  every device, then track step, then entity fusion). Off-thread by
  design — only the cached result is served by `/api/live`. Provided
  finding #1 is fixed.

### Tracks (`mockingbird_tracks.py`)
- `step()` is O(D × T) for D devices × T existing tracks in the slow
  path (`_best_match`). With ~80 devices × ~50 tracks that's 4000 RMS
  compares per tick. Each compare iterates over shared leaves (~8).
  Pure-Python — estimated ~5–20 ms per `step()` on Pi Zero.
- `_cluster_entities` is O(T²) — 50² = 2500 pair compares every 2 s.
  Plus union-find. Estimated ~3–10 ms.
- `kalman_step` / `mle_multilaterate` are 3×3 / 6×6 matrix code in pure
  Python. Each pretty cheap (~1–2 ms), but called per-device-per-tick.

### Firmware (`main.cpp`)
- BLE `ScanCB::onResult` runs in BLE host task on core 0. Hot path: at
  the room's observed BLE density (several hundred adv/sec under heavy
  scan), this fires hundreds of times per second. Allocations: zero
  (uses stack `Msg m{}` + xQueueSend by copy). Good.
- `uplink_task` snprintf-builds JSON line, single `g_tcp.write()`. Stack
  buffer (512 B). Good.

---

## 2. Allocation pressure

### Collector
- `json.loads(line.decode(...))` allocates one dict per obs line. At
  ~700 obs/s on a Pi Zero with armhf CPython that's the dominant
  allocator churn. **Mitigation:** stream parser (`ijson`/`orjson`)
  would help but adds a dep — likely not worth it unless we measure
  GC stalls.
- `buf.add_evt((now, leaf, "hb", json.dumps(msg), ...))` (line 297)
  re-serializes the parsed msg back to JSON for storage. We already
  had the raw line; storing the raw line (or just the relevant
  subfields as columns) avoids the second allocation. **P2.**

### Dashboard
- `_compute_live_snapshot` allocates a fresh `positions` dict, a fresh
  `leaf_macs` set, and a `rows` list each call. Cheap per-tick — once
  every 2 s. Not a hot allocation source.
- `_db_cache` indexed by `threading.get_ident()` is itself the
  allocation leak: connection objects are never freed because dead
  thread IDs stay in the dict (see Finding #2 below).

### Tracks
- `track.fingerprint` (dict per track) and `track.trail` (deque,
  bounded) — bounded. Fine.
- `_cluster_entities` rebuilds `parent`, `clusters`, `new_entities`,
  `new_t2e` from scratch every call. Cheap allocations in absolute
  terms; per 2 s. Fine.

### Firmware
- `String deviceHostname()` heap-allocates a `String` every call (HTTP
  handlers + uplink). Tiny but unnecessary; cache once at boot. **P2.**
- `d->getName()` returns a `std::string` ref — no allocation in
  ScanCB. Good.
- `d->getManufacturerData()` returns a `std::string` by value(!) —
  one allocation per advert. Confirm in NimBLE; if so, this is the
  biggest per-advert allocation. Estimated <100 B but at 700+/s
  it pressures the heap. **Worth measuring.**

---

## 3. SQLite write path

- **WAL mode:** confirmed on (collector.py:65). Good.
- **synchronous=NORMAL:** confirmed. Right tradeoff for our durability
  needs (we tolerate losing the last in-flight batch on crash).
- **Batching:** the WriteBuffer + 200 ms flush + 500-row cap is the
  right shape (`executemany` in one transaction = 1 fsync). Already
  paid down the per-row autocommit penalty noted in the file header.
- **Indices:** `idx_obs_ts`, `idx_obs_mac_ts`, `idx_obs_leaf_ts`. The
  three biggest query shapes are covered.
- **Write amplification:** every INSERT touches obs heap + 3 indices.
  At 700 rows/s that's 2800 b-tree inserts/s. The Pi Zero's SD card
  can handle that with WAL coalescing, but we're not free here —
  consider whether `idx_obs_leaf_ts` is actually used; if not, drop it.
  (Grep: yes, used by `/api/autodetect_phone`. Keep.)
- **Checkpoint pin:** the dashboard's persistent per-thread reader
  connections (Finding #2) are exactly what was causing the WAL to
  balloon — the file header in collector.py calls this out, the fix
  was `wal_checkpoint(TRUNCATE)` every 30 s. But that's a band-aid:
  fixing #2 lets normal auto-checkpoint truncate too.

### Recommended write-path changes
- **None P0/P1.** Write path is well-tuned.
- **P2:** Consider an `obs2` table with `STRICT` + typed columns + no
  `addr_type` if unused, but that's a schema-migration project, not a
  perf win on the hot path.

---

## 4. Memory footprint on Pi Zero W (512 MB)

Rough headroom budget:
- Pi OS Lite base: ~80 MB
- Tailscale: ~40 MB RSS
- collector (asyncio + sqlite3): ~25 MB steady
- dashboard (ThreadingHTTPServer + per-thread DB conns): **unbounded
  growth, see Finding #2**. Each sqlite3.Connection is ~1–2 MB resident
  (page cache + WAL frame map). 10 connections is OK; 1000 is not.
- Page cache for the obs.sqlite: depends on size; CLAUDE.md notes WAL
  was ballooning to 2.5 GB before TRUNCATE — meaning the page cache
  was being asked to materialize huge cold reads. With TRUNCATE every
  30 s plus a single shared reader (post-#2-fix), should stay <50 MB.

**Headroom estimate today:** maybe 200–300 MB free under normal load.
The growth risks are:
1. `_db_cache` (Finding #2). Days of uptime = potentially 100s of MB.
2. `TrackStore.tracks` GC by `FORGET_AFTER_S=300`. Bounded but only
   actually pruned when `_gc(now)` runs in `step()`. If `step()` ever
   stops being called (e.g. Finding #1 blocking the live loop), tracks
   accumulate forever. Worth a watchdog.

---

## 5. Concurrency / thread safety

### Collector — asyncio single-loop
- `WriteBuffer.flush()` uses `asyncio.Lock` to serialize. SQLite calls
  hop to thread pool via `asyncio.to_thread`. Correct.
- `checkpoint_loop` opens a *new* connection inside the writer lock —
  good, avoids cross-thread sharing of the writer's connection.
- One subtle race: `buf.add_obs` is called from the asyncio reader
  task without any lock. That's safe because Python `list.append` is
  atomic under the GIL AND the asyncio loop only runs one task at a
  time. But if anyone moves the collector to multi-threading later,
  this assumption breaks silently.

### Dashboard — `ThreadingHTTPServer` + background threads
- **P0:** `_db_cache` keyed by `threading.get_ident()`. New thread per
  HTTP request → new connection cached → thread dies → ID gets recycled
  by the next thread → it reuses or replaces a connection it didn't
  open. `check_same_thread=False` means the call doesn't crash, but
  the dead-thread connection still exists in the dict, **pinning a WAL
  read snapshot** until the cache is cleared or process restarted.
  This is the root cause of the WAL-bloat that motivated the
  `wal_checkpoint(TRUNCATE)` band-aid in the collector.
- `TrackStore._lock` is a single `threading.Lock` held across
  `step()` (full multilat + clustering + fusion). On a Pi Zero
  single-core, `/api/live` background recompute can hold this for
  hundreds of ms while `/api/autodetect_phone` waits. **P1.**
- `CALIBRATION` is a module-level `global`. Writes happen in two
  threads (calibration loop + `/api/calibrate` POST handler). No lock
  protects the read-update; assignments are atomic in CPython but a
  read of `CALIBRATION` from a worker that's mid-replacement is
  *technically* safe due to GIL — confirm if we ever drop the GIL.
- `LIVE_SNAPSHOT` similarly assigned from one thread, read from
  many. Same logic: atomic in CPython, fine.

---

## 6. Firmware side (ESP32 BLE → TCP)

The firmware is in good shape — the v0.3.1 push-streaming architecture
killed the OOM bug, the priority back-pressure protects MOCK adverts,
TCP keepalive + send-timeout kill wedge cases. Specific wins:

- **`String deviceHostname()` (line 58-64):** called from `setup()`,
  every `handleStatus`, every `uplink_connect`. Each call does a
  WiFi.macAddress() call + snprintf + String construction (heap).
  Static cache at boot, fill once. **P2, ~5 lines.**
- **`d->getManufacturerData()`:** returns `std::string` by value if
  NimBLE-Arduino didn't get the const-ref optimization. Worth checking
  the NimBLE version's signature. If by-value, that's our biggest
  per-advert heap allocation. **P2, measure first.**
- **`Msg m{}` zero-init (line 118):** 84-byte memset per advert. At
  ~100 adverts/s that's 8.4 KB/s of memset bandwidth — negligible on a
  240 MHz dual-core. Keep it; the zero-init guarantees NUL-terminated
  `name`/`manuf` buffers without explicit terminators.
- **`g_tcp.write` return semantics:** the short-write fallback is
  correct and matches the LwIP behavior. No change needed.
- **Heartbeat interval (5 s) + reconnect on short-write:** good.
- **One thing I'd verify next:** `xQueueSend(g_q, &m, 0)` with
  `xTicksToWait=0` is the correct non-blocking call from the BLE
  callback context (we don't want to block the BLE host). Confirmed.

---

## Findings — detailed

### Finding #1: `/api/live` background loop never started — P0 (uncommitted)

**File:** `services/mockingbird-dashboard.py` (working-tree changes,
not yet committed on `main`).
**Symptom:** Once the in-flight "precomputed live snapshot" refactor
lands, `/api/live` will return `{"as_of": ..., "devices": [], "entities": [], "note": "warming up"}` forever. The Three.js dashboard's live view will be empty.
**Root cause:** The refactor adds `LIVE_SNAPSHOT` (a module-level
cache), `_compute_live_snapshot()` (the producer body), and
`live_snapshot_loop()` (the threading wrapper) — but `main()` calls
only `leaf_calibration_loop()` and never starts `live_snapshot_loop()`.
The HTTP handler reads `LIVE_SNAPSHOT` (stays `None`), so it falls
through to the "warming up" stub.

**Fix:** Add `live_snapshot_loop()` immediately after
`leaf_calibration_loop()` in `main()`. 1 line + comment.
NOT shipped in this commit — the feature itself isn't on `main` yet,
and I shouldn't be sneaking a behavior change into another author's
in-flight work. Flagging it here so it gets fixed before the parent
refactor lands.

**Impact:** unbreaks the dashboard's live view entirely (once the
parent refactor merges).

---

### Finding #2: `_db_cache` per-thread leak pinning WAL frames — P0

**File:** `services/mockingbird-dashboard.py:228, 455-473`
**Symptom:** SQLite WAL file grows to gigabytes; `wal_checkpoint`
without `TRUNCATE` doesn't shrink it (which is why the collector ended
up doing `TRUNCATE` every 30 s as a workaround).
**Root cause:** `ThreadingHTTPServer` creates a new OS thread for every
incoming request. Each thread's first DB use stuffs a
`sqlite3.Connection` into `_db_cache[tid]`. Threads exit, but the dict
entry stays. Connections are never closed. Each holds a WAL read
snapshot, so auto-checkpoint can't advance past the oldest pinned
frame. Over hours, hundreds of connections accumulate, each consuming
~1–2 MB resident.

**Fix (recommended, not shipped):** Replace `ThreadingHTTPServer` with
a thread-pool server (`socketserver.ThreadingMixIn` with a bounded
pool, or migrate to `asyncio` + `aiosqlite`). Alternatively, just one
shared read-only connection guarded by a lock (SQLite WAL allows
multiple concurrent readers via separate connections, but our query
load is light enough that a single locked read connection would still
serve `/api/leaves` and `/api/system` in <50 ms). Estimated 20–50 LOC,
needs a regression pass — not in this commit.

**Impact:** eliminates the 30-s TRUNCATE workaround (or makes it
cheap), recovers ~100 MB+ of Pi RAM after a long uptime, and removes
the underlying reason WAL bloated to 2.5 GB historically.

---

### Finding #3: Wrong-index hint on per-leaf range scans — P1

**File:** `services/mockingbird-dashboard.py:342, services/mockingbird_calibration.py:213, 352, 464`
**Symptom:** Slower than necessary; estimated 3–10× scan cost on
queries that filter by both `leaf = ?` AND `ts >= ?`.
**Root cause:** Queries that have `WHERE ts >= ? AND leaf = ?` force
`INDEXED BY idx_obs_ts`, which range-scans the *entire* obs table for
the time window, then filters by leaf in row-fetch. For a 30 s window
at 700 obs/s that's 21k rows scanned to keep ~2.6k.
`INDEXED BY idx_obs_leaf_ts` would seek directly to the per-leaf
range.

Specifically:
- `dashboard.py:342` `WHERE ts >= ? AND leaf = ?` — force `idx_obs_leaf_ts`.
- `calibration.py:213` `WHERE ts >= ? AND leaf IN (...)` — `idx_obs_leaf_ts` per-leaf is better than `idx_obs_ts` + post-filter when there are <8 leaves.
- `calibration.py:352` and `464` `WHERE ts ... AND mac IN (...)` — `idx_obs_mac_ts` per-mac would be better than `idx_obs_ts` scan when the MAC list is small (<20).

**Fix:** Audit each `INDEXED BY` clause; change to the leaf-/mac-prefix
index when the predicate selects strongly on that column. Then
benchmark with `EXPLAIN QUERY PLAN` on a representative DB snapshot.

**Impact:** estimated 2–5× faster auto-detect-phone, calibration fits,
and per-leaf position estimates. Helps the Pi Zero hold up under
700 obs/s steady-state.

---

### Finding #4: `TrackStore.step()` holds coarse lock across heavy work — P1

**File:** `services/mockingbird_tracks.py:859-972`
**Symptom:** All other dashboard handlers that touch `store.tracks` or
`store.entities` block while `step()` runs. On Pi Zero, full `step()`
with ~80 devices + ~50 tracks + MLE per track + clustering + fusion
takes an estimated 50–150 ms each tick.
**Root cause:** Single `self._lock` held from line 859 through 972.

**Fix:** Two-phase processing: phase 1 (read tracks + plan updates,
read-locked), phase 2 (apply updates atomically, write-locked) keeps
the write-lock duration to <5 ms. Or move multilat off the lock
entirely — call MLE without the lock, then re-acquire to commit.

**Impact:** removes 50–150 ms tail latency from concurrent
`/api/autodetect_phone` and `/api/calibrate` calls.

---

### Finding #5: Entity clustering is O(N²) inside the lock — P1

**File:** `services/mockingbird_tracks.py:687-693`
**Root cause:** Nested loop over all active tracks, compares
fingerprints pairwise every `step()` (= every 2 s).

**Fix:** Locality-sensitive hashing on the in-room leaf signature
would prune the comparison set to candidates with overlapping
fingerprints. Or: bucket tracks by their top-RSSI leaf, only compare
within a bucket. Either drops to O(N · K) for K ~10 candidates.

**Impact:** modest now (50 tracks) but scales badly. With more leaves
and more residents this hits the wall — fix before it bites.

---

### Finding #6: `_compute_live_snapshot` runs unbounded work — P2

**File:** `services/mockingbird-dashboard.py:97-203`
**Concern:** No timeout on multilat, no cap on number of devices
processed. A flood of devices (e.g. user walks through a busy café
elsewhere on the floor) could blow the 2 s refresh budget. Right now
the loop is OK because we have ~80 devices typical, but the cost is
linear and unbounded.

**Fix:** Limit to top-K devices by RSSI per tick (already sorted at
the end; just cap before).

---

### Finding #7: `String deviceHostname()` heap-allocates on every call — P2

**File:** `firmware/esp32-wroom-mockingbird/src/main.cpp:58-64`
**Fix:** Cache as a `static char[24]` filled once in `setup()`.
**Impact:** removes a small heap allocation from every HTTP request
and from `uplink_connect`. Heap headroom is at 110–125 KB; this
won't move the needle, but it's free.

---

### Finding #8: Collector re-serializes hb/hello back to JSON for events — P2

**File:** `services/mockingbird-collector.py:275, 297`
**Concern:** `buf.add_evt((now, leaf, "hb", json.dumps(msg), ...))`
calls `json.dumps` on the dict we just parsed. The original line is
already a valid JSON string — pass the raw line instead and skip the
re-allocation.
**Fix:** Keep the raw line bytes around through the parser path; pass
to `add_evt` directly.
**Impact:** small per-event win (~5–10 hb/s aggregate). Mostly tidy.

---

### Finding #9: `_compute_live_snapshot` rebuilds leaf_macs set every tick — P2

**File:** `services/mockingbird-dashboard.py:110-114`
**Concern:** `SELECT mac FROM calibration_points WHERE label LIKE 'leaf-advert%'` runs every 2 s. The set changes only when the calibration loop refits (every 5 min).
**Fix:** Cache with TTL matching `LEAF_CAL_REFRESH_S`, or have the
calibration loop publish the set to a module global when it refits.
**Impact:** removes one SQLite query per `/api/live` tick.

---

### Finding #10: Pure-Python matrix code dominates per-track latency — P3

**File:** `services/mockingbird_calibration.py:702-709` (MLE), `services/mockingbird_tracks.py:276-293` (KF gain).
**Concern:** Triple-nested Python loops for matrix multiply on a Pi
Zero are slow — estimated 1–3 ms per track per tick.
**Fix options:**
1. Numpy on Pi Zero — armhf wheel is ~30 MB. Probably worth it.
2. Hand-unroll the 3×3 multiplies (no inner loop).
3. ctypes wrapper around a tiny C extension — too much rope.
**Impact:** estimated 2–4× speedup on the live path. Defer until #1
and #2 are shipped and we have a measurement.

---

## Recommended sequencing

1. **Now:** ship Finding #1 (this commit).
2. **Next sprint:** Finding #2 (DB connection lifecycle). This is the
   biggest hidden-cost item — likely the cause of most "Pi feels
   slow over time" reports.
3. **Then:** Findings #3 and #4 together. Same file touched, same
   regression window.
4. **Then:** profile. Don't keep optimizing in the dark — we need a
   real flamegraph from the Pi under load before going further.

## Open questions / things I'd measure

- Actual `step()` latency on Pi Zero under representative load. My 50–150 ms is
  reasoned from line counts × ARMv6 Python perf, not measured.
- ESP32 heap allocator pressure under sustained MOCK + ambient flood.
  `g_n_dropped` rate is the proxy; we should be logging it.
- WAL file size over a week of uptime with and without Finding #2 fixed.
- `json.loads` vs `orjson` throughput on armhf — if `orjson` ships an
  armhf wheel, easy win in the collector.
