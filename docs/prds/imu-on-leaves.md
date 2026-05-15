# PRD: IMU on every leaf

**Status:** Draft
**Owner:** Puru (RF engineering) — primary; Ethan (firmware) co-owner
**Author:** Sophie Lefèvre
**Date:** 2026-05-13

## Problem

Recent fusion work brought entity localization to ~23 cm σ (sub-foot), but the residual error budget is dominated by **RSSI gain-pattern asymmetry**: a PCB-antenna ESP32 radiates up to 10 dB differently depending on orientation. Worse, we have no way to know when a leaf has been physically moved — a bumped or re-positioned leaf silently poisons every localization estimate downstream until someone notices the map "looks wrong." Both problems are solved by a $2 MPU6050 IMU per leaf. Without it, we are leaving accuracy on the table and shipping a system with no tamper detection. Ce n'est pas la mer à boire — six wires per leaf, a small firmware change, one new table.

## Goals

- Every leaf reports a 6-axis IMU stream alongside its BLE observations.
- Analyzer normalizes RSSI by leaf orientation before fusion.
- Operator gets an automatic alert when a leaf has moved beyond a configurable threshold.

## Non-goals

- IMU-based human activity classification (foot traffic, doors slamming) — defer to a separate "activity context" PRD.
- Per-board accelerometer calibration in a lab fixture — field calibration ("set leaf flat, press button") is sufficient.
- IMU on the existing 8 deployed leaves before they are physically retrieved — this is a forward-rollout feature; backport opportunistically.

## Users / personas

- **Noah (operator):** wants the map to stay accurate as cats, family members, or cleaning move leaves around.
- **Eszter / Wanjiru (analysts):** want a normalized RSSI input so downstream models stop fighting antenna physics.

## Success metrics

1. **Localization accuracy improvement:** entity-position σ drops from 23 cm to ≤ 18 cm on the same 5-minute test corpus, with IMU-normalized RSSI feeding the fuser. Target: 22% reduction in σ.
2. **Tamper detection latency:** a leaf moved by ≥ 0.5 m or rotated ≥ 15° is flagged within 60 seconds, with ≤ 1 false positive per leaf per week under normal household vibration.

## Requirements

1. **Hardware:** MPU6050 (or LSM6DSO) on I²C (SDA=21, SCL=22). BOM impact: ~$2/leaf + 4 dupont wires.
2. **Firmware:** new `imu` JSON line type on the existing TCP uplink: `{"event":"imu","ts":...,"ax":...,"ay":...,"az":...,"gx":...,"gy":...,"gz":...,"t_ms":...}`. Sample rate 10 Hz steady-state.
3. **NVS calibration:** firmware records zero-rate gyro offsets + a gravity reference vector from a 5s still period triggered by `POST /imu/calibrate`. Persisted across reboots.
4. **Collector:** new `imu` table `(ts, leaf, ax, ay, az, gx, gy, gz)` with index on `(leaf, ts)`.
5. **Analyzer:** RSSI normalization function `rssi_normalized = rssi + gain_correction(leaf_orientation, bearing_to_device)`. Antenna gain pattern as a per-board lookup; fall back to a default isotropic table if no per-board calibration exists.
6. **Tamper alert:** classifier rolling-window compares current orientation to NVS reference; emits `leaf_event` with `kind="tamper"` when threshold exceeded.

## Acceptance criteria (BDD)

**Scenario 1: Calibration anchors orientation**
- **Given** a freshly flashed leaf with no IMU calibration in NVS
- **When** the operator places the leaf flat and POSTs `/imu/calibrate`
- **Then** the leaf streams 5s of IMU samples, computes mean gyro bias and gravity vector, writes both to NVS, and responds `200 OK` with the recorded values
- **And** subsequent boots load the calibration from NVS without re-prompting

**Scenario 2: IMU stream lands in DB**
- **Given** a calibrated leaf is online and streaming
- **When** the collector receives `{"event":"imu",...}` lines
- **Then** rows appear in the `imu` table at ≥ 8 Hz per leaf
- **And** the `(leaf, ts)` index lets `SELECT ... WHERE leaf=? ORDER BY ts DESC LIMIT 1` return in ≤ 5 ms

**Scenario 3: Tamper detection**
- **Given** a leaf has been stationary (orientation matches NVS reference within ±5°) for ≥ 1 hour
- **When** the leaf is rotated by ≥ 15° or its accelerometer reports translation indicating ≥ 0.5 m displacement, sustained for ≥ 3 seconds
- **Then** a `leaf_event` row with `kind="tamper"` is written within 60 seconds of the motion ending
- **And** the leaf's position contribution is flagged `disabled=true` until an operator re-runs calibration

