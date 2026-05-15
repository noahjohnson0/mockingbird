# Fleet firmware audit — 2026-05-14

Auditor: Dr. Vlad Moskovavich
Source of truth: live `GET /` and `GET /version` on each leaf's HTTP API (port 80).
Mac was on `entropy` subnet (192.168.0.241), reaching `192.168.8.0/24` via
the Pi subnet router over tailnet.

## TL;DR

- `platformio.ini` pin: **`v0.3.1-stream`**
- **Two leaves observed**, both running newer firmware than the pin:
  - `mockingbird-4ce184` → `0.5.0-q64`
  - `mockingbird-4c0bdc` → `0.6.4-slow-adv`
- **Six leaves could not be confirmed in this session** due to a Pi-side
  subnet-router outage (see "Why six leaves are blank" below). This is
  **not** evidence that they are offline — it is evidence that I cannot
  see them from where I'm sitting right now.

## Per-leaf table

| Hostname | MAC | Last-seen IP (CLAUDE.md) | Reachable? | Version | Uptime (s) | Free heap (B) | uplink sent | uplink dropped | q_depth | RSSI | Location string |
|---|---|---|---|---|---|---|---|---|---|---|---|
| mockingbird-4ce184 | 8c:94:df:4c:e1:84 | 192.168.8.244 | YES | `0.5.0-q64` | 192,648 (~2d 5h) | 118,192 | 2,029,167 | **8,802,771** | 31 | -53 | "Noah room test rack" |
| mockingbird-4c0bdc | 8c:94:df:4c:0b:dc | 192.168.8.196 | YES | `0.6.4-slow-adv` | 195,119 (~2d 6h) | 2,674,928 | **6,959,155** | **255 (max)** | -48 | "noahs bedroom plant rack" |
| mockingbird-4c36ec | 8c:94:df:4c:36:ec | 192.168.8.219 | NO | — | — | — | — | — | — | — | — |
| mockingbird-4db204 | 8c:94:df:4d:b2:04 | 192.168.8.168 | NO | — | — | — | — | — | — | — | — |
| mockingbird-4ce1d8 | 8c:94:df:4c:e1:d8 | 192.168.8.178 | NO | — | — | — | — | — | — | — | — |
| mockingbird-4ceb7c | 8c:94:df:4c:eb:7c | 192.168.8.126 | NO | — | — | — | — | — | — | — | — |
| mockingbird-92838c | 30:76:f5:92:83:8c | 192.168.8.204 | NO | — | — | — | — | — | — | — | — |
| mockingbird-4d4384 | 8c:94:df:4d:43:84 | 192.168.8.107 | NO | — | — | — | — | — | — | — | — |

## Drift vs `platformio.ini` pin (`v0.3.1-stream`)

| Leaf | On-device | Pin | Verdict |
|---|---|---|---|
| 4ce184 | `0.5.0-q64` | `0.3.1-stream` | **NEWER than pin** |
| 4c0bdc | `0.6.4-slow-adv` | `0.3.1-stream` | **NEWER than pin** |

Both observed units are running OTA images that are ahead of the pin. The
suffixes (`-q64`, `-slow-adv`) suggest experimental branches:

- `0.5.0-q64` → likely the parameterized observation-queue work (matches
  the CLAUDE.md note about commit `8e8a451`). `q_depth=31` at the moment
  of probe is consistent with a 64-slot queue under steady load.
- `0.6.4-slow-adv` → version number jump suggests this rolled in the
  tier-1 calibration work (commit `7992962`) plus slower-advertisement
  tuning, but the version string alone does not prove it. **Confirm with
  Ethan** that this is in fact the calibration build.

So: the `platformio.ini` pin is stale by at least two minor releases for
the units I could see. The pin should be moved forward or the audit
extended to all eight.

## Why six leaves are blank — and why this is not the leaves' fault

The Mac → leaves path runs:

```
Mac (entropy 192.168.0.241)
  → tailnet (DERP relay "lhr", London) 
    → mockingbird-pi (100.73.232.63, advertising 192.168.8.0/24)
      → Mockingbird WiFi → ESP32 leaf
```

Observations during the audit:

- `tailscale ping mockingbird-pi` → **no reply, 3× timeout**.
- `tailscale status` shows Pi as "active" but path is `relay "lhr"` — i.e.
  the direct UDP NAT-punch path is dead and only the DERP fallback (LHR,
  ~80 ms baseline RTT, no QoS, lossy under bursts) is alive.
