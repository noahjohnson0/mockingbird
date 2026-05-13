# WAN-1: EDA plan for the collector data

Author: Wanjiru Mwangi
Branch: `wan/eda-plan`
Status: plan (not yet executed against the live Pi DB)

## Why this document exists

Before we ship any change to the trilateration / RSSI-fusion pipeline, we
need a clear-eyed picture of what the data actually looks like. The team
has been moving fast on modeling (robust MLE, EWMA, per-leaf TX/RX bias
decomposition) and on dashboards — but the EDA itself has been ad-hoc,
mostly living inside `scripts/analyze_ble_db.py`. That script is a useful
spot-check tool; it is not a substitute for a documented baseline of
what's in the database.

I don't have direct read access to `~/mockingbird/observations.sqlite` on
the Pi from this worktree, so this is a **plan**: queries, expected
shapes, what counts as a finding, and the validation framework we should
adopt for trilateration changes going forward. Once Noah or I run these
on the Pi, the outputs land in `docs/data/eda-findings.md` (sibling file,
to be written).

## Schema (from `services/mockingbird-collector.py`)

```sql
CREATE TABLE obs (
    ts        REAL    NOT NULL,   -- Pi wall-clock at receive (seconds, float)
    leaf      TEXT    NOT NULL,   -- e.g. 'mockingbird-4ce184'
    mac       TEXT    NOT NULL,   -- BLE advertiser MAC
    rssi      INTEGER NOT NULL,   -- dBm, signed
    addr_type INTEGER,            -- 0=public, 1=random, etc.
    name      TEXT,               -- advertised local name (often NULL)
    manuf     TEXT,               -- manufacturer-data hex (often NULL)
    leaf_t_ms INTEGER,            -- leaf's millis() at capture (RESETS ON REBOOT)
    location  TEXT                -- per-leaf label, optional
);
-- idx: (ts), (mac, ts), (leaf, ts)

CREATE TABLE leaf_events (
    ts    REAL NOT NULL,
    leaf  TEXT NOT NULL,
    event TEXT NOT NULL,          -- 'hello' | 'hb' | 'disconnect'
    info  TEXT,                   -- JSON blob (for hello/hb)
    location TEXT
);
-- idx: (ts)
```

Key DGP notes that shape every analysis below:

- **`ts` is the Pi's clock at receive**, not the moment of the BLE
  advertisement. WiFi-uplink queueing on a single-chip leaf adds jitter
  (tens to low hundreds of ms when WiFi is busy). For sub-second
  alignment we should treat `ts` as approximate; for second-resolution
  questions it is fine.
- **`leaf_t_ms` resets on reboot.** Do not use it for cross-leaf
  alignment. It is useful for *intra-leaf* ordering when `ts` ties.
- **RSSI is integer dBm**, quantized at 1 dB. Don't fit Gaussians naively
  on small samples without checking for the quantization plateau.
- **Single-chip leaves time-slice WiFi and BLE.** Duty cycle is reported
  to be 30–70% under load. That means observation rate per leaf is not a
  clean Poisson — it's a thinned process that depends on uplink traffic.
- **Random-resolvable BLE addresses rotate** (Apple every ~15 min by
  default). Any "device-level" aggregation by raw MAC will fragment for
  most phones. We can still analyze by raw MAC for the duration of one
  rotation window.

---

## 1. Per-leaf observation rates

**Question.** Are the leaves contributing roughly balanced volumes of
observations? Is any leaf dominating, silent, or showing a bursty /
intermittent pattern that the eye won't catch in a single scalar?

**Query.**

```sql
-- (a) Headline rate per leaf, last 1 hour
SELECT
    leaf,
    COUNT(*)                          AS n_obs,
    COUNT(DISTINCT mac)               AS n_devices,
    MIN(rssi)                         AS rssi_min,
    MAX(rssi)                         AS rssi_max,
    ROUND(AVG(rssi), 1)               AS rssi_mean,
    MIN(ts)                           AS ts_first,
    MAX(ts)                           AS ts_last,
    ROUND(COUNT(*) * 1.0 /
          (MAX(ts) - MIN(ts) + 1e-9), 2) AS obs_per_sec
FROM obs
WHERE ts > (SELECT MAX(ts) FROM obs) - 3600
GROUP BY leaf
ORDER BY n_obs DESC;
```