**Scenario 4: RSSI normalization tightens fusion**
- **Given** a 5-minute test capture with the same physical device walked through the same path, run twice (once with raw RSSI, once with IMU-normalized RSSI)
- **When** entity-position σ is computed for both runs
- **Then** the normalized run shows σ ≤ 18 cm (down from the raw-run baseline of ≈ 23 cm)

**Scenario 5: Graceful degradation**
- **Given** a leaf without an IMU attached (existing deployed leaves)
- **When** firmware boots and probes the I²C bus
- **Then** firmware logs `imu_absent=true`, streams no `imu` events, and BLE observations continue normally
- **And** the analyzer treats that leaf with the default isotropic gain table

## RF spec: orientation → antenna pattern correction (Puru, 2026-05-14)

### Antenna pattern model

The WROOM-32 ships an inverted-F PCB trace antenna on a corner of the module
(Espressif datasheet §1.5, "PCB antenna"). The measured 2.4 GHz radiation
pattern (Espressif "ESP32 Hardware Design Guidelines" Fig. 3-2; corroborated
by Espressif app note AN0501) is **not** isotropic:

- **H-plane (azimuth, module flat on table, looking down at the can):**
  near-omnidirectional within ±2 dB. The can-side is ~2 dB quieter than the
  trace-side (the can-shield reflects/absorbs back-lobe energy).
- **E-plane (elevation, rotating the module on its long axis):** a clean
  toroidal null along the antenna's electrical axis — empirically **−6 to
  −10 dB** at the boresight, recovering by ±60° off-axis.
- **Net peak-to-null:** **~10 dB**, with the deepest null pointing out the
  end of the module along the antenna's main radiating axis.

Define a body frame on the WROOM-32 module:
- **x̂_b**: long axis of the PCB, pointing AWAY from the antenna end (i.e.
  antenna at −x̂_b).
- **ẑ_b**: normal to the PCB, pointing out the trace side (away from the
  can).
- **ŷ_b**: right-handed completion (along the short axis of the PCB).

The dominant null is along **−x̂_b** (out the antenna end); the peak gain
direction is the +ẑ_b hemisphere centered on broadside.

### Correction function

We model the leaf's TX→RX link as:

    rssi_obs = P0 − 10·n·log10(d) + rx_bias_leaf + G_ant(θ, φ) + ε

where (θ, φ) are the elevation/azimuth of the BLE device's bearing
**expressed in the leaf's body frame**, and `G_ant` is the per-board
antenna pattern in dB (max ≈ 0, min ≈ −10).

`rssi_normalized = rssi_obs − G_ant(θ, φ)` — i.e. we *subtract* the
pattern so what feeds the path-loss inverter behaves like an isotropic
radiator. This drops in cleanly upstream of `estimate_distance()` in
`services/mockingbird_calibration.py:479` (which already subtracts
`rx_bias_leaf` before inverting — same algebra, one extra term).

**Pattern representation (v1):** a 2-D lookup `G_ant[θ_bin, φ_bin]` with
15° bins (12×24 = 288 cells, < 2 KB), bilinearly interpolated. Initial
table is the **default isotropic-plus-end-null** analytic approximation:

    G_ant(θ, φ) ≈ −10·cos²(α)·𝟙[α < 60°]

where α is the angle between the device bearing and **−x̂_b** (the null
axis). This captures the dominant feature (the end-fire null) without
needing chamber data and matches measured WROOM-32 patterns to within
~2 dB across most of the sphere.

**Per-board refinement (v2, deferred):** empirical fit from the mesh
itself (see "Where does gain-pattern data come from", below).

### Gravity-vector → bearing flow

The MPU6050 gives us **acceleration in the body frame**. At rest, that
vector is the gravity vector expressed in body coords: `g_b = (gx, gy,
gz)`. Pipeline:

1. **Calibration capture** (per leaf, once at install): operator places
   the leaf in its final orientation, presses calibrate. Firmware
   averages 5 s of MPU6050 samples → `g_b_ref` (the leaf's "down" in
   body coords) and gyro biases. Stored in NVS *and* sent to the
   collector in the existing `hello` event as new fields
   `g_b_ref=[gx,gy,gz]`. Persisted on the Pi in a new `leaf_orientation`
   table `(leaf, ts, gx_ref, gy_ref, gz_ref, R_b2w_json)`.
