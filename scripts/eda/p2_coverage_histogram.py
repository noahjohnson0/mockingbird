#!/usr/bin/env python3
"""
Priority 2 EDA — Coverage histogram.

QUESTION
--------
For a typical BLE device seen in this window, how many of our leaves
observed it?

WHY IT MATTERS
--------------
Trilateration's lower bound is set by coverage, not by algorithm:
- 1-2 leaves: position is unconstrained (a line at best).
- 3 leaves: a poorly-conditioned 2D solve.
- 4+ leaves: well-posed.
If the median device is seen by <4 leaves, no modeling change will
rescue the position estimate. The fix is physical (placement, density),
not algorithmic.

HOW TO INTERPRET
----------------
- The histogram is over UNIQUE MACs, not observations. A device seen
  10000 times by one leaf still counts as `n_leaves = 1`.
- The "window" matters: choose it small enough that random-MAC rotation
  doesn't fragment a single device into many entries. 5–10 min is a
  good default for a coverage census; the default 24h is fine for "what
  was the deployment doing all day."
- Compare against the active-leaf count printed at the top. If
  `median(n_leaves) >= ceil(active_leaves / 2)` the deployment is
  basically working.

USAGE
-----
    python scripts/eda/p2_coverage_histogram.py --since 10m
    python scripts/eda/p2_coverage_histogram.py --since 1h --out coverage.csv
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


SQL_ACTIVE = """
SELECT COUNT(DISTINCT leaf) AS n_leaves
FROM obs
WHERE ts BETWEEN :t0 AND :t1
"""

SQL_PER_MAC = """
SELECT mac, COUNT(DISTINCT leaf) AS n_leaves
FROM obs
WHERE ts BETWEEN :t0 AND :t1
GROUP BY mac
"""

SQL_SINGLETONS = """
SELECT leaf, COUNT(*) AS n_singleton_macs
FROM (
  SELECT mac, MIN(leaf) AS leaf, COUNT(DISTINCT leaf) AS n_leaves
  FROM obs
  WHERE ts BETWEEN :t0 AND :t1
  GROUP BY mac
  HAVING n_leaves = 1
)
GROUP BY leaf
ORDER BY n_singleton_macs DESC
"""


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_args(p)
    args, con, window = standard_setup(p)

    print_header("Priority 2 — Coverage histogram", window)

    n_active = con.execute(SQL_ACTIVE, {"t0": window.t0, "t1": window.t1}).fetchone()["n_leaves"]
    print(f"active leaves in window: {n_active}")

    per_mac = [dict(r) for r in con.execute(SQL_PER_MAC, {"t0": window.t0, "t1": window.t1})]
    if not per_mac:
        print("(no observations in window)")
        return

    counts = [r["n_leaves"] for r in per_mac]
    hist: dict[int, int] = {}
    for c in counts:
        hist[c] = hist.get(c, 0) + 1

    max_n = max(hist.keys())
    rows = []
    cum = 0
    total = len(counts)
    # bars: max width 40 chars
    max_bar = max(hist.values())
    for k in range(1, max(max_n, n_active) + 1):
        n = hist.get(k, 0)
        cum += n
        pct = 100.0 * n / total
        cum_pct = 100.0 * cum / total
        bar = "#" * int(round(40 * n / max_bar)) if max_bar else ""
        rows.append({
            "n_leaves": k,
            "n_devices": n,
            "pct": round(pct, 1),
            "cum_pct": round(cum_pct, 1),
            "bar": bar,
        })

    print("\n-- Devices by number of leaves that saw them --")
    print_table(rows, columns=["n_leaves", "n_devices", "pct", "cum_pct", "bar"], max_rows=None)

    med = median(counts)
    p90 = sorted(counts)[max(0, int(0.9 * total) - 1)]
    trilatable = sum(1 for c in counts if c >= 3)
    well_posed = sum(1 for c in counts if c >= 4)
    print()
    print(f"unique MACs:                {total}")
    print(f"median coverage (leaves):   {med}")
    print(f"90th percentile coverage:   {p90}")
    print(f"devices with >=3 leaves:    {trilatable} ({100.0*trilatable/total:.1f}%)  [trilateration possible]")
    print(f"devices with >=4 leaves:    {well_posed} ({100.0*well_posed/total:.1f}%)  [well-posed]")

    # Per-leaf singleton breakdown
    print("\n-- Lone-observer counts (which leaves are the only one seeing a device) --")
    sing_rows = [dict(r) for r in con.execute(SQL_SINGLETONS, {"t0": window.t0, "t1": window.t1})]
    print_table(sing_rows, columns=["leaf", "n_singleton_macs"])
    if sing_rows:
        print(
            "  High singleton counts on one leaf = a coverage hole at the "
            "boundary of that leaf's RF range. Worth a neighbor."
        )

    # Flags
    print("\n-- Flags --")
    if n_active >= 4:
        if med >= 4:
            print("  median coverage >= 4. Trilateration is broadly viable on this deployment.")
        elif med >= 3:
            print("  median coverage = 3. Trilateration is marginal; expect noisy 2D positions.")
        else:
            print(
                f"  median coverage = {med}. Most devices are NOT trilatable. "
                "Modeling won't fix this — densify the deployment."
            )
    else:
        print(f"  only {n_active} active leaves in window — coverage analysis is structurally limited.")

    write_output(rows, args.out, args.format)


if __name__ == "__main__":
    main()
