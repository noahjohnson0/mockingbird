#!/usr/bin/env python3
"""Analyze mockingbird BLE observations from the collector's SQLite DB.

Defaults to last 60 seconds. Time can be expressed several ways:
    --last 60s             last 60 seconds (default)
    --last 5m              last 5 minutes
    --last 1h              last hour
    --from <unix_ts>       absolute start
    --to <unix_ts>         absolute end (default: now)

Output sections:
    • Per-leaf rates + uplink health
    • Coverage histogram (singletons → universal)
    • Coverage matrix (top by best RSSI)
    • Spread leaders — devices most spatially-informative
    • Per-leaf singletons
    • What each leaf currently sees most clearly
    • Manufacturer + named-advertiser breakdown
    • Devices that appeared / disappeared during the window
"""

from __future__ import annotations

import argparse
import math
import re
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

DB_PATH = Path.home() / "mockingbird" / "observations.sqlite"

TX_POWER_1M = -59
PATH_LOSS_N = 2.5

MFG_ID = {
    "4c00": "Apple", "7500": "Samsung", "0600": "Microsoft", "0006": "Microsoft",
    "00e0": "Google", "0500": "Bose", "0a00": "Sony", "8700": "Garmin",
    "0700": "Belkin", "0227": "Tile", "0157": "Anhui Huami", "0188": "Govee",
}


def rssi_to_distance_m(rssi: float) -> float:
    return 10 ** ((TX_POWER_1M - rssi) / (10 * PATH_LOSS_N))


def label_mfg(manuf_hex: str | None) -> str:
    if not manuf_hex or len(manuf_hex) < 4:
        return "—"
    return MFG_ID.get(manuf_hex[:4].lower(), f"0x{manuf_hex[:4]}")


