# EDA scripts

Shovel-ready scripts for inspecting `~/mockingbird/observations.sqlite`
(the BLE collector DB). Each script answers one question. Run them in
priority order, or run `run_all.py` to get the whole report at once.

The companion document is `docs/data/eda-plan.md` — that has the
queries, expected shapes, and what counts as a finding. The scripts
here are the runnable form of that plan.

## Priority order

Each script has a docstring at the top explaining the question, why
it matters, and how to interpret the output. The order is the order
from the plan; later analyses depend on the interpretation of earlier
ones.

| Script | Question | Why it's at this priority |
|---|---|---|
| `p1_manufacturer_breakdown.py` | What's the device population (manuf, names, public-vs-random)? | Everything downstream depends on what "device" means in this data. |
| `p2_coverage_histogram.py` | How many leaves see a typical device? | Sets the floor on what trilateration can do, regardless of algorithm. |
| `p3_per_leaf_rssi_bias.py` | After the TX/RX decomposition, is any leaf still biased? | The empirical check on calibration. With CIs, on universal devices. |
| `p4_per_leaf_rates.py` | Are leaves contributing balanced volumes? | Imbalance silently anchors fusion to a noisy leaf. |
| `p5_device_churn.py` | Static vs. transient device mix; size of the calibration cohort. | Persistent devices ARE the calibration set. |
| `p6_heartbeat_freshness.py` | How often, and how long, do leaves go silent? | Operational reliability of the mesh. |

## Running

All scripts share the same CLI:

```
--db PATH       observations.sqlite (default ~/mockingbird/observations.sqlite)
--since EXPR    window start. '24h', '30m', '7d' (relative to MAX(ts) in DB)
                OR ISO-8601 absolute. Default '24h'.
--until EXPR    window end. Same parsing. Default = MAX(ts) in DB.
--out FILE      optional .csv or .json for the primary table
--format        force 'csv' or 'json' (otherwise inferred from --out)
```

Examples:

```bash
# Quick population snapshot, last 30 min
python scripts/eda/p1_manufacturer_breakdown.py --since 30m

# Coverage census, last hour, save the histogram
python scripts/eda/p2_coverage_histogram.py --since 1h --out /tmp/cov.csv

# Bias check, last 4h, on a snapshot of the live DB pulled to the Mac
python scripts/eda/p3_per_leaf_rssi_bias.py \
    --db ~/repos/.scratch/snapshots/obs-2026-05-13.sqlite --since 4h

# Whole report
python scripts/eda/run_all.py --since 1h

# Just priorities 1-3
python scripts/eda/run_all.py --since 1h --only p1,p2,p3
```

The DB is opened **read-only** (SQLite URI `mode=ro`), so it's safe to
point at the live collector DB on the Pi via the tailnet:

```bash
# Pull a snapshot first (live DB has write activity; snapshot is cleaner)
ssh pi@mockingbird-pi 'sqlite3 ~/mockingbird/observations.sqlite \
    ".backup /tmp/obs.snap.sqlite"'
scp pi@mockingbird-pi:/tmp/obs.snap.sqlite /tmp/
python scripts/eda/run_all.py --db /tmp/obs.snap.sqlite --since 1h
```

## What to do with the output

Each script prints:

1. A header with the resolved window.
2. One or more tables (manufacturer, leaves, devices, etc.).
3. A "Flags" section that calls out the *actionable* findings — the
   stuff that should change behavior. Read this first if you're skimming.

The `--out` flag writes the primary table to CSV/JSON for downstream
work (notebook plotting, further joins, etc.). The text output is the
intended "did you read this morning's run?" artifact.

A finding worth writing up belongs in `docs/data/eda-findings.md`
(create alongside `eda-plan.md`) — keep the numbers, drop the noise.

## Design notes

- **No pandas / scipy hard dependency.** These scripts run on a Pi
  Zero W or any Python 3.10+ environment. The bootstrap CI in
  `p3_per_leaf_rssi_bias.py` is a 30-line implementation, not a SciPy
  call.
- **Relative time is anchored to MAX(ts) in the DB**, not wall-clock.
  Reason: the collector clock is what stamped the data; if the Pi was
  off for a day, `--since 24h` still gives you the last 24h of *data*.
  Pass absolute `--until` if you want strict wall-clock.
- **Read-only DB access.** Every connection uses `file:...?mode=ro`.
  You cannot accidentally write to a live collector DB.
- **No emoji, no decoration.** Aligned text tables, period.

— Wanjiru