2. **World-frame rotation matrix** `R_b2w`: built on the Pi from
   `g_b_ref`. We know world-`ẑ_w` = up = `−ĝ_b_ref / |ĝ_b_ref|` expressed
   in the leaf frame, and we accept yaw ambiguity (no magnetometer on
   MPU6050). Yaw is resolved by a *secondary* constraint: the leaf's
   measured RSSI vs. several known-position calibration beacons during
   the existing `mockingbird_calibration` pass. The leaf-yaw that
   minimizes residuals against `G_ant(θ, φ)` becomes the leaf's stored
   yaw. This is Eszter's hook: yaw is a 6th free parameter in the
   per-leaf bias fit (`_fit_leaf_biases`), gridded at 15° and refined.
3. **Per-observation bearing** (online, in fuser): for every BLE
   observation, the collector knows (a) the leaf's position in world
   frame (from `leaf_positions`), (b) the entity's current estimated
   position (from the previous Kalman state), (c) the leaf's R_b2w.
   Compute device bearing in world frame, rotate into leaf body frame,
   convert to (θ, φ), look up `G_ant`, subtract.
4. **Bootstrap on first observation** (no prior entity estimate): apply
   the *mean* of `G_ant` over all bearings (a scalar ≈ −3 dB) so the
   first fix isn't biased toward leaves whose null happens to point at
   the device. The Kalman fuser converges within 2–3 obs and subsequent
   bearings use the proper directional correction.
5. **Tilt-aware update** (online, low rate): if `g_b` drifts > 5° from
   `g_b_ref` for ≥ 30 s without crossing the tamper threshold, update
   R_b2w in place (a leaf settling on its mount). Above the tamper
   threshold we *disable* the leaf — covered in Scenario 3.

### Where in the code

- New module: `services/mockingbird_antenna.py` with `G_ant_lookup(theta,
  phi, leaf)`, `build_R_b2w(g_b_ref)`, `bearing_in_body_frame(leaf_pos,
  entity_pos, R_b2w)`.
- Wire into `estimate_distance()` (line 479) via an optional
  `bearing_to_device` arg; when present, subtract `G_ant` before the
  bias subtraction. `mle_multilaterate()` and `multilaterate()` pass
  bearing through using the iterate's current position estimate.
- Per-leaf yaw stored in `CalibrationParams` as a new field
  `yaw_deg: dict[str, float]`.

### Test fixtures

1. **Rotation stage (low-tech) — required for v1 acceptance.** A
   3D-printed protractor base with 15° detents and a single MPU6050-
   equipped leaf clamped on top, ~3 m from a fixed BLE beacon (one of
   the existing leaves running in beacon mode at fixed TX power). Spin
   through 0°/15°/.../345° azimuth × 5 elevation tilts (0°, ±30°,
   ±60°), 30 s dwell per pose, log RSSI + IMU. Output: empirical
   `G_ant` for one board, validation of the analytic model. **Bom:**
   one print, an hour of bench time. Puru owns the print STL.
2. **Known-position beacons — already have, repurpose.** The existing
   calibration anchor flow (operator-placed devices at marked
   positions) gives us (leaf, device, true_d) tuples. Adding "device
   bearing in leaf body frame" is free once R_b2w is known.
3. **Field walking corpus — required for Scenario 4 acceptance.** The
   same 5-minute walked-path capture used to baseline 23 cm σ, run
   twice on the same recorded raw stream — once through the current
   pipeline, once with `G_ant` enabled. Apples-to-apples, no second
   recording needed; this is a *replay* test, which Andy's
   `test_calibration_*` harness already supports.
4. **Anechoic chamber — explicitly NOT required.** Espressif's
   published patterns plus the rotation stage cover us to ~2 dB, which
   is below the 18 cm σ target's RF budget. We can revisit if v1 misses.

### Open questions (resolved by Puru, 2026-05-14)

- **LSM6DSO vs MPU6050 — pick MPU6050.** Drift advantage of LSM6DSO is
  irrelevant for our use case: we're not dead-reckoning, we're using
  gravity (which is observable from the accelerometer alone, no
  gyro-integration drift involved). MPU6050 wins on price (3×
  cheaper), driver maturity, and unit availability. Resolved.
- **Sampling rate — 10 Hz steady-state, 50 Hz burst on tamper trigger.**
  Confirmed sufficient with Ethan: at rest, 10 Hz of accel-only is
  fine for gravity; the moment the magnitude of `(a − g_b_ref)` exceeds
  0.15 g, firmware promotes to 50 Hz for 5 s to capture the motion
  transient cleanly. Cost on the wire is negligible (≤ 250 extra lines
  per tamper event). Resolved — Ethan to implement in firmware.