```sql
-- (b) Per-minute rate per leaf, last hour — for plotting a time-series
SELECT
    leaf,
    CAST(ts / 60 AS INTEGER) AS minute_bucket,
    COUNT(*)                 AS n_obs
FROM obs
WHERE ts > (SELECT MAX(ts) FROM obs) - 3600
GROUP BY leaf, minute_bucket
ORDER BY minute_bucket, leaf;
```

**Pandas equivalent for (b)** (the plotting form I'll actually use):

```python
import pandas as pd, sqlite3
con = sqlite3.connect("observations.sqlite")
df = pd.read_sql(
    "SELECT ts, leaf FROM obs WHERE ts > ? ",
    con, params=[time.time() - 3600],
)
df["t"] = pd.to_datetime(df["ts"], unit="s")
rate = (df.set_index("t")
          .groupby("leaf")
          .resample("1min").size()
          .unstack(level=0)
          .fillna(0))
# rate.plot(subplots=False) → 1 line per leaf
```

**Expected output.** Query (a) yields one row per leaf (8 leaves
currently per the project state). I expect `obs_per_sec` in the rough
range 5–25/leaf based on the "~110 obs/sec aggregate" note in CLAUDE.md.
Query (b) yields a tidy time-series I plot as a small-multiples chart,
one panel per leaf.

**What's a finding worth acting on.**

- Any leaf at <30% of the median leaf's rate while still emitting
  heartbeats → likely BLE-stack starvation under WiFi load, antenna
  orientation issue, or RF shadowing. Action: check uplink RTT to that
  leaf, then physically inspect placement / antenna.
- Any leaf at >2× the median rate → either privileged RF position (fine,
  but document it) or duplicate observation emission (firmware bug).
  Cross-check: does this leaf see more *unique MACs*, or just more
  observations of the same MACs? If the latter, it's a duty-cycle /
  scan-window config drift, not a real signal advantage.
- A leaf with a strong diurnal pattern that the others don't share →
  environmental interference (microwave, neighbor's WiFi). Worth
  recording before we read RSSI bias as physics.
- An hourly gap of >10 min for any leaf, repeated → flag for
  heartbeat-freshness analysis below.

---

## 2. Per-leaf RSSI bias characterization

**Question.** Is RSSI biased per leaf, after controlling for the device
being observed? Is the bias stationary across the day, or is there
time-of-day structure (foot traffic, microwave use, the AP retraining
itself)?

This is the single most important EDA for trilateration. A 4 dB
stationary per-leaf bias is roughly a 50–60% error in linear-distance
estimate at typical indoor path-loss exponents (n ≈ 2.5). The team has
already shipped a TX/RX bias decomposition (commit `6703762`) — this
analysis is the empirical check on whether that decomposition is doing
what it claims.

### 2a. Distribution of RSSI per leaf, on universal devices

**Definition: "universal device" in a window** = a MAC observed by *all*
currently-active leaves within the window. Restricting to these removes
the confound where one leaf sees more weak distant devices than another.

**Query.**

```sql
-- Step 1: find leaves active in window
WITH window AS (
  SELECT (SELECT MAX(ts) FROM obs) - 3600 AS t0,
         (SELECT MAX(ts) FROM obs)        AS t1
),
active_leaves AS (
  SELECT DISTINCT leaf
  FROM obs, window
  WHERE ts BETWEEN window.t0 AND window.t1
),
n_active AS (SELECT COUNT(*) AS n FROM active_leaves),
-- Step 2: MACs seen by all active leaves
universal_macs AS (
  SELECT mac
  FROM obs, window
  WHERE ts BETWEEN window.t0 AND window.t1
  GROUP BY mac
  HAVING COUNT(DISTINCT leaf) = (SELECT n FROM n_active)
)
SELECT leaf, mac, rssi, ts
FROM obs, window
WHERE ts BETWEEN window.t0 AND window.t1
  AND mac IN (SELECT mac FROM universal_macs);
```

Pull into pandas, then:

```python
# Per-leaf bias on universal devices, paired by MAC
pivot = (df.groupby(["mac", "leaf"])["rssi"].mean()
           .unstack("leaf"))
# Reference frame: subtract the mean across leaves per device,
# then average residuals per leaf.
resid = pivot.sub(pivot.mean(axis=1), axis=0)
bias  = resid.mean(axis=0)          # per-leaf bias in dB
bias_ci = resid.apply(
    lambda s: scipy.stats.bootstrap(
        (s.dropna().values,),
        np.mean, n_resamples=2000,
        confidence_level=0.95,
    ).confidence_interval
)
```

**Expected output.** A bar chart: one bar per leaf, height = mean
residual RSSI (dB), with bootstrap 95% CIs. Plus a per-leaf RSSI
*distribution* (violin or KDE) for the same universal-device set.

**Finding worth acting on.**

- Any leaf whose CI for bias excludes zero by >1 dB → record the bias as
  a calibration offset. We already do TX/RX decomposition; this is the
  empirical residual after that decomposition. If residuals are still
  nonzero, the decomposition is under-specified (or has a stale fit).
- Heavy skew or bimodality in the per-leaf RSSI distribution on a fixed
  device set → multipath signature. Worth a follow-up with Vlad: are we
  seeing two propagation regimes (LoS vs. reflected) at this leaf?
- Heavy quantization plateaus at specific dB values (the chip's reported
  RSSI is not perfectly linear) → don't fit Gaussian noise; switch to
  the empirical CDF for any uncertainty quantification.

### 2b. Stationarity of bias across the day

**Query.**

```sql
-- Hourly RSSI mean per (leaf, mac) on universal devices
-- (use the same universal-MAC CTE as above, widened to 24h)
SELECT
    leaf,
    mac,
    CAST(ts / 3600 AS INTEGER) AS hour_bucket,
    AVG(rssi)                  AS rssi_mean,
    COUNT(*)                   AS n
FROM obs
WHERE ts > (SELECT MAX(ts) FROM obs) - 86400
  AND mac IN (/* universal MACs over 24h */)
GROUP BY leaf, mac, hour_bucket
HAVING n >= 5;
```

In pandas: compute per-(leaf, mac) bias *within each hour* (residual
against cross-leaf mean for that hour and MAC), then plot bias-over-time
per leaf.

**Expected output.** A line plot per leaf, x = hour-of-day, y = mean
residual RSSI. Faint band = 95% CI.

**Finding worth acting on.**

- A leaf whose bias drifts by >2 dB between, say, 02:00 and 18:00 → not
  stationary. We should refit calibration on a sliding window or use a
  time-varying model. This is a real possibility — body absorption,
  appliance interference, even temperature-driven LNA gain shifts.
- Synchronized bias swings across all leaves at the same hour → it's the
  *advertiser population* changing (phones leaving the house, etc.),
  not a per-leaf phenomenon. Different action: redefine "universal" on
  shorter windows.

---

## 3. Coverage histogram

**Question.** For a typical device, how many of our leaves see it? This
sets the floor on what trilateration can do: trilateration with 1–2
leaves is impossible, with 3 is poorly constrained, with 4+ is
well-posed.

**Query.**

```sql
SELECT
    n_leaves,
    COUNT(*) AS n_devices
FROM (
    SELECT mac, COUNT(DISTINCT leaf) AS n_leaves
    FROM obs
    WHERE ts > (SELECT MAX(ts) FROM obs) - 600   -- 10 min window
    GROUP BY mac
)
GROUP BY n_leaves
ORDER BY n_leaves;
```

**Expected output.** A histogram with bins 1..N where N is the number of
active leaves (currently 8). CLAUDE.md mentions an earlier 4-leaf run
where 59% of devices were universal — I expect that to drop substantially
at 8 leaves (more leaves → harder for a device to reach all of them).

**Finding worth acting on.**

- If the median coverage is ≥4 leaves → trilateration is viable for the
  majority of devices. Good news.
- If the median is ≤2 → most devices are *not* trilatable; we should
  redesign the deployment (denser, closer to expected device locations)
  OR scope down the claim to "high-RSSI device tracking" only.
- Long tail of 1-leaf-only devices → spatial-coverage holes. Map which
  leaf is the lone observer for each — that tells us where to add a
  neighbor.

---

## 4. Device churn (static vs. mobile signature)

**Question.** Which devices are continuously present (likely static —
fridge BLE beacon, fixed sensor) vs. transient (phone walking past)? The
two classes need different treatment in any motion-tracking pipeline.

**Query.**

```sql
-- For each MAC: first/last seen, total span, gap-coefficient
SELECT
    mac,
    MIN(ts)                              AS ts_first,
    MAX(ts)                              AS ts_last,
    MAX(ts) - MIN(ts)                    AS span_s,
    COUNT(*)                             AS n_obs,
    COUNT(DISTINCT leaf)                 AS n_leaves,
    -- crude "stationary-ness": ratio of obs count to span in seconds
    ROUND(COUNT(*) * 1.0 /
          (MAX(ts) - MIN(ts) + 1), 3)    AS obs_per_s
FROM obs
WHERE ts > (SELECT MAX(ts) FROM obs) - 86400
GROUP BY mac
ORDER BY span_s DESC;
```

```sql
-- Appear/disappear at the midpoint of a window — the churn signature
-- (this is what scripts/analyze_ble_db.py already computes)
WITH halves AS (
  SELECT
    mac,
    SUM(CASE WHEN ts < (t0 + t1)/2 THEN 1 ELSE 0 END) AS n_first,
    SUM(CASE WHEN ts >= (t0 + t1)/2 THEN 1 ELSE 0 END) AS n_second
  FROM obs,
       (SELECT (SELECT MAX(ts) FROM obs) - 3600 AS t0,
               (SELECT MAX(ts) FROM obs)        AS t1) w
  WHERE ts BETWEEN w.t0 AND w.t1
  GROUP BY mac
)
SELECT
  CASE
    WHEN n_first = 0 AND n_second > 0 THEN 'appeared'
    WHEN n_first > 0 AND n_second = 0 THEN 'disappeared'
    WHEN n_first > 0 AND n_second > 0 THEN 'persistent'
  END AS class,
  COUNT(*) AS n_devices
FROM halves
GROUP BY class;
```

**Expected output.** Two summaries: (1) a scatter of `span_s` vs.
`n_obs` colored by `n_leaves` — static devices cluster top-right
(long span, dense observations, many leaves); transient devices form a
diagonal smear at low span. (2) Appeared / disappeared / persistent
counts.

**Finding worth acting on.**

- A device that "disappears" but is known to be physically static → that
  leaf or region is being shadowed. Investigate at the leaf level.
- The persistent-device set is our *de-facto calibration set*. If it's
  small (<10 devices), we're under-constrained for the bias analysis in
  §2. Add cheap static BLE beacons to fix.
- High churn fraction (>50% of unique MACs per hour are transient) →
  expected if the dataset includes phones with rotating addresses. Sanity-
  check by `addr_type`: random-resolvable churn ≠ real device churn.

---

## 5. Heartbeat freshness distribution

**Question.** How often do leaves go quiet, and for how long? This is
the operational reliability of the mesh.

**Query.**

```sql
-- Inter-heartbeat gaps per leaf, last 24h
WITH hb AS (
  SELECT leaf, ts,
         LAG(ts) OVER (PARTITION BY leaf ORDER BY ts) AS prev_ts
  FROM leaf_events
  WHERE event = 'hb'
    AND ts > (SELECT MAX(ts) FROM leaf_events) - 86400
)
SELECT
    leaf,
    COUNT(*)                        AS n_gaps,
    ROUND(AVG(ts - prev_ts), 2)     AS gap_mean_s,
    ROUND(MIN(ts - prev_ts), 2)     AS gap_min_s,
    ROUND(MAX(ts - prev_ts), 2)     AS gap_max_s,
    SUM(CASE WHEN ts - prev_ts > 60   THEN 1 ELSE 0 END) AS n_gap_gt_60s,
    SUM(CASE WHEN ts - prev_ts > 300  THEN 1 ELSE 0 END) AS n_gap_gt_5m,
    SUM(CASE WHEN ts - prev_ts > 3600 THEN 1 ELSE 0 END) AS n_gap_gt_1h
FROM hb
WHERE prev_ts IS NOT NULL
GROUP BY leaf;
```

```sql
-- Disconnect events per leaf
SELECT leaf, COUNT(*) AS n_disconnects
FROM leaf_events
WHERE event = 'disconnect'
  AND ts > (SELECT MAX(ts) FROM leaf_events) - 86400
GROUP BY leaf
ORDER BY n_disconnects DESC;
```

**Expected output.** A table per leaf of gap statistics, plus a CDF plot
of inter-heartbeat gaps (one line per leaf).

**Finding worth acting on.**

- Any leaf with median gap > 1.5× the firmware's heartbeat interval →
  WiFi flakiness, NAT timeout, or the leaf is rebooting.
- Disconnect count > ~5/day → a leaf that needs physical or
  configuration attention.
- A correlated gap across multiple leaves at the same time → AP-side
  issue (Opal reboot, channel scan), not leaf side. Don't blame the
  firmware for a router hiccup.

---

## 6. Manufacturer / advertiser breakdown

**Question.** What does the device population look like? This grounds
every other analysis — if 80% of observations are from one beacon, our
"per-leaf RSSI bias" is really "this beacon's signature at each leaf,"
not a general statement.

**Query.**

```sql
-- Top manufacturers (manuf is the BLE manufacturer-data hex prefix)
SELECT
    SUBSTR(manuf, 1, 4) AS manuf_id,    -- BLE Company ID, little-endian hex
    COUNT(*)            AS n_obs,
    COUNT(DISTINCT mac) AS n_devices
FROM obs
WHERE ts > (SELECT MAX(ts) FROM obs) - 3600
  AND manuf IS NOT NULL
GROUP BY manuf_id
ORDER BY n_obs DESC
LIMIT 20;
```

```sql
-- Named advertisers (TVs, watches, headphones — these often have local names)
SELECT name, COUNT(DISTINCT mac) AS n_devices, COUNT(*) AS n_obs
FROM obs
WHERE ts > (SELECT MAX(ts) FROM obs) - 3600
  AND name IS NOT NULL
GROUP BY name
ORDER BY n_obs DESC
LIMIT 30;
```

```sql
-- Address-type breakdown (random vs. public)
SELECT addr_type, COUNT(*) AS n_obs, COUNT(DISTINCT mac) AS n_macs
FROM obs
WHERE ts > (SELECT MAX(ts) FROM obs) - 3600
GROUP BY addr_type;
```

Map `manuf_id` to vendors using the [Bluetooth SIG company IDs](https://www.bluetooth.com/specifications/assigned-numbers/company-identifiers/)
list — keep a cached `company_ids.json` next to the analysis script
(the list is ~3000 entries; pulling it once is fine).

**Expected output.** Top-N tables for manufacturer, named advertiser,
and address-type, all with both observation and unique-device counts.
The address-type table is small and informative — the random-vs-public
ratio is a one-line summary of "how much MAC rotation are we fighting?"

**Finding worth acting on.**

- Single manufacturer >50% of `n_obs` → calibration analyses must be
  re-run excluding that manufacturer to check we're not just fitting one
  device's behavior.
- Very high random-MAC fraction (>80%) → we will systematically
  underestimate persistence and overestimate churn. Action: implement a
  short-term identity layer (cluster random MACs by their advertised
  payload + RSSI signature) before any "tracking" claims.

---

## Validation framework for trilateration changes

This is the part that matters most for the team going forward, so I'm
being concrete.

### The claim a trilateration change should support

"Algorithm B produces lower position error than algorithm A on data the
algorithm has never seen, drawn from conditions where it will run in
production." That's the only claim worth defending. Improvements on the
fit-set are not findings.

### Held-out sets (in increasing order of realism)

1. **Random-split holdout (weakest).** Random 20% of observations
   reserved. Useful only as a sanity check; offers little protection
   because adjacent-in-time observations are highly correlated.
2. **Temporal holdout.** Train on `ts < T`, test on `ts >= T`. Choose
   `T` such that the test window is at least 1 hour, ideally spans a
   different time-of-day than training. This catches over-fitting to
   diurnal RF conditions.
3. **Device holdout.** Hold out specific MACs entirely. Train on
   {devices A, B, C}, test on {device D}. This is the real test of
   whether the calibration generalizes — if it doesn't, we've fit one
   beacon's idiosyncrasies, not the physics.
4. **Deployment holdout (the gold standard).** A known beacon placed at
   a *measured ground-truth position* not used in any model fit. The
   only set where the metric we care about (centimeters of position
   error) can be computed directly.

For day-to-day work I propose we adopt **temporal + device holdout in
combination**: split by `(time window, MAC)` so neither time nor device
identity leaks. Deployment holdout is the gate before any model is
declared "ready for the dashboard."

### Metrics (with uncertainty)

For a position estimator:

- **Primary:** median Euclidean error (m), reported with bootstrap 95% CI
  over the test points. Median, not mean — RSSI-based estimators have
  fat-tailed error distributions and the mean is dominated by a few
  pathological cases.
- **Secondary:** 90th-percentile error. The deployment cares about worst
  case, not just typical case.
- **Calibration of uncertainty:** if the estimator outputs a covariance
  or confidence region, the *coverage* of that region on held-out data.
  A 90% confidence region should contain the true point 90% of the time.
  Plot a reliability diagram. Recalibrate (isotonic) if it's miscalibrated.
- **Stability:** standard deviation of estimated position across
  consecutive 1-second windows for a known *static* beacon. We've been
  reporting "centroid jitter 300cm → 18cm" — that's exactly this metric.
  Keep reporting it.

### Significance test

Two estimators A and B on the same held-out set: paired test on per-test-
point error. Use a **paired bootstrap** of the difference in median error
(10,000 resamples). Report the 95% CI on `median(err_B) − median(err_A)`.
Significant improvement = the CI is entirely below zero.

Don't use a t-test on raw errors — the errors are not normal and not
independent (consecutive observations are correlated). Bootstrap respects
the empirical distribution.

For cross-validation across multiple held-out folds, report the mean of
fold-level median errors with the across-fold SE; do *not* pool
observations across folds before computing the median (that hides
per-fold variance).

### Pre-registration

For each proposed algorithm change, before running on the held-out set
we write down:

1. The exact metric and the held-out set definition.
2. The decision rule: "we ship B if its 95% bootstrap CI for the median
   error improvement is entirely below zero AND the calibration coverage
   on B is within 5 percentage points of nominal."
3. What we *won't* do: hyperparameter-tune on the held-out set; rerun
   with different splits until we get a significant result; quietly
   redefine the test set after seeing the numbers.

This is boring on purpose. The point is to stop arguing about which
model "feels" better and let the held-out data settle it.

---

## Top 3 queries to run first (and what I hope to learn)

Listed in dependency order — each one informs the interpretation of the
next:

### Priority 1 — §6 Manufacturer / advertiser + address-type breakdown

Run this **first**, even though it's listed last. Everything else
depends on knowing what the population is. If 70% of observations are
random-MAC iPhones with 15-minute rotation, the meaning of "device" in
every other analysis is different from if 70% are static public-MAC
beacons. **Hoped outcome:** a clear picture of (public vs. random)
fraction and the top 5–10 named/manufacturer cohorts — so I can pick
the right cohort to use as the universal-device basis in §2.

### Priority 2 — §3 Coverage histogram

Cheap, fast, decisive. **Hoped outcome:** the median number of leaves
that see a typical device, on the current 8-leaf deployment. If median
coverage is ≥4 we proceed with trilateration as planned. If it's 1–2 we
have a deployment problem that no amount of modeling will fix, and the
next sprint pivots to leaf-placement rather than algorithms.

### Priority 3 — §2a Per-leaf RSSI bias on universal devices

The actionable empirical check on the TX/RX bias decomposition that
already shipped. **Hoped outcome:** per-leaf residual bias with
bootstrap CIs, on the cohort identified in priority 1. If residuals are
within ±1 dB and CIs include zero → the decomposition is working and we
trust trilateration to inherit the calibration. If any leaf shows
>2 dB residual with a CI that excludes zero → we have a calibration
gap to close before validating any new trilateration algorithm. Either
result is useful; the question is well-posed and the data either says
yes or it says no. *Haya — let the held-out data speak.*
