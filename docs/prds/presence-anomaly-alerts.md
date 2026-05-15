# PRD: Presence & anomaly alerts (push notifications)

**Status:** Draft
**Owner:** Ethan (software engineering) — primary; Wanjiru consulting on baselines
**Author:** Sophie Lefèvre
**Date:** 2026-05-13

## Problem

The mesh produces continuous, accurate household telemetry, and nobody is reading it. Until the system can interrupt the operator with information they actually want (someone arrived, nobody is home, an unfamiliar device is in the kitchen at 3 AM), all that beautiful fused localization is just a dashboard nobody looks at. We need a thin, opinionated alerting layer that turns the existing `person_appearance` data (and, with IMU, `leaf_event` tamper data) into push notifications. This is the capability that makes a non-technical household member say "huh, that's useful" instead of "what's that map for." Qui vivra verra — but only if we ship the alerts.

## Goals

- Push notifications to Noah's phone for a small, opinionated set of household events.
- Configurable per-event-type enable/disable and quiet hours.
- Sub-30-second latency from event detection to phone vibration.

## Non-goals

- Building our own mobile app — use Pushover, ntfy.sh, or HomeAssistant companion app.
- Two-way interaction (acknowledging alerts, snoozing from the phone) — v2.
- Alerts for visitors / strangers — depends on Phase 3 person labeling, separate PRD.
- Routing to multiple recipients — single-user (Noah) for v1.

## Users / personas

- **Noah (sole subscriber, v1):** wants ambient awareness without staring at a dashboard.

## Success metrics

1. **Useful-alert ratio:** ≥ 80% of fired alerts are rated "useful, send more like this" by Noah over a 2-week trial period. Tracked via a one-tap thumbs-up/down in the notification payload, captured to a `feedback` table.
2. **Latency:** P95 from event detection (DB write) to phone receipt ≤ 30 seconds.

## Requirements

1. **Alerter daemon** (`services/mockingbird-alerter.py`, systemd unit) tails the DB and matches against a rule set.
2. **Rule set v1** (exactly these four event types, no more):
   - **Arrival:** a known cluster appears after ≥ 30 minutes of absence.
   - **Departure / empty house:** no known clusters seen for ≥ 10 minutes, after ≥ 1 hour of someone being home.
   - **Leaf tamper:** any `leaf_event` of `kind="tamper"` (depends on [[imu-on-leaves]]).
   - **Leaf offline:** a leaf's heartbeat is stale > 5 minutes.
3. **Transport:** Pushover (HTTPS POST to api.pushover.net). API token + user key in `~/repos/.scratch/pushover-creds.txt`.
4. **Config:** `~/mockingbird/alerts.yaml` with per-rule `enabled`, `quiet_hours_local: ["22:00","07:00"]`, `cooldown_seconds`.
5. **Feedback ingest:** Pushover supplementary URL points at `https://mockingbird-pi/api/feedback/<alert_id>/<thumbs>` (over tailnet); writes to `feedback` table.
6. **Quiet hours:** any rule with `respect_quiet_hours: true` is suppressed during the window unless `severity: critical` (tamper is critical; arrival is not).

## Acceptance criteria (BDD)

**Scenario 1: Arrival fires once**
- **Given** cluster `C` has had no `person_appearance` row for ≥ 30 minutes
- **When** a new `person_appearance` row for `C` is written
- **Then** within 30 seconds an "Arrival" push lands on Noah's phone identifying the cluster and the nearest leaf
- **And** the alerter does not fire a second arrival alert for `C` until it has been absent for another 30 minutes

**Scenario 2: Quiet hours suppress non-critical alerts**
- **Given** the local clock is 02:30 and arrival has `respect_quiet_hours: true`
- **When** an arrival event would normally fire
- **Then** no push is sent
- **And** a row is written to `alerts_suppressed` so the operator can review what was muted

**Scenario 3: Tamper bypasses quiet hours**
- **Given** the local clock is 02:30 and a `leaf_event` of `kind="tamper"` is written
- **When** the alerter sees the event
- **Then** a "Leaf tampered" push fires immediately regardless of quiet hours, marked `critical`

