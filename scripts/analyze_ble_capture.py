#!/usr/bin/env python3
"""Analyze BLE-capture JSONs from N mockingbird leaves.

Accepts 2 or more capture files. The outputs scale: with 2 leaves you mostly
get pairwise comparison; with 3+ leaves you also get a coverage matrix,
per-leaf singleton contributions, and per-device RSSI spread (the spatial
information that makes a multi-leaf mesh more than the sum of its parts).

Usage:
    analyze_ble_capture.py <a.json> <b.json> [<c.json> ...]
"""

from __future__ import annotations

import json
import sys


# Log-distance path-loss model.
#   RSSI = TxPower - 10·n·log10(d)
#   d    = 10 ^ ((TxPower − RSSI) / (10·n))
TX_POWER_1M = -59   # generic class-2 BLE calibration assumption
PATH_LOSS_N = 2.5   # indoor (free space = 2.0, dense indoor = 3.0+)


MFG_ID = {
    "4c00": "Apple", "7500": "Samsung", "0600": "Microsoft", "0006": "Microsoft",
    "00e0": "Google", "0500": "Bose", "0a00": "Sony", "8700": "Garmin",
    "0700": "Belkin", "0227": "Tile", "0157": "Anhui Huami", "0188": "Govee",
}


def rssi_to_distance_m(rssi: int) -> float:
    return 10 ** ((TX_POWER_1M - rssi) / (10 * PATH_LOSS_N))


def label_mfg(manuf_hex: str) -> str:
    if not manuf_hex or len(manuf_hex) < 4:
        return "—"
    return MFG_ID.get(manuf_hex[:4].lower(), f"0x{manuf_hex[:4]}")


