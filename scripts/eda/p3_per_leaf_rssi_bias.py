#!/usr/bin/env python3
"""
Priority 3 EDA — Per-leaf RSSI bias on universal devices.

QUESTION
--------
After controlling for the *device* being observed, is each leaf's RSSI
systematically biased high or low? With what statistical confidence?

WHY IT MATTERS
--------------
A ~4 dB stationary per-leaf bias maps to roughly 50–60% linear-distance
error at typical indoor path-loss exponents (n ≈ 2.5). The team shipped
a TX/RX bias decomposition (commit 6703762); this analysis is the
empirical check on whether that decomposition is actually doing its
job in the *current* data. The residual after removing per-device mean
should be small AND have a CI that includes zero.

METHOD (data-generating process)
--------------------------------
1. Find MACs observed by all currently-active leaves in the window
   ("universal" devices). This removes the confound where one leaf sees
   more weak/distant devices than another.
2. For each (mac, leaf), compute mean RSSI.
3. Per-MAC, subtract the cross-leaf mean → residual RSSI matrix.
4. Per leaf, the mean residual is the empirical bias. Bootstrap a 95% CI
   across the MAC population.

We do NOT assume the noise is Gaussian. The bootstrap respects the
empirical RSSI distribution (which is quantized at 1 dB and often skewed).

HOW TO INTERPRET
----------------
- A leaf whose 95% CI excludes zero by >1 dB is biased after the existing
  decomposition. Either the decomposition is under-specified (e.g.
  missing an antenna-orientation term), or its fit is stale.
- The number of universal MACs (`n_universal`) is your statistical power.
  Below ~5 MACs the CIs will be wide and unactionable — try a longer
  window or a shorter list of "active" leaves.
- The print includes a per-leaf RSSI quantization check (most-common
  integer dBm value and its share). Plateaus >30% mean the leaf's chip
  is reporting a clipped/discretized value and downstream Gaussian
  assumptions will be wrong.

USAGE
-----
    python scripts/eda/p3_per_leaf_rssi_bias.py --since 1h
    python scripts/eda/p3_per_leaf_rssi_bias.py --since 4h --out bias.csv
"""

from __future__ import annotations

import argparse
import random
from collections import Counter, defaultdict

from _common import (
    add_common_args,
    print_header,
    print_table,
    standard_setup,
    write_output,
)


SQL_ACTIVE = """
SELECT leaf, COUNT(*) AS n_obs
FROM obs
WHERE ts BETWEEN :t0 AND :t1
GROUP BY leaf
ORDER BY leaf
"""

# universal MACs: observed by every active leaf
SQL_UNIVERSAL = """
WITH active AS (
  SELECT DISTINCT leaf FROM obs WHERE ts BETWEEN :t0 AND :t1
),
per_mac AS (
  SELECT mac, COUNT(DISTINCT leaf) AS n_leaves
  FROM obs
  WHERE ts BETWEEN :t0 AND :t1
  GROUP BY mac
)
SELECT mac
FROM per_mac
WHERE n_leaves = (SELECT COUNT(*) FROM active)
"""

SQL_MEAN_RSSI = """
SELECT mac, leaf, AVG(rssi) AS rssi_mean, COUNT(*) AS n
FROM obs
WHERE ts BETWEEN ? AND ?
  AND mac IN ({placeholders})
GROUP BY mac, leaf
"""

SQL_RSSI_VALUES = """
SELECT leaf, rssi
FROM obs
WHERE ts BETWEEN ? AND ?
  AND mac IN ({placeholders})
"""


