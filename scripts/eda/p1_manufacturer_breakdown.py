#!/usr/bin/env python3
"""
Priority 1 EDA — Manufacturer / advertiser / address-type breakdown.

QUESTION
--------
What does the BLE device population look like? Top manufacturers, top
named advertisers, public-vs-random address split.

WHY IT MATTERS
--------------
Every other analysis rests on knowing the population. If 70% of
observations are random-MAC iPhones rotating every 15 min, the meaning
of "device" downstream is different from a population dominated by
static public-MAC beacons. Run this first.

HOW TO INTERPRET
----------------
- `manuf_id` is the BLE Company ID (little-endian hex of the first two
  bytes of the manufacturer-specific data). 004C = Apple, 0006 = Microsoft,
  0075 = Samsung. We don't ship the full mapping here; eyeball the top
  few and consult the SIG list for unknowns.
- A single manufacturer >50% of `n_obs` means downstream calibration
  analyses should be re-run *excluding* that manufacturer, to check the
  bias estimate isn't just one device's RF signature.
- A high random-MAC fraction (>80%) means we'll systematically
  underestimate persistence and overestimate churn until we build an
  identity-clustering layer.
- A short window (e.g. 10m) is fine here — the population mix is
  stable on minute timescales. Long windows mostly add MAC rotation noise.

USAGE
-----
    python scripts/eda/p1_manufacturer_breakdown.py
    python scripts/eda/p1_manufacturer_breakdown.py --since 6h --out manuf.csv
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


SQL_MANUF = """
SELECT
    SUBSTR(manuf, 1, 4)   AS manuf_id,
    COUNT(*)              AS n_obs,
    COUNT(DISTINCT mac)   AS n_devices,
    ROUND(AVG(rssi), 1)   AS rssi_mean
FROM obs
WHERE ts BETWEEN :t0 AND :t1
  AND manuf IS NOT NULL
  AND length(manuf) >= 4
GROUP BY manuf_id
ORDER BY n_obs DESC
LIMIT :limit
"""

SQL_NAMES = """
SELECT
    name,
    COUNT(DISTINCT mac)   AS n_devices,
    COUNT(*)              AS n_obs,
    ROUND(AVG(rssi), 1)   AS rssi_mean
FROM obs
WHERE ts BETWEEN :t0 AND :t1
  AND name IS NOT NULL
  AND name <> ''
GROUP BY name
ORDER BY n_obs DESC
LIMIT :limit
"""

SQL_ADDR = """
SELECT
    COALESCE(addr_type, -1)  AS addr_type,
    COUNT(*)                 AS n_obs,
    COUNT(DISTINCT mac)      AS n_macs
FROM obs
WHERE ts BETWEEN :t0 AND :t1
GROUP BY addr_type
ORDER BY n_obs DESC
"""

SQL_TOTALS = """
SELECT
    COUNT(*)                AS n_obs_total,
    COUNT(DISTINCT mac)     AS n_macs_total,
    SUM(CASE WHEN manuf IS NOT NULL THEN 1 ELSE 0 END) AS n_obs_with_manuf,
    SUM(CASE WHEN name  IS NOT NULL AND name <> '' THEN 1 ELSE 0 END) AS n_obs_with_name
FROM obs
WHERE ts BETWEEN :t0 AND :t1
"""


ADDR_TYPE_LABELS = {
    0: "public",
    1: "random",
    2: "rpa-public",
    3: "rpa-random",
    -1: "unknown",
}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_args(p)
    p.add_argument("--limit", type=int, default=20, help="top-N rows per table (default: 20)")
    args, con, window = standard_setup(p)

    print_header("Priority 1 — Manufacturer / advertiser / address-type", window)

    totals = dict(con.execute(SQL_TOTALS, {"t0": window.t0, "t1": window.t1}).fetchone())
    n_obs = totals["n_obs_total"] or 0
    n_macs = totals["n_macs_total"] or 0
    print(f"total observations: {n_obs:,}")
    print(f"unique MACs:        {n_macs:,}")
    if n_obs:
        pct_manuf = 100.0 * (totals["n_obs_with_manuf"] or 0) / n_obs
        pct_name = 100.0 * (totals["n_obs_with_name"] or 0) / n_obs
        print(f"with manuf data:    {totals['n_obs_with_manuf']:,} ({pct_manuf:.1f}%)")
        print(f"with local name:    {totals['n_obs_with_name']:,} ({pct_name:.1f}%)")

    # Address-type split
    print("\n-- Address-type split --")
    addr_rows = [dict(r) for r in con.execute(SQL_ADDR, {"t0": window.t0, "t1": window.t1})]
    for r in addr_rows:
        r["addr_label"] = ADDR_TYPE_LABELS.get(r["addr_type"], f"?{r['addr_type']}")
        r["pct_obs"] = round(100.0 * r["n_obs"] / n_obs, 1) if n_obs else 0.0
    print_table(addr_rows, columns=["addr_type", "addr_label", "n_obs", "n_macs", "pct_obs"])

    # Manufacturer table
    print("\n-- Top manufacturers (by observations) --")
    manuf_rows = [
        dict(r)
        for r in con.execute(
            SQL_MANUF, {"t0": window.t0, "t1": window.t1, "limit": args.limit}
        )
    ]
    for r in manuf_rows:
        r["pct_obs"] = round(100.0 * r["n_obs"] / n_obs, 1) if n_obs else 0.0
    print_table(manuf_rows, columns=["manuf_id", "n_obs", "n_devices", "rssi_mean", "pct_obs"])

    # Named advertisers
    print("\n-- Top named advertisers --")
    name_rows = [
        dict(r)
        for r in con.execute(
            SQL_NAMES, {"t0": window.t0, "t1": window.t1, "limit": args.limit}
        )
    ]
    print_table(name_rows, columns=["name", "n_devices", "n_obs", "rssi_mean"])

    # Quick interpretive flags
    print("\n-- Flags --")
    if manuf_rows:
        top = manuf_rows[0]
        if top.get("pct_obs", 0) > 50:
            print(
                f"  [!] manuf_id {top['manuf_id']} dominates "
                f"({top['pct_obs']}% of obs). Re-run bias EDA excluding it as a check."
            )
        else:
            print("  manufacturer mix looks reasonably balanced (top < 50% of obs).")
    rand_obs = sum(
        r["n_obs"] for r in addr_rows if r["addr_type"] in (1, 3)
    )
    if n_obs:
        rand_pct = 100.0 * rand_obs / n_obs
        if rand_pct > 80:
            print(
                f"  [!] {rand_pct:.0f}% of obs are random-MAC addresses. "
                "Persistence / churn metrics will be unreliable without "
                "an identity-clustering layer."
            )
        else:
            print(f"  random-MAC obs fraction: {rand_pct:.0f}% (acceptable).")

    # Primary table written out: manufacturer breakdown
    write_output(manuf_rows, args.out, args.format)


if __name__ == "__main__":
    main()