- Direct ICMP to `192.168.8.202` (Pi LAN IP) → 100% loss.
- Direct ICMP to `192.168.8.1` (Opal) via the same subnet route → 100% loss.
- HTTP to Pi services on `:8080` and `:9001` → connection timeout.
- HTTP to the two leaves only succeeded **in the first burst of the audit**,
  then failed on every subsequent retry — characteristic of a flapping
  DERP path, not of leaves dying simultaneously in the second between probes.

This pattern is the exact signature of the "Pi Zero W on Bookworm/Trixie
can silently lose WiFi" gotcha documented in `CLAUDE.md`. The
control-plane state stays "active" in the netmap because `tailscaled` on
the Pi is up and authenticated; the BCM43430 wlan0 driver wedges
beneath it. Even the leaves that *did* answer fell silent within ~60 s,
which is consistent with the Pi flapping rather than six leaves
independently choosing to be unreachable.

**Therefore: the six blank rows are an artifact of the audit station's
network position right now, not a fleet-firmware finding.** Treat as
inconclusive until re-run from a Mac on Mockingbird directly, OR after
the Pi is power-cycled and the watchdog deploy from
`scripts/deploy-pi-wlan-fix.sh` is confirmed live.

## Anomalies worth flagging (the RF/wave instinct part)

1. **Both reachable leaves show absurd `dropped` counters relative to
   `sent`** — 4.3× and 2.6× more dropped than sent. This is
   uplink-side, i.e. the TCP queue to `mockingbird-pi:9001` is shedding
   observations because the collector or its path can't drain them.
   `mockingbird-4c0bdc` is sitting at `q_depth=255`, the max queue depth
   — every new observation past that is being thrown away. This is
   consistent with the Pi being intermittently wedged: the TCP socket
   stays nominally open, leaf keeps trying to send, Pi never ACKs, queue
   pegs, drops climb. **It is also a calibration-data hygiene problem**:
   if dropped > sent, the dataset feeding `mockingbird_calibration.py`
   and `mockingbird_tracks.py` is sampling with non-stationary loss
   patterns. Any RSSI-distribution-based bias decomposition done from
   data captured during these windows will be biased toward whatever
   conditions correlate with successful delivery (likely toward
   stronger-RSSI / closer-to-AP advertisers — survivorship bias in the
   most literal RF sense).

2. **RSSI values look healthy** for the two reachable leaves: -53 dBm
   and -48 dBm uplink RSSI to the Opal AP. These are strong link-budget
   numbers (~30 dB above the typical -85 dBm WiFi noise floor at 2.4
   GHz). The uplink problem is *not* RF — it is Pi-side. Important to
   record so we don't go chasing antenna patterns when the bug is in
   the BCM43430 driver.

3. **`mockingbird-4c0bdc` free heap is 74 KB**, vs `mockingbird-4ce184`
   at 118 KB. Both are well clear of OOM but the spread is large for
   nominally identical hardware running similar firmware. Possible
   causes (in order of likelihood): (a) `0.6.4-slow-adv` allocates more
   per-MAC state for calibration; (b) `q_depth=255` itself is holding
   ~255 observations in RAM (~30-50 bytes each → 8-13 KB, not enough
   to explain the gap); (c) larger observed device population at the
   plant rack location (more advertisers in range → more transient
   alloc churn). Not concerning yet, but worth a heap probe under
   sustained load before declaring `0.6.4` flightworthy on every leaf.

## Recommended next steps

1. **Power-cycle the Pi** (or wait for the wlan0 watchdog to bounce it
   per `services/pi/`), then re-run this audit from a Mac on Mockingbird
   directly to avoid the tailnet/DERP variable entirely.
2. **Coordinate with Ethan** on:
   - Confirming `0.6.4-slow-adv` ⇔ commit `7992962` (tier-1 calibration).
   - Confirming `0.5.0-q64` ⇔ commit `8e8a451` (parameterized obs-queue).
   - Whether the six blank leaves should be on the same image or are
     intentionally pinned to older builds for A/B testing.
3. **Move the `platformio.ini` pin forward** to whatever is now canonical
   so future fresh flashes don't regress live leaves.
4. **Fix the drop-rate problem before trusting the data**: until the Pi
   stops wedging, the `observations.sqlite` content has non-stationary
   loss and any statistical inference on top of it (bias decomposition,
   Kalman process-noise tuning, motion classifier training) is suspect
   for the affected time windows.
