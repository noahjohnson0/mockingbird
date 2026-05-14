# Mockingbird roadmap

Living list of planned capabilities, in no particular priority. Move items
to "in flight" when started, to "shipped" once landed in `main`, and to
"deferred" if explicitly chosen not to pursue.

## In flight

### Person fingerprinting — Phases 1 & 2 (Q3)
PRD: `docs/prds/person-fingerprinting-phase-1-2.md`. Owner: Wanjiru.
Spec-lock 2026-05-20, ship 2026-06-14. Co-occurrence clustering +
Apple Continuity sub-protocol fingerprints for stable identity across
MAC rotation. Phases 3-6 remain in Planned below.

### IMU on every leaf (Q3)
PRD: `docs/prds/imu-on-leaves.md`. Owners: Puru (RF) + Ethan (FW).
Spec-lock 2026-05-20, first leaf retrofit 2026-06-14. See Planned
section below for the original sketch.

### Presence & anomaly alerts (Q3)
PRD: `docs/prds/presence-anomaly-alerts.md`. Owner: Ethan.
Spec-lock 2026-05-20, trial begins 2026-06-17. Depends on the two
PRDs above (rules 1+2 on person fingerprinting, rule 3 on IMU).

## Planned

### Person fingerprinting + schedule mining (Phases 3-6)
Phases 1 & 2 are now in flight (see PRD above). Remaining phases:

**Phase 3 — Person identity (you tag the cluster).**
A small `persons` table in the DB plus a one-time CLI prompt:
"This cluster is usually here Mon-Fri 9-5 with RSSI -50/-70/-80/-50.
Call it: ___?" You type `noah`. Future appearances of the cluster
fingerprint resolve to `noah`. Self-corrects over time when you flag
mislabels.

**Phase 4 — Walking speed + trajectory (needs leaf positions).**
Per-device timeline:
```
  t=0   leaf-bedroom    RSSI -45  ← peak (closest)
  t=2s  leaf-livingroom RSSI -45  ← peak (handoff)
  t=4s  leaf-kitchen    RSSI -50  ← still moving
```
With known `(x,y,z)` for each leaf, derive a position over time. Speed =
position derivative. Typical walking pace 1.4 m/s; jog 3 m/s; standing
still ≪0.1 m/s. Gait *fingerprint* per person: dominant frequency
(steps/sec), typical speed, acceleration profile. Distinguishes Noah's
walk from his housemate's walk.

**Phase 5 — Schedule discovery.**
Per-person appearance timeline → hour-of-day + day-of-week histograms
→ k-means or DBSCAN over the heatmap → discrete schedule clusters
(e.g. "wakes 7-8 AM weekdays, leaves the house 9-10, returns 6-7 PM").
Trivial Python sklearn or even just numpy. Output as a calendar-ish
visualization.

**Phase 6 — Anomaly detection.**
Once schedules are stable, flag deviations: "Noah usually leaves by
9 AM Mon-Fri, hasn't moved today" → push to phone (via Home Assistant
or pushover). "Stranger fingerprint appeared at 3 AM with no prior
visits" → alert.

**What's needed in the platform**:
- New DB tables: `person`, `device_cluster`, `cluster_member`,
  `fingerprint_event`, `person_appearance`
- A `services/mockingbird-classifier.py` daemon that tails the obs
  stream and updates clusters / appearances in real time
- Periodic retraining cron (nightly) that re-clusters with the
  accumulated data
- HTTP API for the analyzer / dashboard to query "who is currently
  here" and "schedule for X person"

**Honest about hard parts**:
- Person ambiguity when multiple people in the same room — gadget
  clusters merge in RSSI space. Disambiguates over time as they
  separate.
- Visitors with no prior fingerprint get tagged as "unknown #1, #2"
  until you label them.
- Apple Continuity is well-understood but Samsung/Microsoft/Google
  use different patterns — separate parsers per ecosystem.
- Privacy implications are real. This is *household-level* —
  obviously don't expose any of this past the tailnet.

### IMU (accelerometer + gyroscope) on each leaf — see in-flight PRD above
Original sketch retained for context; productionization is tracked in
`docs/prds/imu-on-leaves.md`. Three concrete payoffs:

1. **Orientation-aware RSSI.** PCB-antenna ESP32 radios have a non-uniform
   gain pattern — RSSI varies up to ~10 dB depending on which way the
   board is pointing. Knowing roll/pitch/yaw lets the analyzer normalize
   readings, which makes the spread-ratio distance estimate meaningfully
   tighter (could close the gap from ±50 % to maybe ±20 %).
2. **Tamper / displacement detection.** If a leaf is moved by more than
   a threshold (say 0.5 m or 15° of rotation), the analyzer flags it
   as "spatial fingerprint invalid" until re-calibrated. Stops one
   re-arranged leaf from poisoning the entire mesh's localization.
