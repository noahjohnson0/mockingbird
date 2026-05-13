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

## Open questions

- LSM6DSO vs MPU6050 — MPU6050 is dirt cheap and well-documented; LSM6DSO has better drift specs. **Owner: Puru, decide by 2026-05-20.**
- Sampling rate trade-off — 10 Hz is enough for orientation, but tamper detection responsiveness might want 25–50 Hz transient bursts. **Owner: Ethan, prototype by 2026-05-24.**
- Where does antenna gain-pattern data come from — manufacturer datasheet, anechoic chamber, or empirical from the deployed mesh? **Owner: Puru + Eszter, propose by 2026-05-22.**

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
