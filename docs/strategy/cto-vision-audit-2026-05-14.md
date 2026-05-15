# CTO vision audit — Mockingbird

**Author:** Anthony Zhang, CTO
**Date:** 2026-05-14
**Audience:** Noah (self), shared with Vlad on request
**Scope:** 18-month architectural, organizational, and pitch-credibility read

Bottom line up front:

- **The substrate is right for the next 6 months, wrong for the next 18.** The Pi Zero W + single-AP + SQLite stack will hit a wall once IMU streams + audio land, and the failure will look like sensor data loss, not a crash. Plan the inflection now; execute it at the end of Q3.
- **The missing hire is a privacy/security engineer with a regulatory bent.** Not a SecOps person — a product-privacy person. Every capability on the roadmap is a deposition exhibit waiting to happen, and we have nobody whose job it is to own that surface.
- **The undefendable pitch slide is the accuracy/coverage claim.** "23 cm σ entity fusion" and "±20 % distance with IMU" are both true in our living-room and provably non-generalizable. A sharp investor with a sensor background will eat that slide.

Detail follows.

---

## 1. Architecture sanity-check

### What's working

- Streaming obs → collector → SQLite is the right shape. It's stateless on the leaves (queue + counters, no on-device accumulation since v0.3.0), which means leaves can die and reboot without poisoning state. That's the single best thing about the current design and we should defend it.
- Pi as the central aggregator is fine *today* — ~110 obs/sec aggregate, no drops, ~125 KB free heap on leaves. We have headroom on the wire and on the leaves.
- Tailscale subnet router on the Pi (not the Opal) was the correct call after the flash-budget reality check. One Tailscale identity to manage, one place to enforce ACLs, one place to roll keys.

### Where it breaks first — ranked by how soon

**1. Pi compute, when IMU lands. ~Q3 end.**

Math: 8 leaves × IMU at even a modest 50 Hz × 6 floats = 2,400 samples/sec on top of the current ~110 BLE obs/sec. That's a 20× write amplification into SQLite. The Pi Zero W is a single-core ARMv6 at 1 GHz with 512 MB RAM and a class-10 SD card whose sustained random-write IOPS is maybe 200–400. SQLite in WAL mode buys us batching, but `mockingbird-collector.py` is a single Python process doing line-by-line JSON parsing on a single core. That core is the first thing to saturate.

Symptom you'll see: collector lag → backpressure on the TCP write side of the leaves → leaves drop observations silently (the 64-entry queue overflows). Andy's wire-contract tests won't catch this because the leaves *did* the right thing. The Pi just stopped listening fast enough.

**2. SQLite write throughput + query interference, when classifier/dashboard tail the DB hot. ~6 months.**

The classifier daemon, the dashboard, the analyzer, and the collector are all hitting the same SQLite file. WAL mode handles reader/writer concurrency well, but checkpoints stall everything for hundreds of ms on a Class-10 SD card. Once the DB grows past ~2–3 GB and queries start scanning more than a few minutes of observations, your dashboard latency goes from "feels live" to "feels broken."

**3. Single-AP WiFi as the bus. ~12 months, when you scale past ~15 leaves OR add audio.**

2.4 GHz on the Opal, one channel, shared with every BLE radio time-slicing on the same band. Audio leaves at 16 kHz mono are ~256 kbps each — modest, but they're streaming continuously instead of bursting like BLE obs do. Five audio leaves saturate the AP's airtime budget faster than the bitrate math suggests, because every transmission contends with every BLE scan window.

**4. SD card wearout. ~18–24 months.**

Continuous write workload on consumer flash. The 64 GB card is probably a TLC consumer part with maybe 1,000 P/E cycles. We're writing ~10–50 MB/day today, much more once IMU lands. Plan a replacement schedule or move the DB to an external SSD over USB before this bites.

### The next-architecture inflection point

Two-tier, in order of when you'll need each:

**Tier 1 (do this at end of Q3, before audio):** Move the Pi off the Zero W onto a Pi 5 (or comparable — Radxa Rock 5B if you want more I/O). Same software stack, same physical role, ~10× the compute, dual USB 3.0 for an external SSD. Cost: $80 + SSD. Risk: low — it's the same Debian, same systemd units, same Python.

The Zero W stays in inventory as a known-good fallback / cold spare.

**Tier 2 (do this when you cross 15 leaves OR commit to audio):** Split the collector from the analytics. Collector stays on the Pi (low-latency ingestion, hot ring buffer in a small SQLite). Analytics moves to `svr` over the tailnet — the RTX 4070 is overkill for SQLite but exactly right for the ML side (fingerprinting models, audio classifiers, anomaly detection). Stream observations to svr as a logical replica; let the Pi be a write-through cache.

This also gives you a clean answer to the "what if the Pi dies" disaster scenario, which today is "everything stops" and tomorrow needs to be "ingestion stops, analytics keeps serving."

**Don't do** a Kafka / Redis Streams / proper event-bus rewrite. The line-delimited-JSON-over-TCP wire format is the right level of abstraction for this scale; don't let architecture astronauts (or me on a bad day) talk you into adding a broker. The substrate works. Scale the boxes, not the protocol.

---

## 2. Team formation gap

**The missing hire: a privacy / product-security engineer.**

