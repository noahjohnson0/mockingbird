# Mockingbird — 2026 Q3 Technical Vision

**Author:** Anthony Zhang, CTO
**Horizon:** 90 days (Jul–Sep 2026)
**Status:** Approved

## 1. Thesis

Mockingbird is becoming the **ambient sensing substrate for a household** —
a cheap, always-on mesh that turns commodity BLE chatter and room-scale
RF geometry into a structured, queryable model of *who is home, where,
and what they're doing*. The radios already exist on every device a
person carries; we just listen, fuse, and resolve identity. Recent
commits (per-leaf TX/RX bias decomposition, robust MLE + entity
Kalman, 23 cm σ entity fusion) prove the geometry is real. Q3 is when
we cross from "we can localize a MAC" to "we can recognize a person."

## 2. Capability priorities

1. **Person fingerprinting via Apple Continuity sub-protocols (roadmap
   Phases 1–3).** Why this, why now: the streaming pipeline, entity
   fusion, and 8-leaf coverage are all in place. Identity is the
   unlock — every downstream product story (schedules, anomalies,
   automation) is gated on stable per-person resolution. Why before
   the others: it requires no hardware, and it's the demo that makes
   investors lean forward.
2. **Leaf positions + real trilateration.** Why now: we are leaving
   accuracy on the floor by reporting ratios instead of coordinates,
   and entity fusion already produces an `(x,y)` estimate that's
   wasted without a frame. Why before #3: identity quality
   (#1) compounds with positional quality — co-occurrence clustering
   gets tighter when distances are metric instead of ordinal.
3. **Live web dashboard on the Pi (`:8080`).** Why now: we are
   flying blind without it; every debug session is a Python one-liner.
   Why last of the three: it is a force-multiplier for #1 and #2, not
   a substitute. Builds it on top of finished primitives, not in
   parallel.

## 3. Hardware authorized this quarter

- **8× MPU-6050 IMUs (~$20 total).** One per deployed leaf. Unblocks
  orientation-aware RSSI normalization (estimated ±50% → ±20% on
  distance) and tamper detection. Cheapest experiment we will run
  all year per dB of accuracy gained.
- **1× ESP32-S3-DevKitC-1 with 8 MB PSRAM (~$15).** Evaluation only,
  not fleet rollout. Validates the `main/` MicroLink path so we know
  whether off-mesh deployments are real before Q4. One board, one
  engineer-day, decisive answer.
- **2× additional AITRIP WROOM-32 + USB-C power supplies.** Bring
  deployment to 10 leaves and harden two of them as paired
  specialists (per CLAUDE.md node patterns) for the rooms with the
  worst WiFi-vs-BLE duty-cycle contention.

**Not authorized:** LiPo cells, audio MEMS mics, PIR modules,
additional Opals. See "Do not do."

## 4. The risk that keeps me up

**Apple Continuity sub-protocol drift.** Our entire Phase-2 identity
story rides on a reverse-engineered manufacturer-data layout that
Apple can silently change in any iOS point release. If they rotate
the sub-protocol bytes the way they rotate MACs, our fingerprints
evaporate overnight.

**Mitigation:** (a) version-tag every parser output in the DB so we
can detect schema breaks the day they ship; (b) build identity as a
*pipeline of weak signals* (sub-protocol byte, advertising interval,
TX-power class, gait cadence from #2), not a single field — any one
can fail and the cluster still resolves; (c) keep the Phase-1
co-occurrence clusterer as a permanent fallback that needs no Apple
internals at all.

## 5. Do not do

- **Battery-powered leaves.** Wall power is everywhere we deploy.
  Defer until we have a customer who can't get to an outlet.
- **Audio leaf / MEMS mic classifier.** Different problem domain,
  different privacy story, different ML stack. Wrong quarter.
- **HomeKit / Home Assistant bridge.** Premature — we have no
  presence signal worth exposing yet. Revisit after #1 ships.
- **Fleet-wide ESP32-S3 migration.** One eval board only. The
  WROOM-32s are doing the job.
- **ESP-NOW multi-hop mesh.** UART pairing covers our out-of-range
  case; multi-hop is firmware complexity we have not earned.

— AZ
