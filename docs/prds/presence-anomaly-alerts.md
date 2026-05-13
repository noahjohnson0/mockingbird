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

## Open questions

- Pushover vs ntfy.sh vs HomeAssistant — Pushover is paid but rock-solid; ntfy.sh is free + open-source. **Owner: Sophie + Noah, decide by 2026-05-20.**
- Do empty-house alerts depend on Phase 3 person labels (so we don't alarm on every visitor leaving)? Or does Phase 1 cluster-level "any-known-cluster present" suffice? **Owner: Wanjiru, propose by 2026-05-22.** Lean: Phase 1 is enough for v1.
- How do we test arrival without staging a real walk-out / walk-in? **Owner: Andy (SDET), propose a replay harness by 2026-05-22** (likely shared with [[person-fingerprinting-phase-1-2]]).

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