**Scenario 4: Empty-house alert is debounced**
- **Given** a household member has been home for ≥ 1 hour
- **When** all known clusters disappear from observations for ≥ 10 minutes
- **Then** exactly one "House is empty" push is sent
- **And** if a cluster reappears within 60 seconds of the empty-house determination, the alerter cancels the alert if not yet sent, or sends a corrective "False alarm — Noah is back" push if already sent

**Scenario 5: Feedback loop closes**
- **Given** Noah received an alert with `alert_id=42`
- **When** Noah taps the thumbs-up supplementary URL
- **Then** a row appears in the `feedback` table with `alert_id=42, rating="up", ts=...`
- **And** the weekly summary email/log includes the useful-alert ratio for the trailing 7 days

**Scenario 6: Leaf offline**
- **Given** leaf `mockingbird-4ce184` has not sent a heartbeat in 5+ minutes
- **When** the alerter's 60s sweep runs
- **Then** a "Leaf offline: mockingbird-4ce184" push is sent (severity warning, respects quiet hours)
- **And** when the leaf reconnects, a follow-up "Leaf back online" push is sent

## Design (Ethan, 2026-05-14)

Owning this end-to-end. Pinning the four things Sophie asked me to nail
down: alert sources, anomaly model, notification path, service ownership.

### Alert sources — exactly four event signals, all already in the DB

Every rule fires off one of four signals. Three are existing rows; one
is derived. No new event production paths, only consumption.

| Signal | Source | Latency floor |
|---|---|---|
| `person_appearance` row inserted | `services/mockingbird_tracks.py` writes these when entity clustering promotes a multi-MAC track to a stable identity | ~1-2 s after BLE obs |
| `person_appearance` row absent for ≥ N min | derived: `SELECT MAX(ts) FROM person_appearance WHERE cluster_id=?` polled every 30 s | poll period (30 s) |
| `leaf_events` row with `event='tamper'` | depends on [[imu-on-leaves]] | ~3 s (tamper detector window) |
| Leaf heartbeat staleness | derived: `SELECT MAX(ts) FROM leaf_events WHERE leaf=? AND event='hb'` polled every 60 s | poll period (60 s) |

The alerter is a **pure SQL consumer** — it does not subscribe to the
TCP stream, hook the collector, or share any process state. This is
the right architectural call for three reasons:

1. **Crash isolation.** The collector's job is "don't lose
   observations." A buggy alerter must not be able to block, slow, or
   crash the collector. Separate process + DB-only IPC gives us that
   for free.
2. **Idempotent replay.** Watermark stored in the alerter's own state
   table (`alerter_watermark(rule, last_ts)`). On restart, replay from
   watermark forward. If an alert was already sent, the `cooldown`
   check de-dupes it. Cost: one `SELECT` per rule per tick.
3. **Trivial to test.** Feed it a synthetic SQLite file, assert on the
   `alerts_sent` table and the (mocked) Pushover POST. No need for a
   live mesh in CI.

### Anomaly model — start dumb, earn complexity

V1 is **rule-based, not learned.** Four hand-tuned thresholds:

```
ARRIVAL          : person_appearance after ≥ 30 min absence
DEPARTURE        : no person_appearance in any cluster ≥ 10 min,
                   after ≥ 1 h of presence
TAMPER           : any leaf_event kind='tamper'  (critical, bypasses quiet hours)
LEAF_OFFLINE     : no hb in 5 min  (warning, respects quiet hours)
```

I considered three more sophisticated models and explicitly rejected
them for v1:

- **Per-cluster Poisson presence model with a Bayesian threshold.**
  Right answer for "is this arrival time unusual?" (e.g. Noah is
  home at 3 AM on a Tuesday — anomaly), but it needs ≥ 2 weeks of
  labeled data per cluster to fit. We don't have that yet. Defer to
  v2 once the 2-week trial is logged.