Not Macca (SRE — keeps the boxes up). Not Andy (SDET — keeps the code correct). The role I mean is the person who owns:

- The data-minimization policy: what we store, for how long, with what retention
- The threat model document for "what happens when the Pi is stolen / the tailnet key leaks / a household member wants their fingerprint deleted"
- The MAC-rotation and Apple Continuity work from an "are we adversarial to Apple's privacy stance?" angle, not just a technical-decoder angle
- The audit log for every read of identity data, so we can answer "who looked at Noah's location, when"
- Regulatory readiness: GDPR (if EU customers ever), CCPA (California sales), BIPA if we ever touch biometrics — and gait fingerprinting is arguably biometric under BIPA
- The privacy claims in the pitch deck — they need to be true, not aspirational

Why this and not, say, an ML/MLOps hire or a hardware EE: every other capability gap on the roadmap can be filled by the existing team stretching. Wanjiru can grow into MLOps. Puru can grow into more EE. Eszter can cover applied math indefinitely. The privacy work has *no* growth path from anyone currently on the roster, because it's not a skill adjacent to anyone's seat — it's a discipline. And the moment we have a paying customer, or a press cycle, or a household member who asks "what do you know about me," the absence of this person becomes visible to the outside world.

The 12-month bite: the first commercial pilot (whatever Mara recommends as the wedge) will trigger a security questionnaire from the customer's IT/legal team. Without a named owner who can answer SOC2-shaped questions credibly, we'll either lose the deal or fake the answers — and faking those answers is how startups get sued.

**Profile:** mid-to-senior, ex-Apple/Signal/Cloudflare/1Password kind of background. Comfortable being the only privacy person in a hardware shop. Willing to write code, not just policy. Reports to me, not to Sophie — this is a CTO-org function, not a PM one.

Vlad will push back that this is premature and we should hire another firmware engineer instead. He's wrong, and the argument I'd use with him: firmware bugs cost us a week; a privacy incident costs us the company.

---

## 3. Pitch-readiness — the undefendable slide

Mara's wedge doc isn't on disk yet (`~/repos/.scratch/commercialization-wedge-2026-05-14.md` not present as of this write). So I'm reading this against the technical artifacts we have — `docs/strategy/2026-q3-vision.md`, the roadmap, and the live state — and answering: which slide gets us mauled regardless of which wedge she picks?

**The accuracy/coverage slide.**

Specifically: any version of the deck that says some flavor of "23 cm σ entity fusion" or "±20 % distance accuracy with IMU normalization" or "8 leaves cover a typical home." A sharp investor with a sensor-fusion background — and there are several at a16z and Lux who'll see this — will ask three questions, and we don't have clean answers to any of them:

1. **"What's your N?"** Our 23 cm σ number is measured in *one room*, in *one apartment*, with *known leaf positions and a controlled walk path*. We don't have a multi-environment validation set. Every RF person in the room will know this immediately, because everyone who's ever done indoor RF has burned themselves on results that didn't transfer between buildings. Drywall vs. lath-and-plaster, 2.4 GHz noise floor in an urban apartment vs. a suburban house, body shadowing variance across human heights — none of that is in our error bars.

2. **"What does that number look like with people moving around and Apple Continuity rotating MACs mid-walk?"** Eszter's Kalman fusion handles the static case beautifully. The Phase 1+2 fingerprinting story *intends* to handle the dynamic case, but it hasn't shipped yet, and the dynamic-case σ is going to be much worse than the static-case σ. If the deck conflates the two, that's the question that ends the meeting.

3. **"Have you measured against ground truth, or just self-consistency?"** This is the one that hurts. We don't have a UWB anchor or a camera-based ground truth in the apartment. Our "23 cm" is dispersion of our own estimator, not error against true position. A sensor-fusion partner will catch this in 30 seconds.

**What to do about it before any pitch:**

- Run a second environment. Anywhere — Vlad's apartment, your parents' house, an Airbnb for a weekend. Two data points isn't a study, but it's infinitely more than one and it changes the answer to question 1 from "we haven't measured that" to "here's how it generalized."
- Buy one UWB tag + 3 anchors (~$200, DWM3000-class). Sub-10-cm ground truth, one weekend of work for Eszter. Turns question 3 from a credibility-killer into a credibility-builder.
- Restate the accuracy claim in honest, defensible terms. "Per-track Kalman dispersion of 23 cm in a 60 m² evaluation environment, validated against [N=2] secondary environments, ground-truthed against UWB at [X cm] mean error." That sentence wins the room. The current sentence loses it.

This is the cheapest experiment per dollar of pitch credibility we will run all year. Cheaper than the MPU6050 buy. Do it before the deck goes in front of anyone whose check matters.

---

## Closing

The platform is in good shape. The architecture is appropriate for *now* and the failure modes for *later* are predictable and survivable if we sequence them. The team is strong in execution and weak in one specific direction that matters more than it looks. The pitch story is real, but the accuracy claim is the part that won't survive contact with a sharp diligence process — and it's the cheapest of the three problems to fix.

If I had to pick one of these three to act on this week, it's the UWB ground-truth + second-environment measurement. Two weekends of Eszter's time and $200 of hardware buys us a credibility upgrade we will need before any of the rest of this matters.

— AZ