- **Antenna gain-pattern data source — analytic-default + per-board
  empirical refinement.** v1 ships the analytic `−10·cos²(α)` table
  (no measurement required). v2, after we have ≥ 1 hour of mesh data
  per leaf, the per-board fit falls out of the same residual-
  minimization Eszter already runs in `_fit_leaf_biases` — extend it
  to include the (θ, φ) bin as a free parameter. Anechoic chamber is
  off the table (overkill, no access). Resolved.

## Firmware spec: I²C, sample budget, wire format, heap (Ethan, 2026-05-14)

Signing off the firmware side for the 2026-05-20 spec-lock. Four areas I own:

### 1. I²C bus — SDA=21, SCL=22 on single-chip generalists; SDA=18, SCL=19 fallback

GPIO 21 and 22 are the Arduino-default I²C pins and have **no strapping or
flash-mux conflict** on the WROOM-32. Strapping pins on this module are 0, 2,
5, 12, 15 — none overlap. They're currently unused by
`firmware/esp32-wroom-mockingbird/src/main.cpp`.

Pull-ups: the MPU6050 breakouts in the AITRIP-style packs ship with 4.7 kΩ
pull-ups already populated. **Do not** add board-side pull-ups or we land at
~2.3 kΩ effective and the bus rings at 400 kHz. Verify with a multimeter
between SDA/SCL and 3V3 on the first breakout before populating the rest.

Paired-specialist nodes: GPIO 21/22 may already be wired to the UART link
between the BLE-only and WiFi-only halves of the pair. If so, fall back to
**SDA=18, SCL=19** — also free, also non-strapping. Bring this up with Vlad
before wiring the paired specialists; on the 8 single-chip generalists
currently deployed, 21/22 is the right default.

### 2. Sample rate budget — 10 Hz steady, 50 Hz burst, no impact on BLE duty cycle

Plan, driven by the MPU6050's internal FIFO + DLPF:

- **Steady-state:** 10 Hz, FIFO-backed, drained on a 100 ms FreeRTOS timer.
  One drain is ~14 bytes over I²C at 400 kHz ≈ 350 µs. Negligible vs the
  BLE callback path.
- **Tamper-triggered burst:** when the rolling-window accelerometer norm
  leaves the steady band, the drain interval drops to 20 ms (50 Hz) for
  3 s, then decays back. The detector runs on-device; the analyzer only
  sees the resulting `kind="tamper"` event and the 50 Hz samples that
  accompany it.