- **HMM over presence/absence with state-dependent emission rates.**
  Strictly better for empty-house debouncing than the 10-min
  threshold, but the threshold is good enough for v1 and the HMM
  needs labeled training data we don't have.
- **Anomaly detection on dwell-location distribution per cluster.**
  Cool but premature — v1's value is "you know when people arrive
  and leave," not "you know when their routine deviates."

The architectural punchline: the rule-evaluation function takes a
`(rule_config, db_snapshot, now) -> list[Alert]` shape. Swapping the
threshold for a learned model in v2 is one function replacement, no
plumbing churn.

### Notification path — Pushover for v1, abstraction-thin so it isn't sticky

Three transport options were on the table:

| Transport | Pros | Cons |
|---|---|---|
| **Pushover** | Rock-solid delivery, supplementary URLs for feedback loop, one-time $5/platform, no auth dance | Vendor lock-in, costs money |
| **ntfy.sh** (self-hosted on Pi) | Free, OSS, runs locally | Self-hosted = another systemd unit, another point of failure on the Pi; iOS push needs paid forwarder anyway |
| **HomeAssistant companion** | Free, OSS, already on Noah's phone presumably | Forces installing HA, more moving parts than alerts deserve |

**Recommendation: Pushover.** $10 one-time (iOS + Android), survives
Pi reboots and tailnet hiccups (Pushover's servers do the retry, not
us), and the supplementary URL feature is exactly the feedback loop
we need. Worth $10 to not own a push gateway.

Abstraction shape: `services/mockingbird_alerter/transports.py`
exposes a `Transport.send(alert) -> delivery_id` protocol. Pushover is
one implementation, ntfy.sh is a 30-LOC fallback we keep stubbed but
disabled. Swapping is one config-line change.

