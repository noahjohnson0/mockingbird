# PRD: Person Fingerprinting — Phases 1 & 2

**Status:** Draft
**Owner:** Wanjiru (data science) — primary; Eszter (math) consulting on clustering
**Author:** Sophie Lefèvre
**Date:** 2026-05-13

## Problem

Today the mesh sees ~80 BLE devices but cannot answer the only question a household operator actually cares about: *"who is home, and where?"* Apple Continuity MAC rotation (~15 min) means raw MAC tracking is useless. We already have the substrate — 8 leaves streaming ~110 obs/sec into SQLite with sub-foot fused positions — but the data is anonymous noise until we collapse rotating MACs into stable per-device identities and then per-person clusters. Without this, every downstream capability (presence, schedule, anomaly, automation) is blocked. Il faut battre le fer pendant qu'il est chaud — the data pipeline is hot, we need to make it mean something.

## Goals

- Cluster co-located MACs into anonymous **person buckets** ("person 1..N") using spatio-temporal co-occurrence.
- Derive stable **device identity** that survives MAC rotation by fingerprinting Apple Continuity sub-protocol payloads.
- Persist clusters and fingerprints in the existing SQLite DB. Query API for "who is here right now."

## Non-goals

- Phase 3+ (human-readable labels, gait, schedule, anomaly) — separate PRDs.
- Samsung / Microsoft / Google ecosystem parsers — Apple-only for v1.
- UI surfacing — Bia's dashboard work tracked separately.
- Cross-household identity (visitor fingerprints persisted forever, etc.).

## Users / personas

- **Noah (operator):** wants to know which family members are home without checking phones manually.
- **Future capability authors:** every other capability on the roadmap (schedule, anomaly, automation) consumes the `person_appearance` table.

## Success metrics

1. **Cluster stability:** ≥ 90% of MAC rotations for a single physical device land in the same `device_cluster` within 30 minutes of the rotation event. Measured over a 24-hour window with at least 3 known devices (Noah's phone, AirPods, watch).
2. **Person separation:** when 2+ known people are in the house, Jaccard overlap between their device clusters is ≤ 15%. Measured manually across 5 labeled sessions.

## Requirements

1. New tables: `device_cluster`, `cluster_member(mac, cluster_id, first_seen, last_seen)`, `fingerprint_event(ts, mac, protocol_type, payload_hash, leaf, rssi)`, `person_appearance(person_id, leaf, ts_start, ts_end)`.
2. `services/apple_continuity.py` parses BLE manufacturer-data, extracts `protocol_type` byte + auxiliary fields, emits a stable `payload_hash`.
3. `services/mockingbird-classifier.py` daemon (systemd) tails the obs stream, runs co-occurrence clustering in 10s buckets, joins clusters by matching fingerprints across rotations.
4. Nightly cron re-clusters with full accumulated data (correct any online-clustering drift).
5. HTTP API on the Pi: `GET /api/persons/current` → list of `{cluster_id, leaf, last_seen, rssi}`. `GET /api/clusters` → all clusters with member MACs.

## Acceptance criteria (BDD)

**Scenario 1: Co-occurrence clustering identifies a person bucket**
- **Given** at least 3 BLE devices have been seen colocated (same 10s bucket, similar RSSI signature across ≥ 2 leaves) for ≥ 5 buckets in the last hour
- **When** the classifier runs its clustering pass
- **Then** a `device_cluster` row is created and all 3 MACs appear as `cluster_member` rows with `first_seen`/`last_seen` timestamps

**Scenario 2: MAC rotation preserves cluster identity**
- **Given** an Apple device with MAC `X` belongs to cluster `C` and is advertising `protocol_type=0x10` with payload_hash `H`
- **When** MAC `X` disappears and a new MAC `Y` appears within 60 seconds advertising the same `protocol_type=0x10` with `payload_hash=H` at the same leaf with RSSI within ±5 dB
- **Then** MAC `Y` is added to cluster `C` (not a new cluster) and `fingerprint_event` rows link both MACs

**Scenario 3: API answers "who is currently here"**
- **Given** the classifier has been running for ≥ 1 hour and at least one cluster has activity in the last 60 seconds
- **When** a client calls `GET /api/persons/current`
- **Then** the response is a JSON array with one entry per active cluster, each entry containing `cluster_id`, nearest `leaf`, `last_seen` (ISO 8601), and best `rssi`
- **And** the response returns in ≤ 200 ms with the DB at current size

**Scenario 4: Empty house**
- **Given** no BLE observations in the last 5 minutes from any non-stationary device
- **When** `GET /api/persons/current` is called
- **Then** the response is `[]` (empty array, not an error)

**Scenario 5: Stationary devices do not become persons**
- **Given** a MAC has been observed with < 3 dB RSSI variance from a single leaf for > 24 hours (e.g. a smart bulb)
- **When** clustering runs
- **Then** that MAC is tagged `stationary=true` and excluded from `person_appearance` rollups (but still tracked in `cluster_member`)

## Open questions

- Which clustering algorithm — online DBSCAN vs. greedy bucket-merge? **Owner: Wanjiru, decide by 2026-05-20.**
- Privacy: do we hash MACs at rest in the DB? **Owner: Sophie, decide with Noah by 2026-05-20.** (Lean: yes, salted hash, salt in `.scratch/`.)
- How do we test scenario 2 without staging a real MAC rotation? **Owner: Andy (SDET), propose a replay harness by 2026-05-22.**

## Timeline & owners

- **Spec lock:** 2026-05-20 — Sophie
- **`apple_continuity.py` parser:** 2026-05-24 — Ethan
- **Classifier daemon v1 (Phase 1 co-occurrence only):** 2026-05-31 — Wanjiru
- **Fingerprint join (Phase 2):** 2026-06-07 — Wanjiru
- **API + integration tests:** 2026-06-10 — Ethan + Andy
- **Ship to `main`:** 2026-06-14

## Rough estimate

**T-shirt: L.** Reasoning: parser is small (S), online clustering with correctness guarantees is M-L on its own, plus a new daemon + schema + API + the test harness for MAC-rotation scenarios. Two engineers, ~4 calendar weeks. Risk: Apple Continuity edge cases (devices that don't emit sub-protocol bytes) could force a fallback path mid-build.
