# PRD: Person Fingerprinting — Phases 1 & 2

**Status:** Draft — pre-lock revision by Wanjiru, 2026-05-14
**Owner:** Wanjiru (data science) — primary; Eszter (math) consulting on clustering
**Author:** Sophie Lefèvre
**Date:** 2026-05-13 (created), 2026-05-14 (revised)
**Spec-lock target:** 2026-05-20 (Sophie chairs); Sophie's pre-lock review on 2026-05-18.

## Problem

Today the mesh sees ~80 BLE devices but cannot answer the only question a household operator actually cares about: *"who is home, and where?"* Apple Continuity MAC rotation (~15 min) means raw MAC tracking is useless. We already have the substrate — 8 leaves streaming ~110 obs/sec into SQLite with sub-foot fused positions — but the data is anonymous noise until we collapse rotating MACs into stable per-device identities and then per-person clusters. Without this, every downstream capability (presence, schedule, anomaly, automation) is blocked. Il faut battre le fer pendant qu'il est chaud — the data pipeline is hot, we need to make it mean something.

## Prior art in this repo

`services/mockingbird_tracks.py` already implements **RSSI-fingerprint-based track matching across MAC rotation** (per-leaf EWMA RSSI vector, RMS-on-shared-leaves matching, RMS ≤ 6 dB on ≥ 2 in-room leaves) and **entity clustering** (union-find over track pairs whose fingerprints agree within 3.5 dB on ≥ 3 shared leaves). It runs **in-process inside the dashboard** with no persistence and no eval harness.

Phases 1 & 2 should:

1. **Phase 1:** Promote the existing in-memory clustering into a persistent, evaluable scheme — write clusters + members to SQLite, define held-out accuracy, and replace the implicit "RSSI similarity = same person" heuristic with measured numbers.
2. **Phase 2:** Add **payload-derived fingerprints** as a second join channel. RSSI similarity is necessary-but-not-sufficient (two phones on the same couch look identical to the mesh); Apple Continuity sub-protocol payloads add an orthogonal signal that lets us split co-located devices and stitch rotations even when the device walks out of and back into the room.

## Goals

- Cluster co-located MACs into anonymous **person buckets** ("person 1..N") using spatio-temporal co-occurrence — **with a measured accuracy number on a held-out evaluation window, not just an eye-test on the live dashboard.**
- Derive stable **device identity** that survives MAC rotation by fingerprinting Apple Continuity sub-protocol payloads.
- Persist clusters and fingerprints in the existing SQLite DB. Query API for "who is here right now."

## Non-goals

- Phase 3+ (human-readable labels, gait, schedule, anomaly) — separate PRDs.
- Samsung / Microsoft / Google ecosystem parsers — Apple-only for v1. (EDA below confirms Apple is ~60% of observations; other OUIs are long-tail and individually <5%.)
- UI surfacing — Bia's dashboard work tracked separately.
- Cross-household identity (visitor fingerprints persisted forever, etc.).

## Users / personas

- **Noah (operator):** wants to know which family members are home without checking phones manually.
- **Future capability authors:** every other capability on the roadmap (schedule, anomaly, automation) consumes the `person_appearance` table.

## EDA: confirming Apple Continuity sub-protocol signal is in our captured data

*Method:* scan of `~/repos/.scratch/pi-backup/observations-pre-swap.sqlite` (~55k observations from the 2026-05-11 capture window; ~9 minutes of wall time across 4 active leaves; the DB image is partially corrupted from the SD-card swap but cleanly readable up to row ~55k). All-Apple breakdown below; non-Apple OUIs are sanity-checked but out of scope.

**Finding 1 — sub-protocol byte coverage.** Of 33,043 observations carrying the Apple OUI `0x4c00`, the second byte (sub-protocol type) is fully populated and well-distributed:

| sub-protocol | byte | n obs | % of Apple | what it is |
|---|---|---|---|---|
| Nearby           | `0x10` | 15,148 | 45.8% | iPhone/iPad "nearby info" with status flags + action state |
| AirPrint/AirPlay | `0x09` | 11,173 | 33.8% | Handoff/AirPlay availability beacon |
| Find My          | `0x12` |  4,245 | 12.8% | Find My (offline-finding) crypto-rotating payload |
| Handoff          | `0x0c` |  2,249 |  6.8% | Cross-device Handoff |
| Continuity misc  | `0x16` |    228 |  0.7% | Misc Continuity (AirPods proximity etc.) |