The HTTPS POST to api.pushover.net **MUST egress through the Pi's
direct internet route, not the tailnet**. The tailnet is for inbound
(Noah's phone tapping the supplementary URL); outbound to Pushover
goes via the Opal's WAN to entropy. Verified in the topology in
CLAUDE.md.

### Service ownership — new `mockingbird-alerter.service` systemd unit

New systemd unit `services/mockingbird-alerter.service`. Python entry
at `services/mockingbird-alerter.py`. Mirrors the collector's
hardening choices (MAC-2 conventions):

```
[Service]
Type=simple
ExecStart=/usr/bin/python3 -u /home/pi/mockingbird/services/mockingbird-alerter.py
Restart=on-failure
RestartSec=5
StartLimitIntervalSec=60
StartLimitBurst=5
MemoryHigh=64M
MemoryMax=96M
OOMScoreAdjust=-300
StandardOutput=journal
StandardError=journal
```

Memory budget is half the collector's (collector handles 100+ obs/s of
live TCP; the alerter polls SQL every 30 s and does a few HTTPS
POSTs/day). The crash-loop guard is tighter (5 restarts in 60 s, vs
the collector's 5 in 5 min) because if the alerter is crashlooping,
silence is correct behavior — the collector keeps recording, we just
miss notifications until human intervention. **The alerter must never
take the collector down with it.** Separate process is the structural
guarantee of that.

DB access is **read-only with shared cache**, two-second busy timeout:

```python
sqlite3.connect(f"file:{db_path}?mode=ro&cache=shared", uri=True, timeout=2.0)
```

This means the alerter cannot accidentally write to the obs table or
hold a write lock that stalls the collector. Its own state
(`alerter_watermark`, `alerts_sent`, `feedback`, `alerts_suppressed`)
goes in a separate SQLite file `~/mockingbird/alerter.sqlite` —
small, blast-radius-limited, can be wiped to reset state without
touching observations.

### Schema additions (in `alerter.sqlite`, not `observations.sqlite`)

```sql
CREATE TABLE alerter_watermark (rule TEXT PRIMARY KEY, last_ts REAL NOT NULL);
CREATE TABLE alerts_sent (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    rule TEXT NOT NULL,
    severity TEXT NOT NULL,            -- 'info' | 'warning' | 'critical'
    cluster_id TEXT,                   -- nullable, populated for arrival/departure
    leaf TEXT,                         -- nullable, populated for tamper/offline
    body TEXT NOT NULL,                -- the human-readable message
    delivery_id TEXT,                  -- Pushover receipt
    cancelled INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_alerts_rule_ts ON alerts_sent(rule, ts);
CREATE TABLE alerts_suppressed (
    ts REAL NOT NULL, rule TEXT, reason TEXT, body TEXT
);
CREATE TABLE feedback (
    alert_id INTEGER NOT NULL,
    rating TEXT NOT NULL,              -- 'up' | 'down'
    ts REAL NOT NULL,
    PRIMARY KEY (alert_id, ts)
);
```

The feedback HTTP endpoint (Scenario 5) does **not** go on the
alerter — it goes on `mockingbird-dashboard.service` as a new
`POST /api/feedback/<alert_id>/<thumbs>` handler that writes to
`alerter.sqlite`. The dashboard already terminates HTTPS via
tailscale-serve and already has tailnet auth; making the alerter
listen on a port would double the attack surface for no benefit.

### Latency budget

P95 target: ≤ 30 s from event detection to phone vibration. Decomposed:

| Stage | Budget | Notes |
|---|---|---|
| Event row written → alerter SELECT sees it | ≤ 30 s | poll period for live-row rules; 60 s for derived "absence" rules |
| Rule evaluation + cooldown check | ≤ 10 ms | in-memory dict + one indexed SELECT |
| Pushover HTTPS POST | ≤ 1.5 s | observed P95 from Pi over entropy WAN; measured during dashboard work |
| Pushover → phone push | ≤ 5 s | Pushover's SLA |
| **Total P95** | **≤ ~40 s** for derived absence rules; **~15 s** for live rules |

The 30 s target is achievable for arrival/tamper (live rules) but
**tight for empty-house/leaf-offline** (derived from absence). I'd
rather meet 15 s on the rules people actually care about (arrivals,
tamper) than miss it across the board chasing 30 s. Calling this a
documented trade-off, not a missed target — Sophie to ratify in PRD
review.

## Open questions

- Pushover vs ntfy.sh vs HomeAssistant — recommending **Pushover** per the
  notification-path analysis above. **Owner: Sophie + Noah, ratify by 2026-05-20.**
- Do empty-house alerts depend on Phase 3 person labels? Phase 1 cluster-level
  "any-known-cluster present" is sufficient for v1; visitor-driven false
  positives are acceptable in the 2-week trial because they generate the
  feedback signal we need. **Owner: Wanjiru, ratify by 2026-05-22.** Lean: yes.
- How do we test arrival without staging a real walk-out / walk-in? **Owner:
  Andy (SDET), propose a replay harness by 2026-05-22** — replaying a saved
  observations.sqlite snapshot through the alerter with mocked Pushover is
  the cheap path (the alerter is already a pure SQL consumer by design, so
  this is just "point it at a different DB file").

## Timeline & owners

- **Spec lock + transport decision:** 2026-05-20 — Sophie
- **Alerter daemon skeleton + Pushover integration:** 2026-05-27 — Ethan
- **Rules 1-4 implemented:** 2026-06-10 — Ethan
- **Feedback ingest + weekly summary:** 2026-06-14 — Ethan
- **2-week trial begins:** 2026-06-17
- **Trial results + go/no-go on v2:** 2026-07-01 — Sophie

**Dependency:** rules 1, 2, and the empty-house rule depend on [[person-fingerprinting-phase-1-2]] landing first. Rule 3 depends on [[imu-on-leaves]]. Rule 4 (leaf offline) has no dependencies and can ship standalone — recommend gating the trial on rules 1+2+4 minimum.

## Rough estimate

**T-shirt: S-M.** Reasoning: thin layer on top of two larger PRDs. The daemon itself is small (state machine over a SQL view), Pushover integration is trivial, the test harness is the real cost. One engineer, ~2 calendar weeks of focused work — but calendar-blocked by the two upstream PRDs. Schedule risk is dependency slippage, not effort.