def main():
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        sys.exit(2)

    caps = [json.load(open(p)) for p in sys.argv[1:]]
    leaves = [c["hostname"] for c in caps]
    short  = [l.replace("mockingbird-", "") for l in leaves]
    per_leaf_obs = [{o["mac"]: o for o in c["observations"]} for c in caps]
    all_macs = set().union(*[set(d) for d in per_leaf_obs])

    # ---- 1. header / per-leaf stats ----
    print("=" * 80)
    print(f"  mockingbird BLE capture — {len(caps)} leaves, union of {len(all_macs)} devices")
    print("=" * 80)
    print(f"  {'leaf':28} {'unique':>7} {'total':>7} {'pps':>5}  window")
    for c in caps:
        win = c["scan_window_ms"] / 1000
        pps = c["n_total_obs"] / win if win > 0 else 0
        print(f"  {c['hostname']:28} {c['n_unique']:>7} {c['n_total_obs']:>7} {pps:>5.1f}  {win:.1f}s")

    # ---- 2. coverage histogram ----
    seen_count = {m: sum(1 for d in per_leaf_obs if m in d) for m in all_macs}
    hist = {n: 0 for n in range(1, len(caps) + 1)}
    for n in seen_count.values():
        hist[n] += 1
    print()
    print("─" * 80)
    print("  COVERAGE HISTOGRAM — how many leaves saw each device")
    print("─" * 80)
    for n in range(1, len(caps) + 1):
        bar = "█" * min(50, hist[n])
        tag = "(UNIVERSAL — all leaves)" if n == len(caps) else \
              "(singleton — one leaf only)" if n == 1 else ""
        print(f"  seen by {n}/{len(caps)} {tag:30}  {hist[n]:>3}  {bar}")

    # ---- 3. coverage matrix (RSSI per leaf, top by best signal) ----
    multi = [m for m in all_macs if seen_count[m] >= 2]
    best_rssi = {m: max(d[m]["rssi_avg"] for d in per_leaf_obs if m in d) for m in multi}
    multi.sort(key=lambda m: -best_rssi[m])
    print()
    print("─" * 80)
    print(f"  COVERAGE MATRIX — multi-leaf devices, sorted by best RSSI (top 25)")
    print("─" * 80)
    head = f"  {'MAC':19} " + " ".join(f"{s[:6]:>6}" for s in short) + "   mfg         name"
    print(head)
    print("  " + "-" * (len(head) - 2))
    for mac in multi[:25]:
        cells = []
        for d in per_leaf_obs:
            if mac in d:
                cells.append(f"{d[mac]['rssi_avg']:>+6d}")
            else:
                cells.append(f"{'·':>6}")
        info = next(d[mac] for d in per_leaf_obs if mac in d)
        mfg = label_mfg(info.get("manuf", ""))
        name = (info.get("name") or "")[:22]
        print(f"  {mac:19} " + " ".join(cells) + f"   {mfg:9}  {name}")

    # ---- 4. spatial information: biggest RSSI spread ----
    spread_rows = []
    for mac in multi:
        rssis = sorted(
            ((short[i], d[mac]["rssi_avg"]) for i, d in enumerate(per_leaf_obs) if mac in d),
            key=lambda x: -x[1],
        )
        spread = rssis[0][1] - rssis[-1][1]
        spread_rows.append((spread, mac, rssis))
    spread_rows.sort(reverse=True)
    if spread_rows and len(caps) >= 2:
        print()
        print("─" * 80)
        print("  BIGGEST RSSI SPREAD — most spatially-informative devices (top 12)")
        print("  Each has a clear 'closest' leaf; ratio = est. distance(farthest)/distance(closest)")
        print("─" * 80)
        for spread, mac, rssis in spread_rows[:12]:
            info = next(d[mac] for d in per_leaf_obs if mac in d)
            mfg = label_mfg(info.get("manuf", ""))
            name = (info.get("name") or "")[:18]
            ratio = 10 ** (spread / (10 * PATH_LOSS_N))
            close = f"{rssis[0][0]}({rssis[0][1]:+d})"
            far   = f"{rssis[-1][0]}({rssis[-1][1]:+d})"
            print(f"  {mac}  Δ{spread:>3}dB  closest={close:>14}  farthest={far:>14}  "
                  f"~{ratio:>4.1f}×  {mfg:9}  {name}")

    # ---- 5. per-leaf singletons (what only THIS leaf sees) ----
    print()
    print("─" * 80)
    print(f"  PER-LEAF SINGLETONS — devices seen by exactly one scanner (top 5 strongest)")
    print("─" * 80)
    for leaf, obs in zip(leaves, per_leaf_obs):
        singles = sorted(
            [m for m in obs if seen_count[m] == 1],
            key=lambda m: -obs[m]["rssi_avg"],
        )
        print(f"  {leaf}  ({len(singles)} singletons)")
        for mac in singles[:5]:
            o = obs[mac]
            mfg = label_mfg(o.get("manuf", ""))
            name = (o.get("name") or "")[:22]
            d = rssi_to_distance_m(o["rssi_avg"])
            print(f"    {mac}  rssi={o['rssi_avg']:>+4d}  count={o['count']:>4}  "
                  f"≈{d:>5.1f}m  {mfg:9}  {name}")

    # ---- 6. closest-to-each-leaf ----
    print()
    print("─" * 80)
    print("  WHAT EACH LEAF SEES MOST CLEARLY (top 3 by RSSI per leaf)")
    print("─" * 80)
    for leaf, obs in zip(leaves, per_leaf_obs):
        top = sorted(obs.values(), key=lambda o: -o["rssi_avg"])[:3]
        print(f"  {leaf}")
        for o in top:
            d = rssi_to_distance_m(o["rssi_avg"])
            mfg = label_mfg(o.get("manuf", ""))
            name = (o.get("name") or "")[:22]
            print(f"    {o['mac']}  rssi={o['rssi_avg']:>+4d}  ≈{d:>5.1f}m  "
                  f"{mfg:9}  {name}")

    # ---- 7. manufacturer breakdown ----
    print()
    print("─" * 80)
    print("  MANUFACTURER BREAKDOWN (union)")
    print("─" * 80)
    mfg_counts: dict[str, int] = {}
    for mac in all_macs:
        info = next(d[mac] for d in per_leaf_obs if mac in d)
        m = label_mfg(info.get("manuf", ""))
        mfg_counts[m] = mfg_counts.get(m, 0) + 1
    for m, c in sorted(mfg_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {m:>14}  {c}")

    # ---- 8. named advertisers (with full RSSI vector) ----
    named = []
    for mac in all_macs:
        for d in per_leaf_obs:
            if mac in d and d[mac].get("name"):
                named.append((d[mac]["name"], mac))
                break
    if named:
        print()
        print("─" * 80)
        print("  NAMED ADVERTISERS — RSSI per leaf (· = not seen)")
        print("─" * 80)
        head = f"  {'name':32}  " + " ".join(f"{s[:6]:>6}" for s in short) + "   MAC"
        print(head)
        print("  " + "-" * (len(head) - 2))
        for name, mac in sorted(named, key=lambda x: x[0].lower()):
            cells = []
            for d in per_leaf_obs:
                if mac in d:
                    cells.append(f"{d[mac]['rssi_avg']:>+6d}")
                else:
                    cells.append(f"{'·':>6}")
            print(f"  {name[:32]:32}  " + " ".join(cells) + f"   {mac}")

    # ---- footer ----
    print()
    print("─" * 80)
    print(f"  Path-loss model: d = 10^((TxPower-RSSI)/(10·N))   "
          f"TxPower={TX_POWER_1M} dBm  N={PATH_LOSS_N}")
    print(f"  RELATIVE distance only. Real accuracy ±50% without per-device calibration.")
    print(f"  Spread ratios are more reliable than absolute distance because the unknown")
    print(f"  TxPower cancels out — '3.5× closer to A than B' is a meaningful claim.")


if __name__ == "__main__":
    main()