3. **Activity context.** Cross-fuse BLE proximity with leaf accelerometer
   bursts: "this AirPod's RSSI rose AND the desk leaf got jostled" =
   someone sat down. Multimodal events.

Implementation sketch:
- Wire MPU6050 to ESP32's I²C pins (SDA=21, SCL=22 on WROOM-32) — share
  the bus across all sensors on the board.
- Firmware adds a `gyro` block to `/status` and a periodic IMU sample
  stream alongside the BLE obs stream (same TCP uplink, new
  `{"event":"imu", ...}` line type).
- Collector grows an `imu` table: `(ts, leaf, ax, ay, az, gx, gy, gz, t_ms)`.
- Calibration step on first install: leaf does a 5 s still period, records
  zero-rate offsets to NVS. Then a known orientation push (e.g. "tap top")
  to anchor the gravity vector.
- Power: MPU6050 draws ~4 mA active, much less in sleep — negligible vs
  ESP32's ~80 mA at WiFi+BLE-active.

### ESP-NOW peer mesh for out-of-WiFi-range leaves
The Paired Specialist pattern in CLAUDE.md uses UART to bridge a BLE-only
leaf to a WiFi-uplink leaf. Alternative: pair them over ESP-NOW (no WiFi
config needed, no infrastructure). Useful for outdoor or
unusually-shielded spots that can't reach the Opal directly.

### ESP32-S3 firmware path
The `main/` directory already has an ESP-IDF + MicroLink design for ESP32-S3
boards with PSRAM. If Noah ever buys an S3, each leaf could run its own
Tailscale identity instead of relying on the Pi as a subnet router —
useful if a leaf gets deployed somewhere that can't reach Mockingbird WiFi
(e.g. at another house with its own LAN).

### Battery-powered leaves
For deployments that can't tape a USB power cable to a wall socket. Need:
- Larger flash for deeper sleep states (deepsleep + BLE-only periodic wake)
- LiPo + charging circuit (TP4056 module + boost)
- Lower duty cycle in firmware: scan 5 s every minute, sleep otherwise
- Status reporting becomes "battery_pct" alongside heap/RSSI

### Other capabilities (sketch)
- **Audio leaf**: I²S MEMS mic, FFT on ESP32, classifier — picks up
  doorbells, alarms, broken glass.
- **PIR-augmented leaf**: cheap PIR sensor as a high-confidence "human
  here" signal, calibrate BLE RSSI against it.
- **HomeKit / HomeAssistant bridge** on the Pi exposing observations as
  presence/proximity entities.

## In flight (Q3 PRDs)
- Person fingerprinting Phases 1 & 2 — `docs/prds/person-fingerprinting-phase-1-2.md`
- IMU on every leaf — `docs/prds/imu-on-leaves.md`
- Presence & anomaly alerts — `docs/prds/presence-anomaly-alerts.md`
- Spec-lock for all three: **2026-05-20**. Ship dates per individual PRD timelines.
- Leaf-case 3D-print backlog: 2/8 leaves cased; remaining cases printing
  opportunistically as leaves are pulled for IMU retrofit.

## Shipped
- v0.1: WiFi + ArduinoOTA + HTTP control (first OTA-able firmware)
- v0.2.x: On-device BLE scan accumulator with `/scan/reset` + `/scan/result`
- v0.3.0: Streaming architecture — leaves push obs to Pi collector → SQLite
- v0.3.1: NVS-persisted location label per leaf, non-blocking TCP write
- Pi-side: Tailscale subnet router for `192.168.8.0/24`,
  mockingbird-collector systemd service ingesting from `:9001`
- Analyzer: SQLite-backed query for any time window with coverage
  matrix, spread leaders, churn, manufacturer/named breakdowns
- **Leaf positions → real trilateration:** MLE multilateration shipped,
  with per-leaf TX/RX bias decomposition + path-loss solver
  (`services/mockingbird_calibration.py`). Auto-calibration live.
- **Live web dashboard:** Three.js UI on `:8080`
  (`services/dashboard.html` / `mockingbird-dashboard.service`) —
  heatmap, trails, click-to-select, bird codenames, north compass,
  debounced auto-save.
- **Kalman fusion + entity clustering:** Joseph-form Kalman,
  Tikhonov-regularized solves, multi-MAC tracks survive Apple
  Continuity rotation (`services/mockingbird_tracks.py`). Entity-position
  σ ≈ 23 cm; centroid jitter reduced 300 cm → 18 cm.
- **Adaptive ZUPT + velocity clamp** (kills phantom motion).
- **Phone auto-detection** via 4-leaf walk.
- **Wire-format contract tests + canary** (`tests/test_wire_contract.py`).

## Deferred
- (none right now)