def bootstrap_ci(values: list[float], n_resamples: int = 2000, seed: int = 0) -> tuple[float, float, float]:
    """Return (mean, lo95, hi95). Empty list → (nan, nan, nan)."""
    if not values:
        nan = float("nan")
        return (nan, nan, nan)
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(n_resamples):
        s = 0.0
        for _ in range(n):
            s += values[rng.randrange(n)]
        means.append(s / n)
    means.sort()
    lo = means[int(0.025 * n_resamples)]
    hi = means[int(0.975 * n_resamples) - 1]
    return (sum(values) / n, lo, hi)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_args(p)
    p.add_argument("--resamples", type=int, default=2000, help="bootstrap resamples (default: 2000)")
    p.add_argument("--seed", type=int, default=0, help="bootstrap RNG seed")
    args, con, window = standard_setup(p)

    print_header("Priority 3 — Per-leaf RSSI bias (universal devices)", window)

    active = [dict(r) for r in con.execute(SQL_ACTIVE, {"t0": window.t0, "t1": window.t1})]
    if not active:
        print("(no observations in window)")
        return
    print(f"active leaves: {len(active)}")
    print_table(active, columns=["leaf", "n_obs"])

    universal = [r["mac"] for r in con.execute(SQL_UNIVERSAL, {"t0": window.t0, "t1": window.t1})]
    print(f"\nuniversal MACs (seen by ALL {len(active)} active leaves): {len(universal)}")
    if len(universal) < 3:
        print(
            "  too few universal MACs for a stable bias estimate. "
            "Try a longer window, or restrict to a known-static device set."
        )
        return

    # Pull per-(mac, leaf) means
    placeholders = ",".join("?" for _ in universal)
    sql_mean = SQL_MEAN_RSSI.format(placeholders=placeholders)
    params = [window.t0, window.t1, *universal]
    pair_means: dict[tuple[str, str], float] = {}
    leaves = sorted({r["leaf"] for r in active})
    for row in con.execute(sql_mean, params):
        pair_means[(row["mac"], row["leaf"])] = row["rssi_mean"]

    # Per-MAC cross-leaf mean → residuals
    residuals_by_leaf: dict[str, list[float]] = defaultdict(list)
    for mac in universal:
        vals = [pair_means.get((mac, leaf)) for leaf in leaves]
        if None in vals:
            continue
        cross_mean = sum(vals) / len(vals)
        for leaf, v in zip(leaves, vals):
            residuals_by_leaf[leaf].append(v - cross_mean)

    print("\n-- Per-leaf bias (mean residual RSSI, dB) --")
    rows = []
    for leaf in leaves:
        vals = residuals_by_leaf.get(leaf, [])
        m, lo, hi = bootstrap_ci(vals, n_resamples=args.resamples, seed=args.seed)
        ci_excludes_zero = (lo > 0) or (hi < 0)
        rows.append({
            "leaf": leaf,
            "n_macs": len(vals),
            "bias_db": round(m, 2),
            "lo95": round(lo, 2),
            "hi95": round(hi, 2),
            "flag": "*" if ci_excludes_zero and abs(m) >= 1.0 else "",
        })
    rows.sort(key=lambda r: r["bias_db"])
    print_table(rows, columns=["leaf", "n_macs", "bias_db", "lo95", "hi95", "flag"], max_rows=None)
    print("  flag '*' = CI excludes zero AND |bias| >= 1 dB (actionable residual)")

    # Quantization check
    print("\n-- RSSI quantization plateau check (per leaf) --")
    rssi_by_leaf: dict[str, list[int]] = defaultdict(list)
    sql_raw = SQL_RSSI_VALUES.format(placeholders=placeholders)
    for r in con.execute(sql_raw, params):
        rssi_by_leaf[r["leaf"]].append(r["rssi"])

    q_rows = []
    for leaf in leaves:
        vals = rssi_by_leaf.get(leaf, [])
        if not vals:
            continue
        c = Counter(vals)
        top_val, top_n = c.most_common(1)[0]
        share = 100.0 * top_n / len(vals)
        q_rows.append({
            "leaf": leaf,
            "n_obs": len(vals),
            "mode_dbm": top_val,
            "mode_share_pct": round(share, 1),
            "flag": "*" if share > 30 else "",
        })
    print_table(q_rows, columns=["leaf", "n_obs", "mode_dbm", "mode_share_pct", "flag"], max_rows=None)
    print("  flag '*' = >30% of observations sit on one integer dBm value (real quantization).")

    # Flags
    print("\n-- Summary --")
    flagged = [r for r in rows if r["flag"]]
    if not flagged:
        print("  No leaf has an actionable residual bias. The existing TX/RX decomposition looks healthy.")
    else:
        print(f"  {len(flagged)} leaf/leaves show actionable residual bias:")
        for r in flagged:
            print(f"    {r['leaf']}: {r['bias_db']} dB  [{r['lo95']}, {r['hi95']}]  (n={r['n_macs']})")
        print("  Recommend: refit TX/RX bias on a recent window, or extend the model.")

    write_output(rows, args.out, args.format)


if __name__ == "__main__":
    main()
