# RF calibration model review (PURU-1)

**Author:** Puru
**Branch:** `puru/rf-review`
**Scope of review:** `services/mockingbird_calibration.py` @ commit `6703762`
("Per-leaf TX/RX bias decomposition") plus the RSSI source in
`firmware/esp32-wroom-mockingbird/src/main.cpp` and how the collector
feeds `obs` rows into the calibration pipeline.

---

## 1. The model

The implicit physical model is the **classical log-distance path-loss
equation**:

```
RSSI(d) = P0 - 10 · n · log10(d)        + ε      (global)
RSSI_i(d) = P0_i - 10 · n_i · log10(d)  + ε_i    (per receiver leaf i)
```

with the extension that for an *ordered TX→RX pair* (A transmits, B
receives) at known distance `d_AB`, the observed RSSI is modeled as

```
rssi_AB ≈ P0_B - 10 · n_B · log10(d_AB) + TX_A + RX_B + noise
```

where:

- `P0` is the **reference RSSI at d = 1 m**, expected to land somewhere
  in the −45 to −65 dBm range for the WROOM-32 PCB-antenna +
  generic-advertiser geometry. The fit clamps post-hoc to the kept
  residual set; there is no explicit prior.
- `n` is the **path-loss exponent**. Hard-clamped to `[2.0, 4.5]` for
  the centroid bootstrap fit and `[1.8, 5.0]` for the anchor-truth fit,
  with a per-leaf clamp of `[1.5, 5.5]`. Free space ≈ 2; typical indoor
  with mixed LOS/NLOS ≈ 2.5–3.5; through-wall pushes 4+.
- `d` is the **straight-line 3-D distance** between the known anchor
  position and the leaf position. There is no obstruction graph, no
  per-wall attenuation term.
- `ε ~ N(0, σ_i)` is implicitly **zero-mean Gaussian, IID across
  observations, with a per-leaf variance**. The variance is the RMSE of
  the per-leaf fit residuals in `_fit_per_leaf_models`. This σ is the
  weight used in the MLE multilateration (delta-method propagated to a
  variance on `d`).
- The two-bias decomposition (`TX_A`, `RX_B`) is gauge-fixed by
  `mean(TX) = 0`. The leaf-advert calibration_points (one anchor per
  leaf, where TX is a leaf at a known position) are the only data
  source — *not* the regular `obs` stream.

### What the model assumes (often quietly)

The calibration pipeline assumes, in roughly decreasing order of how
much each assumption hurts when violated:

| Assumption | Where it shows up | Cost when wrong |
|---|---|---|
| **Isotropic radiation** from both TX and RX antennas | `d` enters as a scalar, no angle term | One leaf's bias becomes a function of where the target is in the room — bias gets aliased into the residual, σ_i inflates |
| **One TX power per *receiver* fit** (the receiver's own P0_i absorbs the average TX power of all calibration sources) | `_fit_per_leaf_models` lumps all anchor sources into one regression | A new advertiser with TX power 6 dB different from the average calibration source maps to a distance error of `10^(6/10n) ≈ 1.7×` (for n=2.5) |
| **Frequency-flat behavior across channels 37/38/39** | RSSI is averaged with no channel field | Channel-dependent path loss + leaf antenna gain ripple aliases into σ_i, but is also *systematic* per receiver direction |
| **Multipath is zero-mean Gaussian** | The 15 % residual trim in `fit_pathloss` and Huber IRLS in `mle_multilaterate` try to defend against this | Heavy tails are absorbed reasonably well; *correlated* multipath (e.g. always-present standing waves at certain geometries) is not |
| **Body absorption is constant in time** | No body/occupancy term at all | A person walking between target and leaf produces a 10–20 dB shadow that the model attributes to distance — i.e. position estimate moves with the obstruction |
| **TX power is consistent across advertisers** | Bootstrap path mixes phones, AirPods, Tiles, etc. into one (p0, n) fit | The global P0 ends up at the *population mean*, and individual devices' distances are biased by their own (unmodeled) TX offset |

The current code does **NOT** model: TX-power variation between
advertisers, antenna pattern, multipath, body absorption,
channel-dependent RSSI, PHY type (1M vs Coded), or temperature. The
per-leaf σ is the catch-basin for all of those.

---

## 2. TX vs RX bias decomposition — is it identifiable?

### The linear algebra

For an N-leaf mesh we observe, for each ordered TX→RX pair where both
are positioned and distance is known, the residual

```
r_AB = rssi_AB - (P0_B - 10·n_B·log10(d_AB))   = TX_A + RX_B + ε
```

This is N(N−1) equations in 2N unknowns. For N=8 that's 56 equations,
16 unknowns. **The system is rank-deficient by exactly one**: adding a
constant `c` to every `TX_i` and subtracting `c` from every `RX_i`
leaves every residual unchanged. The implementation fixes this by
constraining `mean(TX) = 0`, which is the standard gauge fix.

Algebraically clean. The ALS solver in `_fit_leaf_biases` converges in
~20 sweeps and the empirical decomposition reported in commit 6703762
(TX spread 19 dB across the 8-leaf fleet) is **plausible and well
within what I'd expect for low-cost ESP32 PCB-antenna boards** — I've
seen 8–15 dB spreads on Cisco access points coming off the same
production line, and ESP32 antenna tuning is much less controlled than
that.

### Where it breaks down

Five degrees-of-freedom failures, ordered by how often they bite:

1. **Geometric near-coplanarity.** If the leaves are all on roughly the
   same horizontal plane (typical for a single-floor home install) the
   anchor-source positions are concentrated in a thin slab, and the
   RX-bias estimate becomes highly correlated with `n_i` — both reduce
   to "this leaf reports lower RSSI than its neighbors at the same
   geometric distance." With only one anchor *per* TX leaf, you cannot
   separate "this RX is biased low" from "this RX's n is high." The
   per-leaf fit lumps them; the bias decomposition then attributes the
   leftover to RX bias because that's the only DOF left. **You can
   double-check this in the data: if you see `n_i` distribute tightly
   around the global `n` and `rx_bias` span a wide range, you're in
   this regime — i.e. n is hiding inside rx_bias.**
2. **Body/clutter asymmetry during calibration.** The bias
   decomposition assumes the residual at `(A→B)` and at `(B→A)` would
   be equal under reciprocity. In practice, if a body is sitting in
   the chair between leaf 4 and leaf 7 *during the 4-leaf-advert
   capture*, both directional pairs get the same shadow added to their
   residual — which then *aliases into either TX_4 or RX_7* depending
   on what the other-direction residuals look like. The fleet's
   reported `TX +7.4 dB` for `4d4384` and `TX -11.7 dB` for `4c0bdc`
   *might* be real RF-chain variance, or might be partly furniture
   geometry that was static during calibration. There's no way to tell
   from the current data set alone.
3. **Single anchor window per TX leaf.** Each leaf has one
   `leaf-advert` calibration_point, captured once. There's no
   replication, no time-of-day variation, no rotation. The residual
   `r_AB` collapses to a single number per pair. **A 1-bit-of-data
   measurement is fine for the bias mean, but offers no σ on the bias
   estimate itself**, so the MLE doesn't know how confident it should
   be in any particular leaf's TX bias.
4. **Coupling with `P0` of the receiver fit.** `_fit_per_leaf_models`
   uses *all* anchor points (including leaf-adverts from leaves with
   their own unknown TX bias) as the regression sample. The receiver's
   `P0_i` therefore absorbs the *average* TX bias of all transmitter
   leaves contributing to its fit. Then `_fit_leaf_biases` is run
   *afterwards* and subtracts its own decomposition. Because the
   bias-mean has already been baked into `P0_i`, the recovered
   `TX_A + RX_B` only captures the *deviation* from that mean. The
   gauge `mean(TX) = 0` is consistent with this, but it means
   `rx_bias` and `P0_i` are jointly determined and the partitioning
   between them is somewhat arbitrary.
5. **Non-leaf advertisers don't contribute to bias estimation.** The
   bias model is fit *only* on `label LIKE 'leaf-advert%'` rows. If a
   leaf has flaky advertising during the capture, it gets a noisy bias
   and there's no path to refine it from the millions of
   non-leaf-advert observations in the `obs` table. This is a missed
   opportunity for free additional constraint.

### Identifiability summary

**The TX/RX decomposition is well-posed given clean data**, but the
data quality bottleneck is severe: 1 anchor window per TX leaf, no
reciprocity check, no per-pair confidence weighting. The 19 dB spread
is probably 60–80 % real RF variance and 20–40 % geometry/clutter
artifacts of that single capture. **Recommendation in §5 covers how to
get this down.**

---

## 3. What we're not modeling

The model is silent on five physical effects that each contribute
meaningfully at this scale of measurement:

### 3.1 Body absorption (~10–20 dB)

A human torso at 2.4 GHz absorbs ~10–15 dB on direct LOS and creates a
similar deep shadow on transmission. For a person standing between a
leaf and an advertiser at 3 m, the model interprets that as the
distance growing by `10^(15/(10·2.5)) ≈ 3.98×` → suddenly the
advertiser is "12 m away from that leaf" and the MLE drags toward the
unblocked leaves. The Huber IRLS limits how badly this distorts
position (residuals beyond `HUBER_K = 1.5 m` get scaled down) but
**the body absorption is structured, not random** — when a person sits
in their usual chair, the same two leaves get shadowed every time, and
Huber-on-each-frame can't see that it's a systematic effect.

### 3.2 Multipath / fading (Rician/Rayleigh ~ 6–10 dB σ at 2.4 GHz indoors)

The fit handles it as zero-mean Gaussian noise after a 15 % residual
trim. This is fine for *uncorrelated* fast fading but the dominant
contribution at fixed geometry is **slow fading from standing waves**
— a steady offset, not noise. In a typical small room you can move
the receiver 6 cm and shift RSSI by 5 dB without anything else
changing. Most of your per-leaf σ_i of 3–13 dB is multipath, not
RF-chain noise.

### 3.3 Antenna pattern on the ESP32 PCB antenna

The WROOM-32's IFA (inverted-F) PCB antenna has a quasi-omnidirectional
azimuth pattern, **but with a roughly 4–6 dB front-to-back null** at
2.4 GHz, plus a sharp vertical-polarization preference and a deep null
toward the ground plane (i.e. through the PCB substrate). Mounting
orientation matters:

- Module flat on a table, antenna sticking off the edge → azimuth
  pretty omnidirectional, but elevation gain is poor below the plane.
- Module standing vertical → the gain peak rotates with it; a leaf
  mounted with the antenna pointing east will read a target to the
  west 3–6 dB hotter than a target to the east, all else equal.

None of this is modeled. **It currently gets aliased into `TX_bias` or
`RX_bias` based on the geometry of the calibration capture**, which is
why the `_fit_leaf_biases` numbers are sensitive to the orientation
each leaf had during the leaf-advert capture (and if anyone later
re-mounts a leaf, the bias is wrong).

### 3.4 Channel-dependent RSSI (37/38/39, ~2 dB chip-side + multipath)

BLE legacy advertising hops over channels **37 (2402 MHz), 38 (2426
MHz), 39 (2480 MHz)**. The chip's gain flatness across that 78-MHz
span is roughly ±1.5 dB on the ESP32 PA/LNA. More importantly, the
*multipath geometry is different on each channel* — at a half-
wavelength of ~6 cm the standing-wave pattern shifts substantially
between 2402 and 2480 MHz, easily yielding 5–10 dB of channel-dependent
RSSI for a stationary advertiser at a fixed leaf.

NimBLE's `onResult` callback in `main.cpp:107` only gives us the RSSI;
the channel is **not** in the message struct. Without that, we average
over an unknown mixture of 3 channels and the variance gets absorbed
into σ_i. **Adding the BLE channel to the obs schema is the single
highest-leverage one-line firmware change I can recommend** — see §5
P0.

### 3.5 Coded PHY vs 1M PHY

BLE 5 advertising can use the Coded PHY (S=2 or S=8) for long-range
operation. The Coded PHY runs at lower data rate with FEC, and **the
receive sensitivity is ~12 dB better than 1M** — so a Coded-PHY
advertiser at 10 m may report comparable RSSI to a 1M-PHY advertiser
at 2.5 m. The path-loss model treats them as identical. For Apple
gear, AirPods, Tiles → all 1M legacy advertising, so this is fine
*today*. The day a Coded-PHY device shows up (newer beacons, some
fitness trackers) its position estimate will be ~4× too close.

The ESP32-D0WD-V3 is a BT 4.2 chip and doesn't even *receive* the
Coded PHY — so in practice we silently miss those advertisers
entirely, which is arguably worse than placing them wrong.

---

## 4. Per-leaf practical issues

### 4.1 RF front-end manufacturing variance

The ESP32 modules in the fleet are AITRIP-rebadged WROOM-32s. Variance
sources, ordered:

| Source | Typical range | Notes |
|---|---|---|
| **PCB antenna trace etching** | ±2 dB | Lithography tolerance on the IFA dimensions shifts the antenna's resonant frequency and matching across 2.4 GHz |
| **Antenna matching network components** | ±2 dB | Series inductor + shunt cap typically ±5 % values |
| **PA output power calibration** | ±3 dB | The ESP32 has a built-in PA but the per-die output power calibration is loaded from eFuse and varies by ~3 dB at the same nominal setting |
| **LNA gain + noise figure** | ±2 dB | Process variation; cumulative with PA spread |
| **Crystal/clock spread** | <0.5 dB | Frequency offset shifts the channel-filter response slightly |

**Cumulative expectation: 5–8 dB RMS spread across same-batch boards,
up to ~15 dB peak-to-peak across mixed batches.** The 19 dB spread in
the empirical decomposition is at the high end of that — consistent
with the AITRIP boards being mixed batches plus the 1× non-AITRIP
Espressif unit (`92838c`) which is from a completely different OUI and
factory.

### 4.2 Mounting / orientation as a confound

Same point as §3.3 from the other direction: in a deployed system, the
"TX bias" + "RX bias" *also includes* the average direction-of-arrival
geometry during calibration. Moving a leaf 30 cm or rotating it 45°
should invalidate its bias entry and trigger a refit. There's no such
mechanism today.

### 4.3 Temperature dependence

ESP32 PA output power has a published thermal drift of approximately
**−0.04 dB/°C** for the PA and **+0.02 dB/°C** for the LNA gain
(roughly canceling on a TX↔RX pair). Over a 20 °C ambient swing —
realistic if a leaf sits near a window or in a closet — you can see a
~1 dB net shift. Not huge, but not zero.

**More interesting** is the WiFi/BLE coexistence thermal coupling. On
the single-chip generalist node pattern (per CLAUDE.md), heavy WiFi TX
duty heats the chip 10–15 °C above ambient within 30 seconds. Then a
leaf running hot under load reports systematically different RSSI from
the same leaf right after a reboot. I'd expect ~1–2 dB of drift over
that warm-up.

We have a hook for this already: the firmware reports `WiFi.RSSI()`
and free heap in heartbeats (`main.cpp:225`). Adding an
`esp_temperature_sensor` reading to the heartbeat would let us
*observe* the effect even if we don't compensate for it.

### 4.4 Board-to-board RSSI variance observed in the wild

Quoted directly from the commit 6703762 message — the empirical
decomposition for our 8-leaf fleet:

```
4d4384  TX +7.4 dB  (shouts)
4ceb7c  TX +4.7 dB
4ce1d8  TX +4.7 dB
4db204  TX +4.7 dB
4c36ec  TX +1.4 dB
4ce184  TX -3.6 dB
92838c  TX -7.7 dB  (whispers)
4c0bdc  TX -11.7 dB (whispers harder)
```

19 dB span. **The Espressif-OUI board (`92838c`) is in the "whispers"
half — consistent with a different antenna match.** The two AITRIP
outliers (`4d4384` at +7.4 and `4c0bdc` at −11.7) are 5–6 σ from the
fleet median; I'd want to physically inspect those boards (visual
check for hairline cracks in the antenna trace, reflow quality at the
shield).

