# Q3 Ticket Backlog

**Curated by:** Sophie Lefèvre — 2026-05-13
**Source PRDs:** [person-fingerprinting-phase-1-2](person-fingerprinting-phase-1-2.md), [imu-on-leaves](imu-on-leaves.md), [presence-anomaly-alerts](presence-anomaly-alerts.md)
**Branch context:** shipped work referenced below lives on `agents/integration-2026-05-13`.

---

## Executive summary

This quarter we ship three capabilities, in this order: **(1) Person Fingerprinting → (2) IMU on leaves (parallel) → (3) Presence & anomaly alerts**. Fingerprinting and IMU are independent engineering tracks; alerts is the thin layer that sits on top of both and is what the household member actually feels. The mathematical, operational, and wire-format foundations the team shipped in late April/early May (Kalman stabilization, sqlite pool fix, systemd hardening, BLE channel plumbing, wire-format contract tests) mean we are not starting from a hot stove — we are starting from a clean *mise en place*.

**Critical path: `FP-1` (Apple Continuity parser).** Until that parser exists, fingerprinting Phase 2 cannot start, anonymous person buckets stay anonymous across MAC rotations, and three of the four alert rules have no signal to fire on. Everything else can wait a week. This one cannot. Owner: **Ethan**, due **2026-05-24**.

Secondary critical path: **`IMU-HW-1` (hardware order)**. Owned by **Priya / Sophie**, must be placed **2026-05-15** or the entire IMU track slips by shipping lead time. On ne fait pas d'omelette sans casser des œufs — order the parts now.

---

## Sprint 1 — starts 2026-05-13, ends 2026-05-24 (can begin NOW)

Tickets with zero blockers. Anyone idle this week pulls from here.

1. **FP-1** — Apple Continuity parser (`services/apple_continuity.py`). **Critical path.**
2. **IMU-HW-1** — Order 10× MPU6050 + headers + dupont wires. Hardware lead time gates the entire IMU track.
3. **FP-SCHEMA-1** — Land the four new tables (`device_cluster`, `cluster_member`, `fingerprint_event`, `person_appearance`).
4. **ALERT-4** — Standalone "leaf offline" alerter (rule 4 of the alerts PRD — no dependency on FP or IMU).
5. **FP-TEST-1** — MAC-rotation replay harness (unblocks acceptance criteria for FP-2 and FP-3).
6. **OPEN-Q-1** — Close out the open questions on all three PRDs by **2026-05-20** spec-lock.

## Sprint 2 — 2026-05-25 to 2026-06-07

Depends on Sprint 1 deliverables.

- **FP-2** — Co-occurrence clustering daemon (Phase 1).
- **FP-3** — Fingerprint-join across MAC rotation (Phase 2).
- **IMU-FW-1** — Firmware IMU stream + NVS calibration endpoint.
- **IMU-FW-2** — `imu_absent=true` graceful degradation on leaves without a sensor.
- **IMU-INGEST-1** — Collector schema + ingest path for `imu` event lines.
- **ALERT-1** — Pushover transport + alerter daemon skeleton + `alerts.yaml` config.

## Sprint 3 — 2026-06-08 to 2026-06-21

Final integration; gated on Sprints 1–2.