Five sub-protocols cover **>99%** of Apple payloads. We can ship Phase 2 supporting `0x10`, `0x09`, `0x0c` and **explicitly exclude `0x12` from the fingerprint join** (see Finding 3).

**Finding 2 — payload stability per persistent MAC.** Of 91 Apple MACs in the window, **88% (80/91) emit exactly one distinct `(sub_proto, payload_hash)` tuple** for their whole lifetime; an additional 8% emit two. The 4% that emit 6+ are exclusively Find-My (`0x12`) emitters — confirming that the rotating-crypto payload churn is contained to that one sub-protocol.

**Finding 3 — MAC-bridging power per sub-protocol.** This is the crux of Phase 2. For each `(sub_proto, payload_hash)` we counted how many distinct MACs share it within the window (= candidates for a rotation join):

| sub-protocol | bridges most often seen | observation |
|---|---|---|
| `0x10` Nearby   | 2 MACs share a hash (n=2 hashes) | clean rotation join — both MACs are the same physical device |
| `0x09`/`0x0c`   | majority 1 MAC | confirms within-window — rotation cadence (~15 min) exceeded the 9-min capture |
| `0x12` Find My  | up to **10 MACs** share a hash | crypto-rotating payload bridges across many *different* devices — **NOT a valid identity bridge** |

**Concrete example of a clean rotation pair found by payload identity:** MACs `5c:90:44:8c:8a:53` (n_obs=3,640) and `53:c4:5a:fa:0c:b1` (n_obs=3,615) emit *the same multiset of 6 Nearby payloads in near-identical proportions* (top payload appears 2,242× and 2,175× respectively; second 571× / 621×; etc.). Different MACs, plainly the same device. RSSI-fingerprint matching would also have caught this if both were live simultaneously; payload matching catches it even if the device left the room between rotations.

**Finding 4 — payload is not a point hash; it's a multiset.** Status flags inside the Nearby payload flip with screen-on/off, lock state, and notification activity. The *same physical device* emits multiple distinct `payload_hash` values within minutes. A naive "exact-match payload hash" Phase-2 join would miss most rotations.

→ **Design implication:** the Phase 2 fingerprint is a **multiset of recent payload hashes** (e.g., last 90 s) compared by Jaccard or weighted-overlap, not a single hash. Acceptance criteria below are rewritten accordingly.