def parse_duration(s: str) -> float:
    m = re.fullmatch(r"(\d+)([smh])?", s.strip())
    if not m:
        raise argparse.ArgumentTypeError(f"bad duration {s!r}; try 60s / 5m / 1h")
    n, unit = int(m.group(1)), (m.group(2) or "s")
    return n * {"s": 1, "m": 60, "h": 3600}[unit]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", type=Path, default=DB_PATH)
    p.add_argument("--last", type=parse_duration, default=60.0,
                   help="window size; e.g. 60s, 5m, 1h (default 60s)")
    p.add_argument("--from", dest="ts_from", type=float, default=None)
    p.add_argument("--to",   dest="ts_to",   type=float, default=None)
    p.add_argument("--top",  type=int, default=25)
    args = p.parse_args()

    now = time.time()
    t_to   = args.ts_to   if args.ts_to   is not None else now
    t_from = args.ts_from if args.ts_from is not None else (t_to - args.last)
    window = t_to - t_from

    db = sqlite3.connect(str(args.db))
    db.row_factory = sqlite3.Row

    # ---- per-leaf stats ----
    leaf_stats = list(db.execute("""
        SELECT leaf, COUNT(*) AS n_obs, COUNT(DISTINCT mac) AS n_unique,
               MIN(ts) AS t_first, MAX(ts) AS t_last
        FROM obs
        WHERE ts >= ? AND ts < ?
        GROUP BY leaf ORDER BY leaf
    """, (t_from, t_to)))
    if not leaf_stats:
        print(f"no observations between {time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(t_from))}"
              f" and {time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(t_to))}")
        return 1

    leaves = [r["leaf"] for r in leaf_stats]
    short  = {l: l.replace("mockingbird-", "") for l in leaves}

    # ---- per (mac, leaf) aggregates ----
    rows = db.execute("""
        SELECT leaf, mac,
               AVG(rssi)  AS rssi_avg,
               MIN(rssi)  AS rssi_min,
               MAX(rssi)  AS rssi_max,
               COUNT(*)   AS count,
               MIN(ts)    AS t_first,
               MAX(ts)    AS t_last
        FROM obs
        WHERE ts >= ? AND ts < ?
        GROUP BY leaf, mac
    """, (t_from, t_to)).fetchall()

    # ---- per-mac metadata (latest non-null name/manuf in window) ----
    meta_rows = db.execute("""
        SELECT mac,
               (SELECT name  FROM obs o2 WHERE o2.mac=o.mac AND o2.name  IS NOT NULL AND ts < ? AND ts >= ? ORDER BY ts DESC LIMIT 1) AS name,
               (SELECT manuf FROM obs o2 WHERE o2.mac=o.mac AND o2.manuf IS NOT NULL AND ts < ? AND ts >= ? ORDER BY ts DESC LIMIT 1) AS manuf
        FROM (SELECT DISTINCT mac FROM obs WHERE ts >= ? AND ts < ?) o
    """, (t_to, t_from, t_to, t_from, t_from, t_to)).fetchall()
    meta = {r["mac"]: dict(r) for r in meta_rows}

    # leaf+mac index
    by_leaf_mac: dict[tuple[str, str], sqlite3.Row] = {(r["leaf"], r["mac"]): r for r in rows}
    by_mac: dict[str, dict[str, sqlite3.Row]] = defaultdict(dict)
    for r in rows:
        by_mac[r["mac"]][r["leaf"]] = r

    all_macs = list(by_mac.keys())
    seen_count = {m: len(by_mac[m]) for m in all_macs}

    # ---- header ----
    print("=" * 80)
    print(f"  mockingbird BLE — {time.strftime('%H:%M:%S', time.localtime(t_from))}"
          f" → {time.strftime('%H:%M:%S', time.localtime(t_to))}  "
          f"({window:.1f}s window, {len(leaves)} leaves, {len(all_macs)} devices)")
    print("=" * 80)
    print(f"  {'leaf':28} {'unique':>7} {'total':>7} {'pps':>5}  first→last")
    for r in leaf_stats:
        leaf_window = r["t_last"] - r["t_first"]
        pps = r["n_obs"] / leaf_window if leaf_window > 0 else 0
        print(f"  {r['leaf']:28} {r['n_unique']:>7} {r['n_obs']:>7} {pps:>5.1f}  "
              f"{time.strftime('%H:%M:%S', time.localtime(r['t_first']))}→"
              f"{time.strftime('%H:%M:%S', time.localtime(r['t_last']))}")

    # ---- uplink health (most recent heartbeats) ----
    hb_rows = db.execute("""
        SELECT leaf, info, ts FROM leaf_events
        WHERE event='hb' AND ts >= ?
        GROUP BY leaf HAVING ts = MAX(ts)
    """, (t_from,)).fetchall()
    if hb_rows:
        import json
        print()
        print(f"  {'leaf':28} {'heap':>8} {'sent':>8} {'drop':>5} {'qd':>3} {'rssi':>5}  age")
        for r in hb_rows:
            try:
                info = json.loads(r["info"])
            except Exception:
                continue
            age = now - r["ts"]
            print(f"  {r['leaf']:28} {info.get('heap','?'):>8} {info.get('n_sent','?'):>8} "
                  f"{info.get('n_dropped','?'):>5} {info.get('q_depth','?'):>3} "
                  f"{info.get('rssi','?'):>5}  {age:.1f}s ago")

    # ---- coverage histogram ----
    hist = {n: 0 for n in range(1, len(leaves) + 1)}
    for n in seen_count.values():
        hist[n] = hist.get(n, 0) + 1
    print()
    print("─" * 80)
    print("  COVERAGE HISTOGRAM")
    print("─" * 80)
    for n in range(1, len(leaves) + 1):
        bar = "█" * min(50, hist[n])
        tag = "(UNIVERSAL)" if n == len(leaves) else "(singleton)" if n == 1 else ""
        print(f"  seen by {n}/{len(leaves)} {tag:14}  {hist[n]:>3}  {bar}")

    # ---- coverage matrix ----
    multi = [m for m in all_macs if seen_count[m] >= 2]
    best_rssi = {m: max(by_mac[m][l]["rssi_avg"] for l in by_mac[m]) for m in multi}
    multi.sort(key=lambda m: -best_rssi[m])
    print()
    print("─" * 80)
    print(f"  COVERAGE MATRIX — top {args.top} multi-leaf devices")
    print("─" * 80)
    head = f"  {'MAC':19} " + " ".join(f"{short[l][:6]:>6}" for l in leaves) + "   mfg         name"
    print(head)
    print("  " + "-" * (len(head) - 2))
    for mac in multi[:args.top]:
        cells = []
        for l in leaves:
            r = by_mac[mac].get(l)
            if r:
                cells.append(f"{int(round(r['rssi_avg'])):>+6d}")
            else:
                cells.append(f"{'·':>6}")
        m = meta.get(mac, {})
        print(f"  {mac:19} " + " ".join(cells) +
              f"   {label_mfg(m.get('manuf','')):9}  {(m.get('name') or '')[:22]}")

    # ---- spread leaders ----
    if multi:
        spreads = []
        for mac in multi:
            samples = [(short[l], by_mac[mac][l]["rssi_avg"]) for l in by_mac[mac]]
            samples.sort(key=lambda x: -x[1])
            spread = samples[0][1] - samples[-1][1]
            spreads.append((spread, mac, samples))
        spreads.sort(reverse=True)
        print()
        print("─" * 80)
        print("  BIGGEST RSSI SPREAD — most spatially-informative (top 12)")
        print("  ratio = est. distance(farthest)/distance(closest), TxPower cancels out")
        print("─" * 80)
        for spread, mac, samples in spreads[:12]:
            ratio = 10 ** (spread / (10 * PATH_LOSS_N))
            close = f"{samples[0][0]}({int(round(samples[0][1])):+d})"
            far   = f"{samples[-1][0]}({int(round(samples[-1][1])):+d})"
            m = meta.get(mac, {})
            print(f"  {mac}  Δ{int(round(spread)):>3}dB  closest={close:>14}  "
                  f"farthest={far:>14}  ~{ratio:>4.1f}×  "
                  f"{label_mfg(m.get('manuf','')):9}  {(m.get('name') or '')[:18]}")

    # ---- per-leaf singletons ----
    print()
    print("─" * 80)
    print("  SINGLETONS PER LEAF — devices seen by only ONE scanner")
    print("─" * 80)
    for l in leaves:
        singles = [m for m in all_macs if seen_count[m] == 1 and l in by_mac[m]]
        singles.sort(key=lambda m: -by_mac[m][l]["rssi_avg"])
        print(f"  {l} ({len(singles)} singletons)")
        for mac in singles[:5]:
            r = by_mac[mac][l]
            mm = meta.get(mac, {})
            d = rssi_to_distance_m(r["rssi_avg"])
            print(f"    {mac}  rssi={int(round(r['rssi_avg'])):>+4d}  "
                  f"n={r['count']:>4}  ≈{d:>5.1f}m  "
                  f"{label_mfg(mm.get('manuf','')):9}  {(mm.get('name') or '')[:22]}")

    # ---- closest-to-each-leaf ----
    print()
    print("─" * 80)
    print("  WHAT EACH LEAF SEES MOST CLEARLY (top 3 by RSSI)")
    print("─" * 80)
    for l in leaves:
        leaf_rows = [r for r in rows if r["leaf"] == l]
        leaf_rows.sort(key=lambda r: -r["rssi_avg"])
        print(f"  {l}")
        for r in leaf_rows[:3]:
            m = meta.get(r["mac"], {})
            d = rssi_to_distance_m(r["rssi_avg"])
            print(f"    {r['mac']}  rssi={int(round(r['rssi_avg'])):>+4d}  "
                  f"≈{d:>5.1f}m  {label_mfg(m.get('manuf','')):9}  "
                  f"{(m.get('name') or '')[:22]}")

    # ---- manufacturer breakdown ----
    print()
    print("─" * 80)
    print("  MANUFACTURER BREAKDOWN")
    print("─" * 80)
    mfg_counts: dict[str, int] = defaultdict(int)
    for mac in all_macs:
        m = meta.get(mac, {})
        mfg_counts[label_mfg(m.get("manuf", ""))] += 1
    for m, c in sorted(mfg_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {m:>14}  {c}")

    # ---- named advertisers ----
    named = [(m, meta[m]["name"]) for m in all_macs if meta.get(m, {}).get("name")]
    if named:
        print()
        print("─" * 80)
        print("  NAMED ADVERTISERS — RSSI per leaf (· = not seen in window)")
        print("─" * 80)
        head = f"  {'name':32}  " + " ".join(f"{short[l][:6]:>6}" for l in leaves) + "   MAC"
        print(head)
        print("  " + "-" * (len(head) - 2))
        for mac, name in sorted(named, key=lambda x: (x[1] or '').lower()):
            cells = []
            for l in leaves:
                r = by_mac[mac].get(l)
                cells.append(f"{int(round(r['rssi_avg'])):>+6d}" if r else f"{'·':>6}")
            print(f"  {name[:32]:32}  " + " ".join(cells) + f"   {mac}")

    # ---- appearance / disappearance ----
    half_window = (t_from + t_to) / 2
    early_macs = set(r[0] for r in db.execute(
        "SELECT DISTINCT mac FROM obs WHERE ts >= ? AND ts < ?", (t_from, half_window)))
    late_macs = set(r[0] for r in db.execute(
        "SELECT DISTINCT mac FROM obs WHERE ts >= ? AND ts < ?", (half_window, t_to)))
    appeared = late_macs - early_macs
    disappeared = early_macs - late_macs

    if appeared or disappeared:
        print()
        print("─" * 80)
        print("  CHURN — devices that crossed the window midpoint")
        print(f"  midpoint: {time.strftime('%H:%M:%S', time.localtime(half_window))}")
        print("─" * 80)
        if appeared:
            print(f"  APPEARED ({len(appeared)}): seen in 2nd half only")
            for mac in list(appeared)[:8]:
                mm = meta.get(mac, {})
                print(f"    {mac}  {label_mfg(mm.get('manuf','')):9}  {(mm.get('name') or '')[:22]}")
        if disappeared:
            print(f"  DISAPPEARED ({len(disappeared)}): seen in 1st half only")
            for mac in list(disappeared)[:8]:
                mm = meta.get(mac, {})
                print(f"    {mac}  {label_mfg(mm.get('manuf','')):9}  {(mm.get('name') or '')[:22]}")

    print()
    print("─" * 80)
    print(f"  Path-loss: d = 10^((TxPower-RSSI)/(10·N))  TxPower={TX_POWER_1M} dBm  N={PATH_LOSS_N}")
    print(f"  Window: {window:.1f}s ({args.last:.0f}s requested)  "
          f"Total observations: {sum(r['n_obs'] for r in leaf_stats):,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
