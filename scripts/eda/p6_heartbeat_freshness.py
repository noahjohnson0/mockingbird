#!/usr/bin/env python3
"""
EDA — Heartbeat freshness distribution.

QUESTION
--------
How often do leaves go quiet, and for how long? Are gaps correlated
across leaves (an AP-side hiccup) or per-leaf (firmware / RF)?

WHY IT MATTERS
--------------
This is the operational reliability of the mesh. If a leaf goes silent
mid-window, every analysis over that window has a phantom coverage hole.

HOW TO INTERPRET
----------------
- `gap_mean_s` near the firmware's configured heartbeat interval = good.
  Way above = a leaf that's flaky or that we lost from the AP.
- A burst of `disconnect` events across multiple leaves at the same
  minute = the Opal/AP rebooted or did a channel scan. Don't blame the
  firmware.
- `n_gap_gt_5m` counts the events where a leaf was silent for >5 min.
  Any nonzero value here for a leaf that's supposed to be online 24/7
  is worth a look.

USAGE
-----
    python scripts/eda/p6_heartbeat_freshness.py --since 24h
"""

from __future__ import annotations

import argparse

from _common import (
    add_common_args,
    print_header,
    print_table,
    standard_setup,
    write_output,
)


SQL_HAS_TABLE = """
SELECT name FROM sqlite_master
WHERE type='table' AND name='leaf_events'
"""

SQL_GAPS = """
WITH hb AS (
  SELECT leaf, ts,
         LAG(ts) OVER (PARTITION BY leaf ORDER BY ts) AS prev_ts
  FROM leaf_events
  WHERE event = 'hb' AND ts BETWEEN :t0 AND :t1
)
SELECT
    leaf,
    COUNT(*)                                              AS n_gaps,
    ROUND(AVG(ts - prev_ts), 2)                           AS gap_mean_s,
    ROUND(MIN(ts - prev_ts), 2)                           AS gap_min_s,
    ROUND(MAX(ts - prev_ts), 2)                           AS gap_max_s,
    SUM(CASE WHEN ts - prev_ts > 60   THEN 1 ELSE 0 END)  AS n_gap_gt_60s,
    SUM(CASE WHEN ts - prev_ts > 300  THEN 1 ELSE 0 END)  AS n_gap_gt_5m,
    SUM(CASE WHEN ts - prev_ts > 3600 THEN 1 ELSE 0 END)  AS n_gap_gt_1h
FROM hb
WHERE prev_ts IS NOT NULL
GROUP BY leaf
ORDER BY n_gap_gt_5m DESC, gap_mean_s DESC
"""

SQL_DISCONNECTS = """
SELECT leaf, COUNT(*) AS n_disconnects
FROM leaf_events
WHERE event = 'disconnect' AND ts BETWEEN :t0 AND :t1
GROUP BY leaf
ORDER BY n_disconnects DESC
"""

# Correlated outage detection: bucket disconnects + long-gap starts by minute
SQL_CORRELATED = """
SELECT
    CAST(ts / 60 AS INTEGER) AS minute_bucket,
    COUNT(DISTINCT leaf)     AS n_leaves_affected,
    COUNT(*)                 AS n_events
FROM leaf_events
WHERE event = 'disconnect' AND ts BETWEEN :t0 AND :t1
GROUP BY minute_bucket
HAVING n_leaves_affected >= 2
ORDER BY n_leaves_affected DESC, n_events DESC
LIMIT 20
"""


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_args(p)
    args, con, window = standard_setup(p)

    print_header("Heartbeat freshness", window)

    if not con.execute(SQL_HAS_TABLE).fetchone():
        print("(no leaf_events table in this DB — skipping)")
        return

    gaps = [dict(r) for r in con.execute(SQL_GAPS, {"t0": window.t0, "t1": window.t1})]
    if not gaps:
        print("(no heartbeat pairs in window)")
    else:
        print("-- Inter-heartbeat gap stats per leaf --")
        print_table(
            gaps,
            columns=["leaf", "n_gaps", "gap_mean_s", "gap_min_s", "gap_max_s", "n_gap_gt_60s", "n_gap_gt_5m", "n_gap_gt_1h"],
            max_rows=None,
        )

    disc = [dict(r) for r in con.execute(SQL_DISCONNECTS, {"t0": window.t0, "t1": window.t1})]
    print("\n-- Disconnects per leaf --")
    if disc:
        print_table(disc, columns=["leaf", "n_disconnects"], max_rows=None)
    else:
        print("  (none)")

    corr = [dict(r) for r in con.execute(SQL_CORRELATED, {"t0": window.t0, "t1": window.t1})]
    print("\n-- Correlated disconnect windows (minute buckets with >=2 leaves) --")
    if corr:
        for r in corr:
            r["minute_iso"] = r["minute_bucket"] * 60
        print_table(corr, columns=["minute_bucket", "n_leaves_affected", "n_events"], max_rows=None)
        print("  Multiple leaves dropping in the same minute = AP-side hiccup, not firmware.")
    else:
        print("  (no correlated outages)")

    print("\n-- Flags --")
    flagged = [g for g in gaps if (g["n_gap_gt_5m"] or 0) > 0]
    if flagged:
        for r in flagged:
            print(f"  [!] {r['leaf']}: {r['n_gap_gt_5m']} gaps >5min, max gap {r['gap_max_s']}s")
    else:
        print("  no leaf has a >5min gap in window — mesh is reliable.")

    write_output(gaps, args.out, args.format)


if __name__ == "__main__":
    main()