Per-leaf σ ranges 3.0–13.0 dB after fit. The two well-calibrated leaves
(σ = 3.0 dB) probably have favorable LOS geometry to most anchor
sources; the σ = 13 dB leaves are seeing structured multipath that
isn't being captured. Not a bug — just the ceiling of a free-space
model in a furnished room.

---

## 5. Recommendations (prioritized)

### P0 — do these now, low cost, high impact

#### P0-1. Capture the BLE channel in every observation

**Cost:** ~10 lines of firmware + 1 column in the obs table.
**Benefit:** Eliminates 3–10 dB of channel-aliased variance from σ_i,
unblocks per-channel calibration (3 separate P0/n fits per leaf, or
one fit with channel as a categorical covariate).

NimBLE on Arduino exposes the channel through
`NimBLEAdvertisedDevice::getPrimaryPhy()` and the underlying scan
result includes channel for legacy advertising. If the Arduino wrapper
doesn't surface it, drop to the ESP-IDF `host/nimble` API for the
scanner. The collector schema migration is trivial: `ALTER TABLE obs
ADD COLUMN chan INT;` with NULL allowed for backfill.

Pick **channel 38 (2426 MHz)** as your "calibration channel" — it's in
the middle of the band, less interfered-with than 37/39 (which sit
adjacent to WiFi channel 1 and 11). Fit the model on chan=38 obs only;
use 37/39 for extra observations at runtime with a learned
per-channel offset (one number per channel per leaf).

