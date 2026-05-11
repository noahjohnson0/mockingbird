#!/usr/bin/env python3
"""Compare two BLE-capture JSONs from mockingbird ESP32 leaves.

Answers:
  • Do the two scanners see the SAME devices, or different ones?
  • For overlapping devices, what's the RSSI difference?
  • Can we estimate physical distance from RSSI?

Usage:
  analyze_ble_capture.py <a.json> <b.json>
"""

from __future__ import annotations

import json
import math
import sys


# Log-distance path-loss model.
#   RSSI = TxPower - 10·n·log10(d)
#   d    = 10 ^ ((TxPower − RSSI) / (10·n))
#
# We don't know each device's true TxPower-at-1m calibration; iBeacons publish
# it, generic BLE advertisers don't. Assume TX_POWER_1M = -59 dBm (typical
# class-2 transmit power received at 1m line-of-sight) and PATH_LOSS_N = 2.5
# (typical indoor office; free space is 2.0, dense indoor 3.0+).
TX_POWER_1M = -59
PATH_LOSS_N = 2.5


def rssi_to_distance_m(rssi: int) -> float:
    return 10 ** ((TX_POWER_1M - rssi) / (10 * PATH_LOSS_N))


# Common Bluetooth SIG Company IDs we'll see in manufacturer-data prefix.
# https://www.bluetooth.com/specifications/assigned-numbers/company-identifiers/
MFG_ID = {
    "4c00": "Apple",
    "7500": "Samsung",
    "0006": "Microsoft",
    "00e0": "Google",
    "0500": "Bose",
    "0a00": "Sony",
    "01ff": "Logitech",
    "8700": "Garmin",
    "0700": "Belkin",
    "0d00": "Asus",
    "0227": "Tile",
    "0157": "Anhui Huami",  # Xiaomi/Mi Band
}


def label_mfg(manuf_hex: str) -> str:
    if not manuf_hex or len(manuf_hex) < 4:
        return "—"
    # First two bytes (4 hex chars), little-endian (BLE quirk: company ID is LE)
    return MFG_ID.get(manuf_hex[:4].lower(), f"id=0x{manuf_hex[:4]}")


def by_mac(obs_list):
    return {o["mac"]: o for o in obs_list}


def main():
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    a = json.load(open(sys.argv[1]))
    b = json.load(open(sys.argv[2]))

    a_host = a["hostname"]
    b_host = b["hostname"]
    a_obs = by_mac(a["observations"])
    b_obs = by_mac(b["observations"])

    a_macs = set(a_obs)
    b_macs = set(b_obs)
    both = a_macs & b_macs
    a_only = a_macs - b_macs
    b_only = b_macs - a_macs

    print("=" * 68)
    print(f"  {a_host:25} vs {b_host}")
    print("=" * 68)
    print(f"  scan window:        {a['scan_window_ms']/1000:>5.1f}s / "
          f"{b['scan_window_ms']/1000:.1f}s")
    print(f"  unique devices:     {len(a_macs):>5}    / {len(b_macs)}")
    print(f"  total observations: {a['n_total_obs']:>5}    / {b['n_total_obs']}")
    print(f"  packets/sec:        {a['n_total_obs']/(a['scan_window_ms']/1000):>5.1f}    "
          f"/ {b['n_total_obs']/(b['scan_window_ms']/1000):.1f}")
    print()
    print(f"  seen by BOTH:       {len(both):>5}     ({len(both)/len(a_macs|b_macs)*100:.0f}% of union)")
    print(f"  only by {a_host[-6:]}:    {len(a_only):>5}")
    print(f"  only by {b_host[-6:]}:    {len(b_only):>5}")

    # ---- Overlap: RSSI delta + distance estimate ----
    print()
    print("─" * 68)
    print("  OVERLAPPING DEVICES — RSSI compared, sorted by |Δ| (top 25)")
    print("─" * 68)
    print(f"  {'MAC':17}  {'A_rssi':>7} {'B_rssi':>7} {'Δ':>5}  "
          f"{'A_dist':>7} {'B_dist':>7}  {'mfg':>10}  name")
    rows = []
    for mac in both:
        ar = a_obs[mac]["rssi_avg"]
        br = b_obs[mac]["rssi_avg"]
        rows.append((abs(ar - br), mac, ar, br))
    rows.sort(reverse=True)
    for _, mac, ar, br in rows[:25]:
        ad = rssi_to_distance_m(ar)
        bd = rssi_to_distance_m(br)
        name = a_obs[mac].get("name") or b_obs[mac].get("name") or ""
        mfg = label_mfg(a_obs[mac].get("manuf", ""))
        print(f"  {mac}  {ar:>7d} {br:>7d} {br-ar:>+5d}  "
              f"{ad:>6.1f}m {bd:>6.1f}m  {mfg:>10}  {name[:24]}")

    # ---- Devices seen only by one scanner ----
    print()
    print("─" * 68)
    print("  DEVICES SEEN ONLY BY ONE SCANNER (top 10 each, by sample count)")
    print("─" * 68)
    for label, only, obs in [(a_host, a_only, a_obs), (b_host, b_only, b_obs)]:
        ranked = sorted(only, key=lambda m: -obs[m]["count"])[:10]
        print(f"  {label} only:")
        for mac in ranked:
            o = obs[mac]
            d = rssi_to_distance_m(o["rssi_avg"])
            mfg = label_mfg(o.get("manuf", ""))
            print(f"    {mac}  rssi_avg={o['rssi_avg']:>+4d}  count={o['count']:>4}  "
                  f"≈{d:>5.1f}m  {mfg:>10}  {o.get('name','')[:24]}")

    # ---- Manufacturer breakdown ----
    print()
    print("─" * 68)
    print("  MANUFACTURER BREAKDOWN (union)")
    print("─" * 68)
    mfg_counts = {}
    for mac in a_macs | b_macs:
        m = (a_obs.get(mac) or b_obs.get(mac))["manuf"]
        mfg_counts[label_mfg(m)] = mfg_counts.get(label_mfg(m), 0) + 1
    for m, c in sorted(mfg_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {m:>14}  {c}")

    # ---- Named devices ----
    print()
    print("─" * 68)
    print("  NAMED ADVERTISERS")
    print("─" * 68)
    named = {}
    for mac in a_macs | b_macs:
        name = (a_obs.get(mac, {}).get("name") or
                b_obs.get(mac, {}).get("name") or "")
        if name:
            named[mac] = (name, a_obs.get(mac), b_obs.get(mac))
    for mac, (name, ao, bo) in sorted(named.items(), key=lambda kv: kv[1][0]):
        a_str = f"A={ao['rssi_avg']:>+4d}" if ao else "  A=· "
        b_str = f"B={bo['rssi_avg']:>+4d}" if bo else "  B=· "
        print(f"  {name[:32]:32}  {a_str}  {b_str}  {mac}")

    # ---- Notes on the distance estimate ----
    print()
    print("─" * 68)
    print("  Distance estimate uses log-distance: d = 10^((TxPower-RSSI)/(10·N))")
    print(f"  with TxPower={TX_POWER_1M} dBm (calibration-at-1m) and N={PATH_LOSS_N}")
    print(f"  (indoor environment). Real accuracy is ±50%+ unless we know each")
    print(f"  device's actual TxPower. Use these as RELATIVE distance only.")


if __name__ == "__main__":
    main()
