# Q3 PRD spec-lock readiness review

**Date:** 2026-05-14
**Spec-lock:** 2026-05-20 (Monday 10:00 review on 2026-05-18 — 4 working days away)
**Reviewer:** Sophie Lefèvre

---

## 1. `person-fingerprinting-phase-1-2.md` (Wanjiru)

### Open questions still unresolved (per PRD)
- Clustering algorithm: online DBSCAN vs greedy bucket-merge — **Wanjiru, due 2026-05-20**. Cutting it fine; need a decision Monday at review.
- MAC hashing at rest — **Sophie + Noah, due 2026-05-20**. I will close this with Noah by EOD 2026-05-15.
- Replay harness for MAC rotation — **Andy, due 2026-05-22**. *After* spec-lock; acceptable, but means Scenario 2 cannot be verified pre-lock. Flag as a known risk, don't block lock.

### Acceptance-criteria gaps
- **Success metric #1 is partially untestable at spec-lock.** "≥90% of MAC rotations land in same cluster within 30 min" — there is no ground-truth source defined. Need a labeled rotation log to measure against. Wanjiru must specify: who labels (Noah?), how (manual phone-MAC capture? Bluetooth-scanner-as-oracle?), retention.
- **Success metric #2 ("Jaccard ≤ 15% between known people")** — "5 labeled sessions" — sessions of what duration, with how many people, in which rooms? Underspecified.
- **Requirement #2 (`payload_hash`)** does not specify the hash function or which auxiliary fields go into it. This is the load-bearing piece of Phase 2; if fields are wrong, the fingerprint is unstable. Wanjiru must enumerate fields (with byte offsets per Apple sub-protocol) before lock.
- **Requirement #5 (HTTP API)** lacks auth posture. Tailnet-only? Token? Open? Decide before lock.