#### P0-2. Replicate the leaf-advert anchor capture, multiple geometries

**Cost:** A 30-minute calibration ritual + maybe a `--rotate` flag on
`scripts/set-leaf-position.py`.
**Benefit:** Direct attack on the §2 identifiability problem. With
3 replicated captures per leaf at slightly different orientations, the
bias estimate gets a real σ and the gauge between RX_bias and P0_i
becomes empirically separable.

Concretely: for each leaf, do a 60 s leaf-advert capture, then rotate
the leaf 90° on its long axis, capture again, rotate another 90°,
capture again. The mean of the 4 captures is the orientation-averaged
TX/RX bias; the spread is the antenna-pattern contribution we're
currently aliasing into the bias.

#### P0-3. Add a temperature reading to leaf heartbeats

**Cost:** 5 lines using `temperature_sensor_install()` /
`temperature_sensor_get_celsius()` (the IDF temp-sensor driver is
available on the ESP32-D0WD-V3 even though it's not advertised
prominently).
**Benefit:** Lets us *see* thermal drift even before deciding to
compensate for it. Cheap insurance.

### P1 — do these soon, medium cost, real wins

#### P1-1. Channel-aware path-loss fit

After P0-1 lands, refactor `_fit_per_leaf_models` to either:
(a) fit on chan=38 only and learn `Δ_chan` offsets per leaf for 37/39,
or (b) include channel as a categorical feature in the regression.
Option (a) is simpler and preserves the existing 2-parameter `(P0, n)`
fit; option (b) is more statistically efficient with limited data.

