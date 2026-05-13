#!/usr/bin/env python3
"""
EDA — Per-leaf observation rates.

QUESTION
--------
Are the leaves contributing roughly balanced volumes of observations,
or is one dominating / silent / bursty?

WHY IT MATTERS
--------------
Imbalanced contribution biases every downstream fusion. A leaf that
contributes 10× the data of another implicitly anchors the fused
estimate, even if its position is no better. We need to know who's
talking and who's quiet — and *why* — before fusing.

HOW TO INTERPRET
----------------
- `obs_per_sec` is the rate, in observations per wall-second, in the
  window. Compare leaves on this, not raw `n_obs` (windows might differ).
- `n_devices` is the unique-MAC count seen by that leaf. A leaf with
  high `n_obs` but average `n_devices` is over-observing the same set
  (duty-cycle or scan-window config drift, possibly a firmware bug).
- `<30% of median rate while still emitting heartbeats` → BLE-stack
  starvation under WiFi load, antenna trouble, or shadowing.
- `>2× median rate` → privileged RF position (document it) OR a
  duplicate-emission firmware bug (cross-check with n_devices).

USAGE
-----
    python scripts/eda/p4_per_leaf_rates.py --since 1h
"""

from __future__ import annotations

import argparse
from statistics import median

from _common import (
    add_common_args,
    print_header,
    print_table,
    standard_setup,
    write_output,
)


SQL_RATES = """
SELECT
    leaf,
    COUNT(*)                                     AS n_obs,
    COUNT(DISTINCT mac)                          AS n_devices,
    MIN(rssi)                                    AS rssi_min,
    MAX(rssi)                                    AS rssi_max,
    ROUND(AVG(rssi), 1)                          AS rssi_mean,
    MIN(ts)                                      AS ts_first,
    MAX(ts)                                      AS ts_last
FROM obs
WHERE ts BETWEEN :t0 AND :t1
GROUP BY leaf
ORDER BY n_obs DESC
"""


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_args(p)
    args, con, window = standard_setup(p)

    print_header("Per-leaf observation rates", window)

    rows = [dict(r) for r in con.execute(SQL_RATES, {"t0": window.t0, "t1": window.t1})]
    if not rows:
        print("(no observations in window)")
        return

    span = window.span_s if window.span_s > 0 else 1.0
    for r in rows:
        r["obs_per_sec"] = round(r["n_obs"] / span, 2)
        r["obs_per_device"] = round(r["n_obs"] / r["n_devices"], 1) if r["n_devices"] else 0.0

    med_rate = median(r["obs_per_sec"] for r in rows)
    for r in rows:
        ratio = r["obs_per_sec"] / med_rate if med_rate else 1.0
        if ratio < 0.3:
            r["flag"] = "low"
        elif ratio > 2.0:
            r["flag"] = "high"
        else:
            r["flag"] = ""

    print_table(
        rows,
        columns=["leaf", "n_obs", "obs_per_sec", "n_devices", "obs_per_device", "rssi_mean", "rssi_min", "rssi_max", "flag"],
        max_rows=None,
    )
    print(f"\nmedian rate: {med_rate:.2f} obs/sec/leaf")
    print("  flag 'low'  = <30% of median rate (investigate BLE duty cycle / RF)")
    print("  flag 'high' = >200% of median rate (privileged position or dup emission)")

    write_output(rows, args.out, args.format)


if __name__ == "__main__":
    main()