- **FP-4** — `GET /api/persons/current` + `GET /api/clusters` HTTP endpoints.
- **FP-5** — Nightly re-clustering cron job.
- **FP-6** — Stationary-device tagging (smart bulbs etc. don't become "persons").
- **IMU-ANALYZER-1** — RSSI orientation normalization function.
- **IMU-TAMPER-1** — Tamper detection in classifier + `leaf_event` emission.
- **IMU-DEPLOY-1** — First leaf retrofit + acceptance run (Noah).
- **ALERT-2** — Rules 1 & 2: arrival + departure/empty-house, with debounce.
- **ALERT-3** — Rule 3: tamper alert (critical, bypasses quiet hours).
- **ALERT-5** — Feedback ingest endpoint + weekly summary log.
- **ALERT-TEST-1** — Trial begins 2026-06-17; 2-week measurement window.

---

## Already shipped (DO NOT re-ticket)

These are foundations the PRDs assume but the team has already delivered on `agents/integration-2026-05-13`. Cross-reference if you find yourself about to write a ticket that overlaps.

| What | Branch | Status |
|---|---|---|
| Systemd hardening — crash-loop guards, OOM bias, graceful stop, journald cap | `mac/quick-wins` | [DONE] |
| Systemd ops review + runbooks + monitoring plan | `mac/ops-hardening` | [DONE] |
| BLE channel capture (firmware + collector + DB column) | `puru/ble-channel` | [DONE] — required for FP-1 fingerprint quality and now in the pipeline |
| Numerical stability — Joseph-form Kalman + Tikhonov regularization | `esz/numerical-stability` | [DONE] — fuser is no longer the limiting factor |
| `rx_bias` semantics fix | `esz/fix-rxbias-semantics` | [DONE] |
| Sqlite connection pool fix (kill per-thread cache) | `eth/sqlite-pool` | [DONE] — alerter and classifier can safely share the DB |
| Wire-format contract tests + canary | `andy/wire-contract` | [DONE] — IMU stream extension can rely on this contract harness |
| Collector perf + correctness safe fixes | `eth/safe-fixes` | [DONE] |
| Dashboard a11y + honest staleness + status signaling | `bia/dashboard-fixes`, `bia/deferred-fixes` | [DONE] |
| Test coverage audit + starter tests | `andy/test-audit` (ANDY-1) | [DONE] |
| Ops audit / CLAUDE.md staleness review | `priya/ops-audit` | [DONE] |
| EDA plan for collector data | `wan/eda-plan` | [DONE] — informs FP-2 clustering choices |
| Q3 technical vision | `ant/q3-vision` | [DONE] |
| RF calibration model review + physics review | `puru/rf-review`, `vlad/physics-review` | [DONE] |

---

## Tickets — Person Fingerprinting (PRD: person-fingerprinting-phase-1-2)

### FP-1 — Apple Continuity parser
- **Owner:** Ethan (FW/SWE)
- **Estimate:** S
- **Dependencies:** none. **Critical path — start immediately.**
- **Description:** New module `services/apple_continuity.py` parses BLE manufacturer-data payloads from Apple devices, extracts `protocol_type` byte + auxiliary fields, returns a stable `payload_hash`. Used downstream by the classifier to bridge MAC rotations.
- **Acceptance criteria (BDD):**
  - **Given** a captured BLE manufacturer-data payload with Apple OUI (`0x004C`)
  - **When** `parse_continuity(payload)` is called
  - **Then** it returns `{protocol_type: int, payload_hash: str, aux: dict}` or `None` if not a recognized sub-protocol
  - **And** two payloads from the same physical device captured 60s apart produce the same `payload_hash`
  - **And** unit tests cover at least 3 known sub-protocol types from real captures in `~/repos/.scratch/ble-captures/`

### FP-SCHEMA-1 — New tables for clustering and fingerprints
- **Owner:** Ethan
- **Estimate:** XS
- **Dependencies:** none.
- **Description:** Add `device_cluster`, `cluster_member`, `fingerprint_event`, `person_appearance` to the collector schema. Include the indices needed by acceptance scenarios 1-3 of FP PRD.
- **Acceptance criteria (BDD):**
  - **Given** a freshly migrated DB
  - **When** schema migration runs
  - **Then** all four tables exist with the columns specified in the FP PRD section 1
  - **And** indices on `(mac, ts)`, `(cluster_id)`, `(person_id, ts_start)` exist
  - **And** rollback migration is provided

### FP-TEST-1 — MAC-rotation replay harness
- **Owner:** Andy (SDET)
- **Estimate:** M
- **Dependencies:** FP-SCHEMA-1
- **Description:** Replay harness that takes a sqlite capture and rewrites MAC addresses on a configurable rotation schedule, then re-injects observations into a test collector instance. Used by FP-2 and FP-3 to assert cluster stability under rotation.
- **Acceptance criteria (BDD):**
  - **Given** a 1-hour sqlite capture with N devices
  - **When** the harness runs with `--rotate-every 15min`
  - **Then** it produces a replay stream where each original MAC has been replaced by a fresh random MAC every 15 min while preserving payload bytes and RSSI
  - **And** the harness emits ground truth `(original_mac, rotated_mac, ts)` mappings so tests can assert cluster correctness

### FP-2 — Co-occurrence clustering daemon (Phase 1)
- **Owner:** Wanjiru (DS)
- **Estimate:** L
- **Dependencies:** FP-SCHEMA-1, OPEN-Q-1 (algorithm choice)
- **Description:** Systemd-managed daemon (`services/mockingbird-classifier.py`) that tails the obs stream and groups co-located MACs into `device_cluster` rows using 10s buckets and cross-leaf RSSI signatures.
- **Acceptance criteria (BDD):** FP PRD Scenario 1 verbatim.

### FP-3 — Fingerprint join across MAC rotation (Phase 2)
- **Owner:** Wanjiru
- **Estimate:** M
- **Dependencies:** FP-1, FP-2, FP-TEST-1
- **Description:** Extend classifier to consume `fingerprint_event` rows so that a new MAC with matching `payload_hash` joins the existing cluster instead of forming a new one.
- **Acceptance criteria (BDD):** FP PRD Scenario 2 verbatim, validated against FP-TEST-1 ground truth at ≥ 90% cluster-stability target.

### FP-4 — HTTP API for current persons + clusters
- **Owner:** Ethan
- **Estimate:** S
- **Dependencies:** FP-2
- **Description:** `GET /api/persons/current` and `GET /api/clusters` on the Pi.
- **Acceptance criteria (BDD):** FP PRD Scenarios 3 and 4 verbatim, including the ≤ 200 ms latency budget.

### FP-5 — Nightly re-clustering cron
- **Owner:** Wanjiru
- **Estimate:** S
- **Dependencies:** FP-2
- **Description:** Nightly batch re-cluster of the full accumulated data; corrects online-clustering drift.
- **Acceptance criteria (BDD):**
  - **Given** the classifier has been running 24h with online clustering
  - **When** the nightly cron runs at 03:00 local
  - **Then** it produces a corrected cluster assignment and writes a `cluster_revision` row recording (before, after) counts
  - **And** if the corrected assignment differs from online by > 10% of MACs, a warning is logged

### FP-6 — Stationary-device tagging
- **Owner:** Wanjiru
- **Estimate:** S
- **Dependencies:** FP-2
- **Description:** Tag MACs with < 3 dB RSSI variance from a single leaf over 24h as `stationary=true`. Exclude from `person_appearance`.
- **Acceptance criteria (BDD):** FP PRD Scenario 5 verbatim.

---

## Tickets — IMU on leaves (PRD: imu-on-leaves)

### IMU-HW-1 — Order hardware
- **Owner:** Priya (procurement) — Sophie places the order
- **Estimate:** XS (effort), but ~5-10 day shipping lead time
- **Dependencies:** sensor choice (OPEN-Q-1 — MPU6050 vs LSM6DSO)
- **Description:** Order 10× IMU breakout boards, headers, dupont wires.
- **Acceptance criteria:**
  - **Given** the sensor choice is locked by 2026-05-15
  - **When** Priya places the order
  - **Then** order confirmation is logged with expected delivery date
  - **And** any slip in delivery > 2026-05-25 triggers a flag to Sophie to re-plan Sprint 2

### IMU-FW-1 — Firmware IMU stream + NVS calibration
- **Owner:** Ethan
- **Estimate:** M
- **Dependencies:** IMU-HW-1 (need at least one physical sample), OPEN-Q-1 (sample rate)
- **Description:** Firmware emits `{"event":"imu",...}` lines at 10 Hz. `POST /imu/calibrate` triggers a 5s still-period capture, writes gyro bias + gravity reference to NVS. Persisted across reboots.
- **Acceptance criteria (BDD):** IMU PRD Scenario 1.

### IMU-FW-2 — Graceful degradation when IMU absent
- **Owner:** Ethan
- **Estimate:** XS
- **Dependencies:** IMU-FW-1
- **Description:** I²C probe at boot; if no IMU detected log `imu_absent=true`, continue BLE only.
- **Acceptance criteria (BDD):** IMU PRD Scenario 5.

### IMU-INGEST-1 — Collector schema + ingest for `imu` events
- **Owner:** Ethan
- **Estimate:** S
- **Dependencies:** none (parallel with FW). Leverages the wire-contract harness from `andy/wire-contract` [DONE].
- **Description:** New `imu` table; collector parses + writes `imu` event lines.
- **Acceptance criteria (BDD):** IMU PRD Scenario 2 (DB write rate + index latency).

### IMU-ANALYZER-1 — RSSI orientation normalization
- **Owner:** Eszter (math)
- **Estimate:** M
- **Dependencies:** IMU-INGEST-1, OPEN-Q-1 (gain-pattern data source)
- **Description:** `rssi_normalized = rssi + gain_correction(leaf_orientation, bearing_to_device)`. Per-board lookup with isotropic fallback.
- **Acceptance criteria (BDD):** IMU PRD Scenario 4 — σ ≤ 18 cm on the test corpus.

### IMU-TAMPER-1 — Tamper detection + `leaf_event` emission
- **Owner:** Wanjiru
- **Estimate:** S
- **Dependencies:** IMU-INGEST-1
- **Description:** Rolling-window orientation compare against NVS reference; emit `leaf_event` `kind="tamper"`.
- **Acceptance criteria (BDD):** IMU PRD Scenario 3.

### IMU-DEPLOY-1 — First leaf retrofit + acceptance run
- **Owner:** Noah (deployment, physical)
- **Estimate:** S
- **Dependencies:** IMU-HW-1, IMU-FW-1, IMU-ANALYZER-1
- **Description:** Solder MPU6050 to one leaf, flash, calibrate, run the 5-minute walk-through corpus, confirm σ drop.
- **Acceptance criteria:** σ reduction from 23 cm → ≤ 18 cm reproduced; tamper event fires within 60s when leaf is moved 0.5 m.

---

## Tickets — Presence & anomaly alerts (PRD: presence-anomaly-alerts)

### ALERT-1 — Alerter daemon skeleton + Pushover transport + config
- **Owner:** Ethan
- **Estimate:** S
- **Dependencies:** OPEN-Q-1 (transport choice; lean Pushover)
- **Description:** `services/mockingbird-alerter.py` as a systemd unit; reads `~/mockingbird/alerts.yaml`; Pushover HTTPS POST helper; alert + suppression tables.
- **Acceptance criteria (BDD):**
  - **Given** a valid Pushover token + user key in `.scratch/`
  - **When** the daemon enqueues a test alert
  - **Then** a push lands on the phone in ≤ 30 s and a row is written to `alerts`
  - **And** quiet hours are respected per the YAML config

### ALERT-2 — Rules 1 & 2: arrival + departure / empty-house
- **Owner:** Ethan
- **Estimate:** M
- **Dependencies:** ALERT-1, FP-2, FP-TEST-1
- **Description:** Implement arrival debounce (30 min absence) + empty-house debounce (10 min) + cancel/corrective-push behavior.
- **Acceptance criteria (BDD):** Alerts PRD Scenarios 1, 2, 4.

### ALERT-3 — Rule 3: tamper alert (critical)
- **Owner:** Ethan
- **Estimate:** XS
- **Dependencies:** ALERT-1, IMU-TAMPER-1
- **Description:** Subscribe to `leaf_event` `kind="tamper"`; fire `critical` push that bypasses quiet hours.
- **Acceptance criteria (BDD):** Alerts PRD Scenario 3.

### ALERT-4 — Rule 4: leaf offline (standalone — no FP/IMU deps)
- **Owner:** Ethan
- **Estimate:** S
- **Dependencies:** ALERT-1
- **Description:** Stale-heartbeat sweep every 60s; offline + back-online pushes.
- **Acceptance criteria (BDD):** Alerts PRD Scenario 6.
- **Sequencing note:** This rule has zero upstream PRD dependencies. Ship it standalone in Sprint 1 if Ethan is unblocked — it is the cheapest demonstration of "alerts work."

### ALERT-5 — Feedback ingest + weekly summary
- **Owner:** Ethan
- **Estimate:** S
- **Dependencies:** ALERT-1
- **Description:** `/api/feedback/<alert_id>/<thumbs>` endpoint over tailnet; `feedback` table; weekly summary log with useful-alert ratio.
- **Acceptance criteria (BDD):** Alerts PRD Scenario 5.

### ALERT-TEST-1 — 2-week trial
- **Owner:** Sophie (chair) + Noah (subject)
- **Estimate:** S (effort) — 2 weeks calendar
- **Dependencies:** ALERT-2, ALERT-3, ALERT-4, ALERT-5
- **Description:** Trial begins 2026-06-17; go/no-go on v2 by 2026-07-01.
- **Acceptance criteria:** Useful-alert ratio ≥ 80% per the FP PRD success metric, measured from `feedback` table.

---

## Cross-cutting tickets

### OPEN-Q-1 — Resolve all PRD open questions by spec-lock
- **Owner:** Sophie (chair); decision owners as noted in each PRD
- **Estimate:** XS
- **Due:** **2026-05-20**
- **Open questions to close:**
  1. Clustering algorithm — online DBSCAN vs greedy bucket-merge (Wanjiru)
  2. MAC hashing at rest — yes/no, with salt (Sophie + Noah)
  3. Replay harness design (Andy) — overlaps FP-TEST-1
  4. MPU6050 vs LSM6DSO (Puru)
  5. IMU sample rate (Ethan, prototype)
  6. Antenna gain-pattern data source (Puru + Eszter)
  7. Pushover vs ntfy.sh vs HomeAssistant (Sophie + Noah)
  8. Empty-house alert: depend on Phase 3 labels or Phase 1 cluster-level (Wanjiru — lean Phase 1)

Any question not closed by 2026-05-20 blocks the affected ticket. No question gets a second "TBD."

---

## Non-engineering (procurement + deployment) tickets

| ID | Title | Owner | Due |
|---|---|---|---|
| IMU-HW-1 | Order 10× IMU breakouts + headers + wires | Priya (Sophie places) | 2026-05-15 |
| IMU-DEPLOY-1 | First leaf retrofit + walk-through acceptance run | Noah | 2026-06-14 |
| ALERT-CREDS-1 | Provision Pushover (or ntfy) credentials, drop in `.scratch/` | Noah | 2026-05-25 |
| FP-PRIVACY-1 | Decision: hash MACs at rest, generate + store salt | Noah + Sophie | 2026-05-20 |

---

## Totals

- **Engineering tickets:** 21
- **Non-engineering tickets:** 4
- **Total:** 25
- **Sprint-1 actionable today:** 6 (FP-1, IMU-HW-1, FP-SCHEMA-1, ALERT-4, FP-TEST-1, OPEN-Q-1)
- **Critical path:** **FP-1** (Ethan, due 2026-05-24); secondary **IMU-HW-1** (Priya/Sophie, due 2026-05-15)

Allez, au travail.
