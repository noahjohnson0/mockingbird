#!/usr/bin/env python3
"""
EDA — Device churn (static vs. mobile signature).

QUESTION
--------
Which MACs are continuously present (likely static beacons) vs.
transient (a phone walking past)? How much of the unique-MAC count is
real churn vs. random-MAC rotation?

WHY IT MATTERS
--------------
Motion-tracking and identity tracking need different treatment for these
two classes. The set of persistent devices is also our de-facto
calibration cohort for RSSI bias work; if it's too small, bias analyses
are statistically thin.

HOW TO INTERPRET
----------------
- `appeared` = present only in the second half of the window.
- `disappeared` = present only in the first half.
- `persistent` = present in both halves.
- High churn fraction (>50% of MACs per hour) is expected when phones
  with rotating random MACs dominate (see p1). Sanity-check against
  `addr_type`: random churn ≠ real device churn.

USAGE
-----
    python scripts/eda/p5_device_churn.py --since 1h
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


SQL_PERMAC = """
SELECT
    mac,
    MIN(ts)                          AS ts_first,
    MAX(ts)                          AS ts_last,
    MAX(ts) - MIN(ts)                AS span_s,
    COUNT(*)                         AS n_obs,
    COUNT(DISTINCT leaf)             AS n_leaves,
    MAX(COALESCE(addr_type, -1))     AS addr_type
FROM obs
WHERE ts BETWEEN :t0 AND :t1
GROUP BY mac
"""

SQL_HALVES = """
WITH halves AS (
  SELECT mac,
         SUM(CASE WHEN ts < :tmid THEN 1 ELSE 0 END)  AS n_first,
         SUM(CASE WHEN ts >= :tmid THEN 1 ELSE 0 END) AS n_second,
         MAX(COALESCE(addr_type, -1)) AS addr_type
  FROM obs
  WHERE ts BETWEEN :t0 AND :t1
  GROUP BY mac
)
SELECT
  CASE
    WHEN n_first = 0 AND n_second > 0 THEN 'appeared'
    WHEN n_first > 0 AND n_second = 0 THEN 'disappeared'
    ELSE 'persistent'
  END                                       AS class,
  CASE WHEN addr_type IN (1,3) THEN 'random' ELSE 'public' END AS addr_kind,
  COUNT(*)                                  AS n_devices
FROM halves
GROUP BY class, addr_kind
ORDER BY class, addr_kind
"""


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_args(p)
    p.add_argument("--top", type=int, default=15, help="top-N most-stationary devices to list (default: 15)")
    args, con, window = standard_setup(p)

    print_header("Device churn", window)

    permac = [dict(r) for r in con.execute(SQL_PERMAC, {"t0": window.t0, "t1": window.t1})]
    if not permac:
        print("(no observations in window)")
        return
    for r in permac:
        span = r["span_s"] or 0.0
        r["obs_per_s"] = round(r["n_obs"] / span, 3) if span > 0 else None
        r["span_h"] = round(span / 3600, 2)

    # most-stationary = longest span + many obs + many leaves
    top = sorted(permac, key=lambda r: (r["span_s"], r["n_obs"], r["n_leaves"]), reverse=True)[: args.top]
    print(f"\n-- Top {len(top)} candidate static / persistent devices --")
    print_table(top, columns=["mac", "span_h", "n_obs", "n_leaves", "addr_type", "obs_per_s"], max_rows=None)
    print(
        "  Long span + many leaves + steady obs_per_s = likely static beacon. "
        "These are your calibration cohort."
    )

    # appeared / disappeared / persistent
    tmid = 0.5 * (window.t0 + window.t1)
    halves = [dict(r) for r in con.execute(SQL_HALVES, {"t0": window.t0, "t1": window.t1, "tmid": tmid})]
    print(f"\n-- Midpoint churn (split at {window.span_s/2/3600:.2f}h into window) --")
    print_table(halves, columns=["class", "addr_kind", "n_devices"], max_rows=None)

    totals = {"appeared": 0, "disappeared": 0, "persistent": 0}
    rand = {"appeared": 0, "disappeared": 0, "persistent": 0}
    for r in halves:
        totals[r["class"]] += r["n_devices"]
        if r["addr_kind"] == "random":
            rand[r["class"]] += r["n_devices"]
    n_total = sum(totals.values()) or 1
    print()
    for k in ("persistent", "appeared", "disappeared"):
        n = totals[k]
        rpct = 100.0 * rand[k] / n if n else 0.0
        print(f"  {k:11s}: {n:5d} ({100.0*n/n_total:.0f}%)  random-MAC share: {rpct:.0f}%")
    print()
    if totals["persistent"] < 10:
        print(
            "  [!] Only {} persistent devices in this window. Calibration "
            "cohort is thin; widen the window or add static beacons.".format(totals["persistent"])
        )

    write_output(top, args.out, args.format)


if __name__ == "__main__":
    main()