**Cost:** ~50 lines in `mockingbird_calibration.py`, schema already
updated by P0-1.
**Benefit:** Per-leaf σ should drop from current 3–13 dB range into
2–8 dB range, which compounds through the MLE position weight into
~20–30 % tighter position covariance ellipsoids.

#### P1-2. Use full inter-leaf obs traffic for bias decomposition, not just leaf-advert anchors

The ESP32 fleet is constantly advertising. **Every leaf sees every
other leaf's advertising traffic in the normal `obs` stream, not just
during leaf-advert calibration windows.** Modify `_fit_leaf_biases` to
collect inter-leaf pairs from the full `obs` table (filter by mac in
the known-leaf-MAC set, group by `(tx_leaf, rx_leaf)`, take recent
window).

**Cost:** Modest — extend the existing `mac_to_leaf` mapping to be
persistent (already implicit via `leaf-advert` labels), and add a
SQL pass over `obs`.
**Benefit:** Many more pair-observations per fit, much tighter bias
estimates, can re-fit nightly to track drift.

#### P1-3. Reciprocity check on the bias decomposition

Add a diagnostic: for each unordered pair {A, B}, compute
`abs(r_AB - r_BA)`. Under perfect reciprocity + IID noise this should
be small (sqrt(2)·σ_pair). Any pair where this is >> the expected
noise is a flag that the *geometry or environment* between those two
leaves is asymmetric (one of them is shadowed) — useful for catching
calibration captures that ran with somebody sitting in the room.