The non-trivial concern is the **2.4 GHz single-radio coex ceiling**, not
RAM. The uplink task and BLE callback already contend for the radio;
adding I²C work doesn't add radio load, but the timer ISR can preempt the
NimBLE scan callback. **Mitigation:** pin the IMU drain task to **core 0**
(NimBLE's core) so it serializes naturally with the scan callback rather
than racing it across cores. Uplink task stays on core 1 as today.

100 Hz starts to fight NimBLE's scan callback on core 0 in bench runs of
similar shape from other projects. If Puru's `R_b2w` filter wants higher
than 50 Hz, we either move IMU to a dedicated FreeRTOS task on core 1
with explicit yields (more LOC, marginal risk of starving the uplink) or
cap at 50 Hz. **Decision deferred to a bench measurement on the first
retrofit leaf, 2026-05-24.**

### 3. Wire format — additive `imu` line, batched, leaf-tagged

Endorse the PRD's `{"event":"imu", ...}` shape with three refinements:

- Add `"leaf":"<hostname>"` so the collector can demux without sniffing
  the TCP peer. Matches existing `obs` and `hb` events.
- **Batch up to 5 samples per line:**
  `{"event":"imu","leaf":"...","t_ms":..., "samples":[[ax,ay,az,gx,gy,gz], ...]}`.
  At 10 Hz × 8 leaves = 80 sample-events/s across the fleet. One line per
  sample doubles the collector's parse cost for marginal benefit; batched
  at 5 it's 16 lines/s. Cost on the leaf is a 5-slot ring buffer
  (~120 B); benefit on the Pi is real.
- **Burst samples** (50 Hz, post-trigger) ship in the same `samples`
  array with `"burst":true` so the analyzer can apply a different
  smoothing window without inferring it from rate.

Storing in the DB: the PRD's proposed `imu` table is fine, but I'd
**unroll the batches at ingest** (one row per sample) so downstream
queries don't have to parse JSON. Index on `(leaf, ts)`; storage cost
is ~64 B/row × 80 rows/s × 86400 s/day = ~440 MB/day — too much for the
Pi's SD card without TTL. **Recommend a 14-day TTL** on the `imu` table
via a nightly `DELETE WHERE ts < strftime('%s','now','-14 days')` in the
collector's existing maintenance path. Calibration data lives in NVS on
the leaf anyway; the DB is for residual debugging only.

### 4. Heap impact — ~1.4 KB static, zero new heap allocations in steady state

Per-leaf budget:
- I²C driver static buffers: ~1.2 KB
- FIFO drain buffer (12 samples × 7 bytes): 84 B
- Tamper rolling window state: ~96 B
- New `g_imu_q` of 16 slots × 28 B: **448 B**

**Total: ~1.8 KB additional static, zero additional heap allocations in
steady state.** We sit at 110–125 KB free under load today; post-IMU
budget is ~108–123 KB. Well inside our floor.

On the IMU queue: not sharing the existing 64-entry `Msg` queue. `Msg`
is BLE-shaped (96 B/slot for `mac`/`rssi`/`name`/`manuf`), an IMU
sample is 28 B raw — sharing wastes 68 B/slot and tangles the uplink
send path. Separate `g_imu_q`, drained by the same uplink task with
two `xQueueReceive` calls per loop iteration. ~30 LOC delta in
`uplink_task()`.

### 5. Graceful degradation — already in PRD Scenario 5, here's how

I²C probe at boot: `Wire.beginTransmission(0x68); Wire.endTransmission();`
— ACK means MPU6050 present, NACK means absent. On NACK, set a global
`g_imu_present = false`, skip the drain timer setup entirely, include
`"imu_absent":true` in the hello line. **No timer task spun up if no
IMU.** This way the 8 currently-deployed leaves run the new firmware
unchanged in behavior until they're physically retrofitted.

## Open questions (remaining for Sophie, 2026-05-18 PRD review)

- **Yaw bootstrap accuracy.** The analytic null model is symmetric about
  −x̂_b, so yaw is identifiable from RSSI residuals only when at least
  one calibration beacon falls within ±60° of the null. With 8 leaves
  and the current beacon-placement convention, every leaf should see at
  least one such beacon — but I'd like Sophie to confirm the planned
  6/2 single-chip/paired-specialist layout doesn't isolate a leaf with
  its null pointing at a wall corner where no beacons live. **Mitigation
  if it does:** require operator to place one calibration device along
  the leaf's −x̂_b axis at install time.
- **Schema decision — bake `bearing_deg` into the `obs` table, or
  compute it online in the fuser?** Storing it (one extra REAL column,
  ~5% DB growth) makes replay deterministic and lets us re-derive
  `G_ant` post-hoc when v2 lands. Computing on the fly keeps the schema
  small. Recommend: **store it.** Replay-determinism is worth 5%.
  **Sophie to ratify.**
- **Tamper interaction with R_b2w drift update.** Scenario 3 disables a
  leaf on rotation ≥ 15°. The "tilt-aware update" path (step 5 above)
  also rotates by up to 5° silently. Need to confirm the bands don't
  overlap badly (5° silent + 15° threshold = 10° dead-band where
  *neither* happens but the leaf has clearly shifted). **Recommend
  narrowing the silent band to ±2° and surfacing 2–15° drift as a
  `kind="drift"` event** — operator-visible but doesn't disable the
  leaf. Sophie to call.

## Timeline & owners

- **Hardware order (10× MPU6050 + headers):** 2026-05-15 — Sophie places order
- **Spec lock:** 2026-05-20 — Sophie
- **Firmware IMU stream + calibration:** 2026-05-29 — Ethan
- **Collector schema + ingest:** 2026-05-31 — Ethan
- **Analyzer gain-correction:** 2026-06-07 — Eszter
- **Tamper detection:** 2026-06-10 — Wanjiru
- **First leaf retrofit + acceptance run:** 2026-06-14
- **Fleet rollout:** opportunistic as leaves are pulled for case-printing

## Rough estimate

**T-shirt: M.** Reasoning: scope is well-bounded — one new sensor with a well-known driver, additive firmware (no architectural changes), additive schema, additive analyzer function. The only true unknown is the gain-pattern data source; that risk is M-sized at worst. Two engineers, ~3 calendar weeks. Hardware lead time is the schedule risk, not engineering — on ne fait pas d'omelette sans casser des œufs, order the parts first.