### Missing BDD scenarios
- **No negative scenario for fingerprint collisions** — what if two physical devices emit identical `payload_hash`? Need: Given two distinct physical devices with colliding hashes / When clustering runs / Then they are NOT merged (e.g. RSSI-signature divergence wins).
- **No scenario for cluster splitting** — if a previously-clustered MAC is later observed at a leaf 20m away from the rest of the cluster for 10 min, does it split? Needs explicit BDD.
- **No scenario for daemon restart / state recovery** — Given the classifier daemon crashes / When it restarts / Then it resumes from the last committed cluster state without re-clustering from t=0.
- **No scenario for nightly re-cluster correcting drift** (Requirement #4 references it but has no acceptance criterion).

### Verdict: **AT RISK** for 2026-05-20.
Two of the three open questions are scheduled to resolve on the lock date itself. Three success-metric specifications are too loose to write tests against. **Recommendation:** Wanjiru must surface algorithm decision + payload_hash field list by Friday 2026-05-16 so Monday's review can close them. If not, push spec-lock for this PRD to 2026-05-22 and let the other two land on time.

---

## 2. `imu-on-leaves.md` (Puru + Ethan)

### Open questions still unresolved
- MPU6050 vs LSM6DSO — **Puru, due 2026-05-20**. Blocks hardware order (which I committed to placing by 2026-05-15 — **tomorrow**). This is a hard blocker on my own deliverable.
- 10 Hz vs 25-50 Hz transient sampling — **Ethan, prototype by 2026-05-24**. *After* lock — acceptable since it's a tuning decision, not a contract decision.
- Antenna gain-pattern data source — **Puru + Eszter, due 2026-05-22**. *After* lock. Acceptable; firmware/schema work doesn't depend on knowing the pattern, only the analyzer does.

### Acceptance-criteria gaps
- **Success metric #1 ("σ from 23 cm to ≤18 cm")** — needs a defined test corpus. "Same 5-minute test capture" is mentioned but the capture itself doesn't exist yet (or isn't pinned). Without a frozen baseline corpus, this metric is unmeasurable. Eszter to pin a corpus before lock.
- **Success metric #2 ("≤1 false positive per leaf per week")** — under "normal household vibration." Define normal: cat jumping on shelf, HVAC vibration, door slam? Need a vibration profile or accept that the first week of deployment IS the calibration.
- **Requirement #3 (calibration)** — does not define what happens if calibration is triggered while the leaf is moving (operator error). Should reject with 4xx and a clear message.
- **Requirement #5 (gain correction)** — depends on data we don't have yet (open question #3). Lock the *interface* (function signature, fallback isotropic table) even if the lookup table is empty at lock.

### Missing BDD scenarios
- **No scenario for re-calibration after tamper** — Scenario 3 disables a leaf on tamper; there's no acceptance criterion for the recovery path. Add: Given a leaf is flagged tampered / When the operator re-runs calibration / Then `disabled=false` and the leaf rejoins fusion.
- **No scenario for IMU detected but failing** — I²C is present but returns garbage. Need a health-check threshold and the analyzer behavior (treat as `imu_absent`?).
- **No scenario for time alignment** — IMU samples at 10 Hz, BLE obs are irregular. Define how the analyzer joins an IMU sample to a BLE obs (nearest, interpolate, latest-before)?
- **No scenario for storage budget** — 10 Hz × 8 leaves × 24h × 7 days × ~40 bytes/row ≈ 1.9 GB/week if we never prune. Pi SD is 64 GB. Need retention policy in the PRD.

### Verdict: **ON TRACK** but with one hard blocker.
PRD is the cleanest of the three. The MPU6050 vs LSM6DSO decision **must** land tomorrow (2026-05-15) — I have a hardware-order commitment for that date. If Puru can't decide by EOD 2026-05-15, I'll order MPU6050 (cheap enough that wrong-choice waste is acceptable) and document the assumption.

---

## 3. `presence-anomaly-alerts.md` (Ethan)

### Open questions still unresolved
- Pushover vs ntfy.sh vs HomeAssistant — **Sophie + Noah, due 2026-05-20**. I own this; will close with Noah by EOD 2026-05-15.
- Empty-house dependency on Phase 3 vs Phase 1 — **Wanjiru, propose by 2026-05-22**. *After* lock; acceptable, lean is documented (Phase 1 sufficient).
- Replay harness — **Andy, due 2026-05-22** (shared with PRD #1). After lock, acceptable.

### Acceptance-criteria gaps
- **Success metric #1 ("≥80% useful-alert ratio")** — denominator is "alerts fired in 2-week trial." If the trial fires very few alerts (quiet house), the ratio is noisy. Need a minimum-alert-count floor before the ratio is meaningful (say ≥ 20 alerts).
- **Requirement #4 (config file)** — no schema. Provide a minimal example YAML in the PRD so Ethan isn't inventing the schema during build.
- **Requirement #5 (feedback URL)** — `https://mockingbird-pi/api/feedback/...` requires TLS on the Pi. Today the Pi serves HTTP on `:8080` for the dashboard. Either define a TLS plan (caddy? tailscale serve?) or accept HTTP-over-tailnet and document the decision.
- **Requirement #6 (quiet hours + severity)** — "tamper is critical; arrival is not" is in prose but not in a structured per-rule severity table. Add a table mapping rule → severity → quiet-hours-bypass.

### Missing BDD scenarios
- **No scenario for transport failure** — Pushover API is down or returns 5xx. Retry policy? Dead-letter? Operator notification of degraded alerting? Without this, silent failures will erode trust.
- **No scenario for clock skew / quiet-hours boundary** — what if local time is 21:59 when the event fires and 22:01 when the push lands? Define: evaluation timestamp = event detection time, not delivery time.
- **No scenario for cooldown override** — Requirement #4 mentions `cooldown_seconds`. What if a critical event fires during a cooldown of a same-rule alert? Critical should bypass; needs BDD.
- **No scenario for "leaf back online"** — Scenario 6 mentions it in an "and" clause but it deserves its own scenario with its own success criteria (debounce on flapping leaves).
- **Latency metric** (success #2) has no acceptance scenario. Given a synthetic event written to DB / When the alerter sweep runs / Then a push request is submitted to the transport within X seconds.

### Verdict: **ON TRACK**, conditional.
Lock is achievable IF the transport decision lands by Monday's review (I own it). Calendar-blocked by the two upstream PRDs regardless, so spec-lock slippage on this one hurts less — but DO lock it on schedule so Ethan can start the skeleton.

---

## Bottom line

| PRD | Spec-lock 2026-05-20 | Blocker(s) |
|---|---|---|
| Person fingerprinting (Wanjiru) | **AT RISK** | Algorithm + payload_hash field list, both due on lock date. Success metrics too loose to write tests. |
| IMU on leaves (Puru + Ethan) | **ON TRACK** | MPU6050 vs LSM6DSO decision needed 2026-05-15 to unblock my hardware order. |
| Presence & anomaly alerts (Ethan) | **ON TRACK** | Transport decision (Sophie owns). Calendar-blocked by the two above regardless. |

## Sophie's actions

- **2026-05-15 (tomorrow):** Close transport decision with Noah; close MAC-hashing-at-rest decision with Noah. Place IMU order (MPU6050 default if Puru hasn't decided).
- **2026-05-15:** Send each owner their gap list above, ask for written reply by EOD 2026-05-17.
- **2026-05-18 Monday 10:00 review:** Drive each PRD owner to close their open questions live. Walk away with locked specs or a written deferral.
- **If person-fingerprinting is not lockable on 2026-05-20:** push it to 2026-05-22, no further. Document the slip and the cause.