**Cost:** Trivial.
**Benefit:** Don't ship bad bias values silently.

### P2 — do these when there's a reason

#### P2-1. Body-occupancy term

The roadmap already mentions a PIR-augmented leaf. Once that's in,
extend the model to `RSSI = ... + B · occupancy_indicator` where
`occupancy_indicator` is 1 when a PIR within the LOS path of the
target says someone's there. Coefficient B fits to roughly −10 to
−15 dB if the geometry is right.

**Cost:** Hardware + PIR-aware regression.
**Benefit:** Removes the largest single source of *correlated*
multipath / shadowing, which is currently the floor on indoor position
stability when humans are present.

#### P2-2. Per-advertiser TX power calibration

For specific high-value advertisers (the user's phone, their watch),
fit a per-MAC TX offset against the population mean from a
constant-distance walk-through. Improves the 1.7×-distance error
mentioned in §1.

**Cost:** UI for "calibrate this device" + per-MAC table.
**Benefit:** High for tracked devices, zero for ambient ones.

#### P2-3. Antenna pattern model per leaf

Capture each leaf's mounted antenna pattern by spinning a known TX
source around it at 1 m on the floor. Fit a low-order spherical
harmonic to the per-angle RSSI. Use the per-(leaf, target-direction)
gain in the path-loss inversion.

**Cost:** Real measurement campaign, ~1 hour per leaf, plus model
changes.
**Benefit:** This is what eliminates the last 2–4 dB of residual on
well-positioned leaves. Diminishing returns relative to P0/P1, and
fragile if a leaf gets re-mounted.

#### P2-4. Coded-PHY detection

Tag observations with PHY type. Currently irrelevant (D0WD-V3 can't
receive Coded PHY) but the day Noah adds an S3 leaf, the model needs
to know.

---

## Open questions worth resolving in data, not in the head

1. Does `n_i` actually distribute tightly around the global `n`, or does
   it span widely? (Tests the §2 "n hiding inside rx_bias" concern.)
2. What's the reciprocity gap `r_AB - r_BA` for our current 8-leaf
   fleet? (Diagnostic for whether the current bias is real or
   geometric.)
3. What does σ_i look like as a function of TX-RX distance? If it
   *grows* with distance, that's multipath dominating. If it's
   distance-flat, that's chain noise. Tells us which physics to invest
   in.

Each of those is a 30-line addition to `scripts/analyze_ble_db.py`. I'd
do them before pulling the trigger on P1-1, because the answers
inform how to weight the fit.