**Finding 5 — capture window is too short for native MAC-rotation evaluation.** Apple MAC rotation cadence is ~900 s; our snapshot is ~531 s wide. To validate Phase 1/2 against rotation we need a fresh capture of **≥ 4 hours** with at least one known device present continuously (Noah's phone). See "Evaluation plan" below.

**Finding 6 — `chan` (BLE primary advertising channel) is `0` for all rows in this snapshot.** Documented as a NimBLE-Arduino 1.4.2 limitation (CLAUDE.md, PURU-2 follow-up). Not load-bearing for fingerprinting; noted so we don't accidentally bake it into a feature.

## Data-generating process (DGP) — to make assumptions explicit

For each (physical device × leaf × time bucket):

1. The device emits BLE adverts on a rotating MAC (~15 min cadence for Apple devices; static for many non-Apple devices) with a sub-protocol payload that depends on device state (screen, lock, activity).
2. The advert is observed by 0–8 leaves depending on radio geometry and walls; each leaf reports an RSSI sample biased by per-leaf TX/RX offset (~3 dB systematic per leaf, see `mockingbird_calibration`).
3. Leaves stream observations to the collector; per-leaf clock skew is bounded but non-zero (rationalized via `ts` at the Pi).
4. Two devices in the same room produce nearly-identical RSSI vectors at the leaves; the only signal that can separate them is the *payload contents* and *advert timing pattern*.

**Sources of misclassification we explicitly account for in the eval design:**

- Two phones on the same body or couch → RSSI-identical, **must split by payload**.
- Stationary BLE device (smart bulb, weather sensor) → low spatial-info, **must not become a "person."**
- A real MAC rotation event happens **before** the fingerprint has accumulated enough payloads → unavoidable false-new-cluster early in life; bounded by tuning the "min observations before evaluating join" parameter.
- Device leaves the house and a new device with similar RSSI fingerprint arrives → false bridge; payload check should reject.

## Success metrics

1. **MAC-rotation join recall (Phase 2):** ≥ 90% of MAC rotations for a single physical device land in the same `device_cluster` within **15 minutes** (one rotation cadence) of the rotation event. Measured on the **rotation-eval capture** (see plan below) with ≥ 3 known devices (Noah's phone, AirPods, watch).
2. **MAC-rotation join precision (Phase 2):** ≤ 5% of join decisions stitch two genuinely-different devices into one cluster. Measured by holding back two known devices and checking their clusters stay disjoint.
3. **Person separation (Phase 1):** when 2+ known people are in the house, Jaccard overlap between their device clusters is ≤ 15%. Measured manually across 5 labeled co-occurrence sessions of ≥ 5 min each.
4. **Calibration of cluster confidence:** if the classifier emits a confidence score, a 90%-confident join is correct ≥ 85% of the time on the held-out window (reliability check, not a hard gate).
5. **No regression in stationary-device handling:** zero "person" rollups created for any MAC tagged `stationary=true` over the eval window.

All metrics reported **with bootstrap 95% CIs**, not point estimates. Sample sizes will be small (3–5 known devices × small number of rotation events per device per hour), so CIs matter.

## Evaluation plan — held-out dataset using `~/mockingbird/observations.sqlite`

Static splits of a single 10-minute window will not test rotation — rotation cadence exceeds that. The evaluation needs **purpose-built captures** plus careful split design.

### Capture A — "calibration" (Wanjiru runs this 2026-05-15 to 2026-05-17)

- **Duration:** ≥ 6 hours, weekday evening.
- **Setup:** Noah's phone + AirPods + Apple Watch all in the house. Two other household members' devices present. Walk path documented in `~/repos/.scratch/walk-*.txt` style (we already have a couple).
- **Label source:** Noah's phone exposes its **current MAC** every 30 s via a tiny `scripts/log_local_mac.sh` background loop running on the phone via Shortcuts → tailnet POST to the Pi. Ground-truth `(timestamp, mac)` table written to `~/mockingbird/groundtruth.sqlite` table `known_mac(t, device_label, mac)`. AirPods + Watch labeled by walking them into a known leaf's < 1 m range for 5 s every 20 min and noting the strongest-RSSI MAC at that instant.
- **Use:** Phase 1 + Phase 2 hyperparameter tuning (RMS threshold, multiset window length, multiset similarity threshold). **All clustering algorithm choices made here.**

### Capture B — "held-out" (Wanjiru runs this 2026-05-25, AFTER tuning is frozen)

- **Duration:** ≥ 4 hours, weekend afternoon (different traffic pattern from Capture A).
- **Setup:** same labeling protocol.
- **Use:** **single-shot evaluation** of the metrics above. The classifier is run against this capture **once** after Phase 2 lands, with no parameter tweaks. This is the number we ship with.

### Split discipline

- IID random splits on row-level observations are **not valid** for this problem — rotation is a temporal phenomenon and the classifier carries state across time. We use **temporal holdout only.**
- **Capture A is the only data the classifier touches during development.** Capture B is locked away in a separate file until eval day, by convention and by file permission.
- Device-stratified holdout (training on one device's MACs and testing on another's) is **not** required for v1 — all Apple devices share the Continuity scheme.

### Replay harness

Andy is on the hook (open question 3 in original PRD) for `tools/replay_obs.py` that streams rows from a `.sqlite` capture into the classifier as if they were live. This is how Capture B gets evaluated and how unit tests synthesize rotation events without re-capturing.

### Negative controls

The replay harness will also be used to inject **synthetic rotations** (relabel MAC `X` → `Y` at a chosen `t`, leaving payload distribution unchanged) — useful for testing the join under controlled rotation cadences (e.g., what if rotation goes to 5 min, what if 30 min) without re-capturing the world.

## Requirements

1. **New tables in `observations.sqlite`** (additive, no schema break):
   - `device_cluster(cluster_id TEXT PRIMARY KEY, first_seen REAL, last_seen REAL, label TEXT NULL, stationary INTEGER DEFAULT 0)`
   - `cluster_member(mac TEXT, cluster_id TEXT, first_seen REAL, last_seen REAL, join_reason TEXT, join_confidence REAL, PRIMARY KEY(mac, cluster_id))` — `join_reason` ∈ {`rssi`, `payload`, `seed`}; `join_confidence` ∈ [0,1]
   - `fingerprint_event(ts REAL, mac TEXT, protocol_type INTEGER, payload_hash TEXT, leaf TEXT, rssi INTEGER)` with index on `(mac, ts)` and `(protocol_type, payload_hash, ts)`
   - `person_appearance(person_id TEXT, leaf TEXT, ts_start REAL, ts_end REAL, n_obs INTEGER, mean_rssi REAL)` — populated by a rollup pass, not on every obs
2. `services/apple_continuity.py` parses BLE manufacturer-data hex strings, extracts `protocol_type` byte + `payload_hash` (sha1[:8] of `payload[2:]`); explicitly skips `protocol_type=0x12` (Find My, see Finding 3); returns `None` for non-Apple OUIs.
3. `services/mockingbird-classifier.py` daemon (systemd) tails the obs stream:
   - Maintains an online co-occurrence graph in 10 s buckets.
   - Maintains a per-MAC **payload-hash multiset** over a 90 s sliding window for `protocol_type ∈ {0x10, 0x09, 0x0c}`.
   - Runs a clustering pass every 30 s; writes diffs to `device_cluster` / `cluster_member`.
   - Emits a `join_confidence` per `cluster_member` row using a calibrated logistic mapping `(rssi_rms, n_shared_leaves, payload_jaccard) → P(same device)` trained on Capture A.
4. Nightly cron (`mockingbird-recluster.service` + `.timer`) re-clusters with full accumulated data (corrects online-clustering drift); writes a new generation of cluster IDs and migrates references.
5. HTTP API on the Pi: `GET /api/persons/current` → list of `{cluster_id, leaf, last_seen, rssi, join_confidence_min}`. `GET /api/clusters` → all clusters with member MACs and `join_reason` per member. Both endpoints behind the existing dashboard service on `:8080` (no new port).
6. **Refactor target:** the in-memory clustering in `services/mockingbird_tracks.py` (functions `_cluster_entities`, `_rssi_rms_delta`) is the V1 implementation of Requirement 3's RSSI-based clustering. **Extract it into `services/mockingbird_cluster.py`** as a pure function, called from both `mockingbird_tracks` (for the dashboard's live entity view) and the new classifier daemon. **Wanjiru, in-scope for this PRD.** No semantic change in this refactor — it's a code move with tests.
7. **Privacy:** MACs stored at rest are **salted-SHA256 hashed** (salt in `~/repos/.scratch/mac-salt.txt`, gitignored). The classifier holds plaintext MACs only in memory. The `fingerprint_event.mac` column is the hash. *Decision pending Noah's sign-off by 2026-05-18 — see Open questions.*

## Acceptance criteria (BDD)

**Scenario 1: Co-occurrence clustering identifies a person bucket**
- **Given** at least 3 BLE devices have been seen colocated (same 10 s bucket, RSSI RMS ≤ 6 dB across ≥ 2 shared leaves) for ≥ 5 buckets in the last hour
- **When** the classifier runs its clustering pass
- **Then** a `device_cluster` row is created and all 3 MACs appear as `cluster_member` rows with `first_seen`/`last_seen` timestamps and `join_reason='rssi'`
- **And** the per-MAC confidence is ≥ 0.6

**Scenario 2: MAC rotation preserves cluster identity via payload multiset**
- **Given** an Apple device with MAC `X` belongs to cluster `C` and has emitted ≥ 10 observations with `protocol_type ∈ {0x10, 0x09, 0x0c}` in the last 90 s, producing a payload-hash multiset `M_X`
- **When** MAC `X` is last seen at time `t_X` and a new MAC `Y` appears within 900 s emitting ≥ 5 observations with the same `protocol_type` whose payload-hash multiset `M_Y` has weighted-Jaccard overlap with `M_X` ≥ 0.6, at a leaf whose RSSI is within ±5 dB of `X`'s last RSSI
- **Then** MAC `Y` is added to cluster `C` (not a new cluster), `cluster_member.join_reason='payload'`, and one `fingerprint_event` row exists per matched payload hash for both MACs

**Scenario 3: API answers "who is currently here"**
- **Given** the classifier has been running for ≥ 1 hour and at least one cluster has activity in the last 60 s
- **When** a client calls `GET /api/persons/current`
- **Then** the response is a JSON array with one entry per active cluster, each entry containing `cluster_id`, nearest `leaf`, `last_seen` (ISO 8601), best `rssi`, and `join_confidence_min` (minimum confidence among contributing members)
- **And** the response returns in ≤ 200 ms with the DB at current size (≥ 5 M obs rows — extrapolated from current ~110 obs/s)

**Scenario 4: Empty house**
- **Given** no BLE observations in the last 5 minutes from any non-stationary device
- **When** `GET /api/persons/current` is called
- **Then** the response is `[]` (empty array, not an error)

**Scenario 5: Stationary devices do not become persons**
- **Given** a MAC has been observed with < 3 dB RSSI variance from a single leaf for > 24 hours (e.g. a smart bulb)
- **When** clustering runs
- **Then** that MAC is tagged `stationary=1` in `device_cluster` and excluded from `person_appearance` rollups (but still tracked in `cluster_member`)

**Scenario 6: Find-My payloads do not produce spurious bridges**
- **Given** two genuinely distinct Apple devices each emit `protocol_type=0x12` adverts whose payload hashes happen to collide (see Finding 3 — the EDA found 10 distinct MACs sharing one such hash)
- **When** the classifier evaluates a payload-based join
- **Then** the join is rejected because `0x12` is excluded from the multiset; the two MACs remain in separate clusters
- *(This is a regression-prevention scenario based on the EDA finding, not a new behavior to invent.)*

**Scenario 7: Phase 2 join recall on the held-out capture**
- **Given** Capture B (≥ 4 hours, labeled rotations of ≥ 3 known devices) is replayed through the classifier with parameters frozen from Capture A tuning
- **When** the run completes
- **Then** the classifier achieves rotation-join recall ≥ 0.85 and precision ≥ 0.90 (lower-bound of bootstrap 95% CI), and the eval report is committed at `docs/test/person-fingerprint-eval-2026-05-25.md`
- *(This is the single-shot eval that decides ship/no-ship.)*

## Acceptance criteria flagged in the original draft as not measurable against the DB

For the record (Sophie's review aid), here is what changed from the 2026-05-13 draft and why:

| Original wording | Issue | Fix |
|---|---|---|
| "≥ 90% of MAC rotations land in the same cluster within 30 minutes" | No definition of "MAC rotation" event in the DB; no held-out source for ground truth; no statistical envelope. | Defined via Capture B + per-device labels; replaced 30 min with 15 min = one rotation cadence; added bootstrap CI. |
| "matching `protocol_type=0x10` with `payload_hash=H`" | EDA Finding 4: a single device emits a multiset of payload hashes — exact-match join would miss most rotations. | Changed to weighted-Jaccard on a 90 s multiset. |
| "RSSI within ±5 dB" (Scenario 2) | Underspecified — at which leaf? aggregated how? | Specified: at the leaf where `X` was last seen. |
| "Jaccard overlap ≤ 15% across 5 labeled sessions" (metric 2) | No protocol for what counts as a "session" or how labels are collected. | Capture A/B protocol above; ≥ 5 min minimum, ≥ 2 known people in different rooms. |
| Stationary detection: "< 3 dB RSSI variance for > 24 hours" | The 24 h floor is unreachable in a 4 h held-out window. | Kept the operational definition unchanged; added Scenario 5 explicitly to the held-out eval but with the relaxed condition that *all* obs of the device in the window meet the variance bound (smaller-sample equivalent). |

## Open questions

- ~~Which clustering algorithm — online DBSCAN vs. greedy bucket-merge?~~ **Resolved 2026-05-14:** start from the existing `mockingbird_tracks._cluster_entities` union-find (Requirement 6). It's already in production for the dashboard's entity view and has shipped against live traffic for weeks. Phase 1 lifts-and-shifts it; we revisit DBSCAN only if Capture B metrics fall short.
- **Privacy: hash MACs at rest?** **Owner: Sophie + Noah, decide by 2026-05-18.** Lean: yes, salted SHA-256, salt in `.scratch/`. Adds ~10 ns per row, no downside for our query patterns since we never need to recover plaintext MAC from the DB.
- ~~How do we test scenario 2 without staging a real MAC rotation?~~ **Owner: Andy, replay harness `tools/replay_obs.py` by 2026-05-22.** Spec: takes a `.sqlite` capture, streams rows to the classifier in pseudo-real-time (configurable speedup), optionally injects synthetic rotations.
- **NEW: should we exclude `0x12` (Find My) from fingerprinting entirely or also from the `fingerprint_event` table?** Lean: write the row but flag it; downstream queries skip `protocol_type=0x12`. Owner: Wanjiru, decide by 2026-05-18.
- **NEW: capture instrumentation on Noah's phone — Shortcuts loop or a TestFlight app?** Owner: Ethan, decide by 2026-05-17 (blocks Capture A start).
- **NEW: handling of non-Apple manufacturer payloads — discard, hash-only, or parse opportunistically?** EDA shows `0x0600` (Microsoft) is 28% of non-Apple traffic with apparent stable structure. Out of scope for v1; noted for v1.1.

## Timeline & owners

- **Pre-lock review with Sophie:** 2026-05-18 — Wanjiru presents this revised draft + EDA, Sophie + Noah resolve open questions.
- **Spec lock:** 2026-05-20 — Sophie.
- **Capture A (calibration):** 2026-05-15 – 2026-05-17 — Wanjiru, blocks on Ethan's phone-MAC logger by 2026-05-17.
- **`apple_continuity.py` parser:** 2026-05-24 — Ethan. Unit tests against ≥ 100 payloads pulled from the snapshot DB.
- **Refactor `mockingbird_cluster.py` from tracks:** 2026-05-26 — Wanjiru.
- **Classifier daemon v1 (Phase 1 co-occurrence only):** 2026-05-31 — Wanjiru.
- **Replay harness `tools/replay_obs.py`:** 2026-05-22 — Andy.
- **Fingerprint join (Phase 2):** 2026-06-07 — Wanjiru.
- **Capture B + single-shot eval:** 2026-06-08 – 2026-06-09 — Wanjiru; eval report committed.
- **API + integration tests:** 2026-06-10 — Ethan + Andy.
- **Ship to `main`:** 2026-06-14.

## Rough estimate

**T-shirt: L.** Reasoning: parser is small (S), online clustering with correctness guarantees is M-L on its own, plus a new daemon + schema + API + the test harness for MAC-rotation scenarios + a non-trivial evaluation effort (two captures + a replay tool + a reliability calibration). Two engineers, ~4 calendar weeks. Risk: Capture A turns up payload behavior that contradicts the EDA's 9-minute window (e.g., overnight payload-hash churn we didn't see) — mitigation is that Capture A happens first and tuning can be re-run cheaply.

---

## Appendix — EDA reproduction

To regenerate the numbers in the EDA section against any future capture:

```python
# scripts/eda/p7_apple_continuity_breakdown.py  (to be committed alongside)
import sqlite3, collections, hashlib
con = sqlite3.connect('observations.sqlite')
cur = con.cursor()

# (1) sub-protocol byte distribution
apple_subproto = collections.Counter()
for (manuf,) in cur.execute("SELECT manuf FROM obs WHERE substr(manuf,1,4)='4c00'"):
    if len(manuf) >= 6:
        apple_subproto[manuf[4:6].lower()] += 1
print(apple_subproto.most_common())

# (2) payload multiset per MAC for sub-protocol 0x10
by_mac = collections.defaultdict(collections.Counter)
for ts, mac, manuf in cur.execute("SELECT ts, mac, manuf FROM obs WHERE substr(manuf,1,6)='4c0010'"):
    by_mac[mac][hashlib.sha1(manuf[6:].encode()).hexdigest()[:8]] += 1
# unique payloads-per-MAC histogram + cross-MAC hash sharing
```
