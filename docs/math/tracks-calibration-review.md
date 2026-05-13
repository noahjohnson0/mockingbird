# Mathematical review — `mockingbird_tracks.py` + `mockingbird_calibration.py`

Reviewer: Eszter Kovács (ESZ-1). Files reviewed at commit `6703762`
(`Per-leaf TX/RX bias decomposition`). Line references are against that
revision.

This is a review of the *math*, not of the code style. The implementation
is, in places, mathematically correct but numerically dangerous; in other
places, mathematically wrong but quantitatively harmless because the
errors lie inside the dB noise floor. I distinguish the two below.

The single most important finding is in §3.1: the position covariance
Σ_p returned to the Kalman filter is computed against the wrong objective
and is **scale-wrong by orders of magnitude** depending on geometry. Read
that first.

---

## 1. Formulations

### 1.1 Per-leaf log-distance path-loss model

For receiver leaf `i`, source-to-leaf distance `d_i`:

    rssi_i = P0_i − 10 · n_i · log10(d_i) + ε_i,   ε_i ~ N(0, σ_i²)

Implemented in `_fit_per_leaf_models` (calibration.py:429) via OLS of
`y = rssi`, `x = −10·log10(d)`, slope = `n`, intercept = `P0`. The
residual std `σ_i` is stored per-leaf and used as a per-measurement
uncertainty downstream.

Implicit assumptions:

- **ε_i is additive Gaussian in dB.** In reality RSSI noise has a heavier
  upper tail (occasional NLOS dips of 10–20 dB) and is bounded below by
  receiver sensitivity (~ −95 dBm). The Gaussian assumption is benign for
  MLE position weights but biases σ_i upward when the training set
  contains a few NLOS dips. Vlad will recognize this from his physics
  review.
- **ε_i is independent across leaves.** Multipath says otherwise — two
  leaves on the same wall see correlated dips. The covariance treatment
  downstream (§1.3) ignores this; positions will look more certain than
  they are.
- **σ_i estimated from training is valid at inference.** Only if the
  geometry of inference is statistically similar to the geometry of
  calibration. A leaf calibrated by walking near it will under-estimate σ
  for a device 6 m away.

### 1.2 Per-leaf TX/RX bias decomposition

`_fit_leaf_biases` (calibration.py:304). For each ordered pair (A
transmitting, B receiving) at known distance `d_AB`:

    residual_AB = rssi_AB − (P0_B − 10·n_B·log10(d_AB))
                ≈ TX_A + RX_B + multipath_AB + ε

Two-way ALS: alternate `TX_A ← mean_B(residual_AB − RX_B)` and
`RX_B ← mean_A(residual_AB − TX_A)`. Identifiability constraint
`mean(TX) = 0` is imposed once per outer iteration (line 406).

This is a **rank-1 deficient model** without the constraint: replacing
`(TX, RX)` with `(TX + c, RX − c)` leaves every residual unchanged. The
sum-to-zero constraint resolves the degeneracy. After convergence the
*pair-specific* residual is interpreted as multipath and stored for IDW
interpolation at inference.

### 1.3 MLE multilateration (`mle_multilaterate`, calibration.py:583)

For candidate position `p ∈ ℝ³`, predicted RSSI on leaf `i`:

    rssi_hat_i(p) = P0_i − 10·n_i·log10(‖p − x_i‖) − RX_i − multipath_i(p)

The code does **not** minimize the natural log-likelihood

    L(p) = ½ · Σ_i (rssi_i − rssi_hat_i(p))² / σ_i²       (the dB-domain LL)

Instead it transforms each measurement to a distance estimate

    d_i_obs = 10^((P0_i − rssi_corrected_i) / (10·n_i))

and minimizes a **distance-domain** weighted sum of squares

    Σ_i w_i (d_i_obs − ‖p − x_i‖)²,    w_i ≈ 1 / σ_{d,i}²

with `σ_{d,i} = d_i · σ_i · ln(10) / (10·n_i)` (delta method).

These two objectives are *not* equivalent — they give different minimizers
unless σ_i is uniform and noise is small. See §3.1.

Algorithm: weighted linearization (Bancroft-style: subtract reference
equation 0 → linear in `p`), then up to 6 Gauss-Newton iterations with
IRLS-Huber reweighting (k=1.5) and a per-iteration multipath refresh.
Step length is clipped to 2 m.

Position covariance is reported as `Σ_p = (JᵀWJ)⁻¹` from the final
iteration (line 740). This is **the formula for the unweighted-Gauss-
Newton residual covariance under unit-variance residuals**; the units
are wrong relative to the weighted objective. See §3.1.

### 1.4 Kalman update (per-track and per-entity)

Constant-velocity 6-state, position-only measurement:

    x_{k+1} = F·x_k + w,   w ~ N(0, Q)
    z_k     = H·x_k + v,   v ~ N(0, R)
    F = [[I, dt·I], [0, I]],  H = [I, 0],  Q = diag(q_p, q_p, q_p, q_v, q_v, q_v)

Standard textbook predict/update. R is taken from `last_cov_3x3` (clamped
to [σ=10cm, σ=5m] per axis). After update, several non-Kalman heuristics
are applied:

- **ZUPT** (zero-velocity update) on stationary detection: hard-zero the
  velocity components, shrink velocity covariance to ~0.01 (lines 302–307).
- **Velocity magnitude clamp** at 0.20 m/s (line 341).
- **Stationary anchor**: recursive mean of measurements, then *overwrite*
  the Kalman position state with the anchor (lines 313–330). This is a
  parallel filter that bypasses the Kalman covariance entirely.
- **State sanity check + re-init** if any |pos|>50 or |vel|>5 or jump>5m
  (lines 138–142, 210).

### 1.5 EWMA position smoothing (Track.update, lines 426–505)

Two parallel position-smoothing pipelines exist:

1. The Kalman filter on `kf_state`, producing `kf_pos` returned to caller.
2. A separate adaptive-α EWMA on `last_position` (the position that the
   dashboard actually renders), with α a piecewise-linear function of
   jump distance, plus a stationary-anchor override (lines 449–504).

These two filters operate on the same measurement stream in *series*:
`kf_pos` from `kalman_step` is fed back into `track.update()`
(line 914) which then EWMA-smooths it and applies its own stationary
anchor. The displayed position therefore has **two stacked smoothers and
two stacked stationary-anchor logics** — see §4.2.

### 1.6 Entity-level information-form fusion (`_fuse_entities`, lines 748–842)

For N member tracks of one entity, with each track contributing position
`μ_i` and covariance `Σ_i`:

    Σ_fused⁻¹ = Σ_i Σ_i⁻¹
    μ_fused   = Σ_fused · Σ_i Σ_i⁻¹ μ_i

This is the optimal linear unbiased estimator *if the `μ_i` are
independent and unbiased estimates of the same quantity*. Both
assumptions are violated:

- **Bias**: each `μ_i` is the EWMA-smoothed-then-Kalman-smoothed track
  position. By construction it is a biased estimate of the instantaneous
  true position (lag in proportion to α and Q).
- **Independence**: tracks for the same entity share an antenna and an
  RF chain. Their RSSI noise is highly correlated. The fused covariance
  scales as `Σ/N` but the true variance reduction is closer to `Σ/N_eff`
  where `N_eff ≪ N` for tightly co-located radios.

The code comment on line 759 acknowledges this. The fix is one of:
(a) feed *raw* per-track MLE measurements into the fusion, not smoothed
positions; (b) inflate Σ_i by an empirical correlation factor before
inversion. See §5.

---

## 2. Well-posedness

### 2.1 Path-loss fit (`_linear_fit`)

Becomes singular when `var(x) → 0`, i.e. all distances similar. Caught
at line 522 (`sxx < 1e-9`). Adequate.

Two failure modes not caught:

- **Distance range too narrow** — `n` can be fit with huge variance even
  when the regression is well-conditioned in the algebraic sense. With
  only points between 1 m and 2 m, slope std is ~5× what it is with the
  same number of points spread 0.5–8 m. No CRLB or condition-number gate
  on `n` reported.
- **Per-leaf models with `min_pts=3` are essentially uncalibrated** —
  three points in 2D give zero degrees of freedom for σ. The σ_i fed to
  MLE weights is meaningless for any leaf with <~6 anchor points.

### 2.2 TX/RX bias decomposition

Rank deficiency `(TX, RX) ≡ (TX+c, RX−c)` correctly resolved by sum-to-
zero constraint. A *second* gauge ambiguity exists if the bipartite
graph of (transmitter, receiver) pairs is not connected: TX biases in
one connected component can shift relative to another. With 8 leaves
all advertising and all receiving, the graph is K_{8,8} minus self-
loops, fully connected; not a practical concern at current fleet size.
**Worth flagging** if some leaves go offline during calibration — then
identifiability can break silently.

### 2.3 Multilateration geometry

Multilateration is non-convex. The linearized initial estimate is **not
guaranteed** to land in the basin of the global minimum:

- If the leaves are nearly coplanar (e.g. all ceiling-mounted), the z
  axis is poorly observed. JᵀWJ becomes ill-conditioned along z. The
  3×3 inverse used (`_invert_3x3`) inverts via cofactors with no
  conditioning check — det>1e-12 is *not* a condition-number gate.
  Practically: small det relative to ‖JᵀWJ‖ produces a huge `Σ_p` in
  z but no warning is emitted.
- With exactly 4 leaves, the linearization can have a sign ambiguity in
  the reflection across the leaf plane. The Gauss-Newton refinement
  preserves whichever side the linearization picked; you can get a
  "phantom ceiling" position when the true position is on the floor and
  vice versa. The `bounds` check at line 732 catches the most extreme
  cases but not subtle z-flips inside the room.
- "Reference leaf" for linearization is always `items[0]` (line 651).
  Picking the closest or best-conditioned leaf as reference (rather than
  arbitrary first) reduces bias and improves conditioning. Cheap fix.

### 2.4 Distance-vs-RSSI domain transformation is biased

`d = 10^((P0 − rssi)/(10n))` is a nonlinear monotone transform of a
Gaussian RSSI. If `rssi ~ N(μ, σ²)`, then `E[d]` is *not*
`10^((P0 − μ)/(10n))`; it is

    E[d] = 10^((P0 − μ)/(10n)) · E[10^(−ε/(10n))]
         = d_true · exp(½ (σ·ln10/(10n))²)

For σ=5 dB, n=2.5, the multiplicative bias on `d` is ~1.11 — distance
over-estimated by 11%. This pushes every position estimate *outward*
from each leaf by a leaf-dependent amount, which the MLE then averages.
Net effect on position is geometry-dependent but typically 10–30 cm of
systematic outward bias. **The dB-domain MLE in §1.3 would avoid this
entirely.** See §5 P1.

---

## 3. Numerical stability

### 3.1 [P0] Position covariance is computed against the wrong objective

`mle_multilaterate` (line 740):

    cov_3x3 = _invert_3x3(JtWJ)

`JᵀWJ` here is the Hessian of the **distance-domain** objective
Σ w_i (d_i − ‖p − x_i‖)². For a properly-normalized weighted-least-
squares Hessian to equal the inverse Fisher information, the weights
must equal the *inverse measurement variances*. The code sets

    w_i = 1 / σ_{d,i}²,   σ_{d,i} = d_i_obs · σ_i · ln10 / (10·n_i)

But `σ_{d,i}` is recomputed at the *initial* `items_at(init_p)` (line 646)
and is **not refreshed** with the multipath-corrected `d_i` inside the
loop. As the multipath correction shifts `d_i_obs` by up to ±2 m near
walls, σ_{d,i} is then wrong by a factor of `d_new / d_init` — typically
within 2× but occasionally 4×, and the weights enter Σ_p quadratically.

In addition, even with correct weights, `(JᵀWJ)⁻¹` is the covariance of
the *estimator* only if the residuals are zero-mean and uncorrelated
with the assumed variance. The actual residual RMS computed at line 747
is *not* compared to the assumed `σ_d` — any mismatch should rescale
the covariance:

    Σ_p_correct = (JᵀWJ)⁻¹ · s²,   s² = rᵀWr / (m − 3)

where `m = len(items)`. The downstream Kalman filter trusts Σ_p as
absolute, so when MLE is over-confident (s² > 1) the KF tracks too
aggressively; when under-confident (s² < 1) the KF leans on the prior
and motion lags.

**Impact:** the entity-fusion code in §1.6 uses this Σ_p as `Σ_i` in
the information-form sum. A miscalibrated Σ_p across N tracks
*multiplies* through. The 23 cm fused-σ claim in commit `a1a72b0` is
believable as RMSE but not as a covariance: it likely contains
substantial systematic bias the covariance does not see.

### 3.2 [P0] Naïve `_invert_3x3` with `det < 1e-12` gate

`_invert_3x3` (calibration.py:757) and `_invert_3x3_local` (tracks.py:348).
Both check absolute determinant against 1e-12. This is **not** an
ill-conditioning check; it is a singularity check. A 3×3 with
‖M‖ = 1e6 and det = 1e-10 passes the test, but the inverse has
elements ~1e16 and any subsequent operation is poison.

The right check is the condition number, or — even more robust — a
Cholesky or LDLᵀ factorization that fails gracefully. For 3×3 the
cheapest robust solve is Cholesky with a small Tikhonov regularization
`M + εI`:

    M' = M + ε · trace(M) · I,   ε ~ 1e-8

then Cholesky-solve. Always-succeeds, biases inverse mildly, and
preserves PSD-ness. Recommended over the cofactor inversion.

### 3.3 [P0] Kalman covariance update form is non-stable

`kalman_step` (lines 286–293):

    P = (I − KH) · P

This is the **textbook short form**, not the **Joseph form**

    P = (I − KH) · P · (I − KH)ᵀ + K·R·Kᵀ

The short form is algebraically identical to Joseph only when K is
exactly optimal — round-off pushes K off-optimal each iteration. The
short form does not preserve symmetry or PSD-ness. After many
iterations, P drifts asymmetric and acquires negative diagonal
entries, especially when measurement noise is much smaller than
process noise (our case under multi-track fusion). The `_kf_state_
looks_corrupt` re-init then fires for the wrong reason — it sees
diverging velocity, but the root cause was P drifting non-PSD.

Joseph costs ~3× the flops; on a Pi Zero, with one Kalman step per
track per ~500 ms, this is irrelevant.

Additionally, symmetry should be explicitly re-imposed each step:
`P = 0.5 (P + Pᵀ)`. This is one statement and bounds asymmetry
growth to round-off.

### 3.4 [P1] Stationary anchor unbounded mean

In `Track.update` (line 487) the rolling stationary mean uses
`eff_n = min(n, 40)`. Good — bounded so a single big jump can shift
the anchor. But in `_entity_kalman_step` and in `kalman_step`'s ZUPT
branch (lines 322, 829) the same recursive mean is *unbounded*:

    anchor ← anchor + (z − anchor)/(n+1)

For a track that has been stationary 1 hour at ~2 Hz updates,
`n ≈ 7200`. A new measurement contributes 1/7200 ≈ 1.4e-4 weight.
This is fine when stationary, but the *exit* from stationarity is
correspondingly hard — the anchor takes minutes to drift to a new
position. The intentional "snap" at line 327 then masks real motion
for 5–30 s after a stationary device starts moving.

Bound `eff_n` to the same 40 cap as `Track.update` does, or use an
explicit EWMA with α matched to the expected motion timescale.

### 3.5 [P1] Linearization reference equation choice

In `mle_multilaterate` line 651 and `multilaterate` line 809, the
"reference" equation for the Bancroft-style linearization is always
`items[0]` / `dists[0]`. This is dict-iteration order — arbitrary.

The information content of the linearization depends on which leaf is
subtracted out:

- if `items[0]` is far away (large `d_0`), it has high distance
  variance and the linearization residual blows up;
- if `items[0]` is the worst-calibrated leaf, every other equation
  inherits its bias.

Best practice: pick the leaf with the strongest RSSI (smallest
σ_{d,i}) as reference. Free improvement.

### 3.6 [P1] Normal-equations form for the multilateration linear stage

The linearization at line 660 builds `AᵀWA` and `AᵀWb` explicitly,
then `_solve_3x3` does Gauss elimination. The condition number of
`AᵀWA` is the *square* of the condition number of A; for marginal
geometries (3 of 4 leaves nearly colinear) this can lose 4–6 digits
of precision unnecessarily. The fix on a Pi Zero where numpy is not
available is **QR via Givens rotations**: 3×3 right-triangular factor
solved by back-substitution. ~30 lines of code, replaces `AᵀA + solve`
with `qr + triangular_solve`, and condition number stays ~κ(A) not
κ(A)².

For our geometry (8 leaves in a ~6×6 m room, n_i in [2, 4.5], RSSI ±5
dB) κ(A) is order 10²–10³, so normal equations cost 4–6 digits — still
within float64 budget. **This is a P1, not P0, because it's not
currently producing visible error**, but it's the kind of thing that
bites when a future deployment puts most leaves on one wall.

### 3.7 [P2] `_clamp_pos_cov` correlation clamp does not preserve PSD-ness

Lines 363–379: clamps off-diagonals so each `|ρ_ij| < 0.95`. This is
correct for the 2×2 sub-blocks but **does not guarantee the 3×3 is
PSD** — three pairwise correlations can each be 0.94 and the 3×3 still
have a negative eigenvalue (the "correlation triangle inequality").

Correct alternative: do an eigen-decomposition, clamp eigenvalues to
[min_var, max_var], reconstruct. For 3×3 this is closed-form (cubic
characteristic polynomial). At our update rate this is sub-microsecond.

Mild in practice because R is then added to P_prior which is large and
diagonally dominant, but it is mathematically incorrect.

### 3.8 [P2] EWMA on RSSI fingerprints discards observation count

`Track.update` line 437: `fp ← (1 − α)·fp + α·rssi`, α = 0.35 fixed.
Equivalent effective sample size ≈ `2/α − 1 ≈ 4.7`. A track that has
been observed 200 times effectively only remembers the last ~5
observations. This is *intended* (so it can track slow RSSI changes
as the person moves), but it means the fingerprint match RMS used for
entity clustering (`ENTITY_RMS_THRESHOLD`) is comparing two estimators
each with std ≈ σ_RSSI / √5 ≈ 2.2 dB. The threshold of 4.5 dB is then
2σ — appropriate, but only by coincidence. If α changes the threshold
must change.

A weighted-running-mean with explicit `(sum, sum_sq, n)` triple gives
both the mean *and* its own σ, letting the entity match threshold
adapt automatically.

---

## 4. Convergence and tuning

### 4.1 Gauss-Newton convergence

In `mle_multilaterate`, 6 GN iterations with step-clip 2 m and
termination at `‖dp‖ < 1 cm`. The step clip is doing real work — for
ill-conditioned geometries the unclipped step can be tens of meters,
and clipping converts blow-ups into bounded oscillation. The trade-off
is rate: clipped steps need O(diameter/2 m) ≈ 3 iterations just to
walk across the room. 6 iterations is on the edge of "enough" when the
initial Bancroft estimate lands 5 m from the truth (which happens for
NLOS or wall-bounce dominated devices).

There is **no convergence check on the objective value** — only on
step length. If the IRLS Huber reweighting changes the objective in a
way that increases the residual, the code happily takes the step
anyway. A monotone-descent check (`if r_new > r_old: shrink step`)
would harden this.

### 4.2 Two-EWMA-and-an-anchor stack

The displayed position passes through (in order):

1. Bancroft + 6 GN iterations of distance-domain WLS  →  `mle_pos`
2. Kalman filter (constant velocity, R from Σ_p)      →  `kf_pos`
3. Adaptive-α position EWMA (jump-dependent α)        →  `last_position`
4. Stationary-anchor recursive mean                   →  override

Each layer adds smoothing. The composite step response on a stationary
device snapping to the anchor is dominated by layer 4 (40-sample cap →
~20 s settle). The composite step response on a moving device is
dominated by layer 2 (Kalman with `q_pos = 0.005·dt` → time constant
~1–2 s). Layer 3's adaptive-α is doing little once layers 2 and 4 are
in place — a stationary device is anchored, a moving device is
Kalman-smoothed, and the EWMA only acts during the transitional 1–2
samples per state change. **It is mostly dead code at the math level.**

This is not wrong, just expensive complexity. Removing the EWMA and
tuning Q in the Kalman to match the desired transient response is the
clean version. See §5 P1.

### 4.3 ALS bias decomposition: 20 iterations, no convergence check

`_fit_leaf_biases` line 396: `for _ in range(20)`. For a well-posed
ALS this converges in 4–6 iterations; for ill-posed it can oscillate
indefinitely. No `‖Δbias‖ < tol` check. Mostly harmless because the
system is small (~16 unknowns) and well-determined (~56 equations),
but a convergence-or-bust check is cheap insurance.

### 4.4 Tuning knobs and what they trade

- `EWMA_ALPHA = 0.35` (fingerprint smoothing) — trades match-stability
  vs. responsiveness to RSSI drift. Tied to `ENTITY_RMS_THRESHOLD` via
  effective-sample-size; should be co-tuned.
- `MATCH_RMS_DBM_THRESHOLD = 6.0` — sets the per-MAC match radius in
  fingerprint space. Higher = more rotation-survival, more cross-device
  false merges. With σ_RSSI ≈ 5 dB and √n_shared ≈ √2, the natural
  detection threshold from a Neyman-Pearson view is ~5–6 dB. 6.0 is
  reasonable.
- `ENTITY_RMS_THRESHOLD = 4.5` — tight for same-antenna pairs (σ_RSSI
  shared antenna ≈ 1–2 dB), loose enough for ESP32 RX noise.
- `q_pos, q_vel` (Kalman process noise) — *the* knob for motion
  responsiveness. Currently `q_pos = 0.005 dt`, `q_vel = 0.04 dt`. The
  ratio `q_pos / q_vel = 0.125` gives a Kalman time constant of
  ~√(0.04/0.005)·dt ≈ 2.8 dt seconds = ~1.4 s at 2 Hz polling.
  Reasonable for a human walker; too slow for a thrown phone.
- `HUBER_K = 1.5` — Huber transition at 1.5 m residual. Compares to
  σ_d ≈ 0.7–1.5 m for typical leaves. So ~1σ — *aggressive* down-
  weighting of even moderate residuals. Statistically optimal would be
  ~1.345·σ (Huber's classical recommendation for 95% efficiency at the
  Gaussian). Currently the constant is hardcoded in meters rather than
  in σ-units, so as σ varies across leaves the effective robustness
  varies. Should be `HUBER_K = 1.345 · σ_d_i` per leaf.

---

## 5. Recommendations (prioritized)

### P0 — Correctness or safety. Fix before next physics-claim experiment.

**P0-1. Fix the position covariance in `mle_multilaterate`.**
File: `services/mockingbird_calibration.py:740`. Today:

    cov_3x3 = _invert_3x3(JtWJ) or fallback

Change to:

    s_sq = (rᵀ W r) / max(1, m − 3)             # residual scale
    cov_3x3 = (JᵀWJ)⁻¹ · s_sq                    # rescaled Fisher inverse

with `r, W` computed from the *final-iteration* residuals and weights,
*after* the multipath-corrected `d_i` has been refreshed (i.e. recompute
`σ_{d,i}` inside the loop, not at init). Without this rescale, the Σ_p
fed to the Kalman filter and to the entity information-form fusion is
miscalibrated by a factor that can be 0.25× – 4×. Math impact: the
fused-σ numbers in commit messages should be treated as RMSE not as
σ.

**P0-2. Replace `_invert_3x3` + `_invert_3x3_local` with Tikhonov-
regularized Cholesky solves.**
Files: `mockingbird_calibration.py:757`, `mockingbird_tracks.py:348`.
Today: cofactor inverse, det>1e-12 gate. Change to:

    M' = M + ε · trace(M) · I,    ε = 1e-8
    L = chol(M')
    solve(M', b) via two triangular solves

Replace every site that *inverts then multiplies* (e.g. `K = P Hᵀ S⁻¹`)
with a solve: solve `S X = (P Hᵀ)ᵀ`, then `K = Xᵀ`. The current code
forms `S_inv` explicitly and matrix-multiplies (tracks.py:271, 280).
Numerical-stability gain: trivial flop cost, dramatically more robust
to ill-conditioned geometry (z near-coplanar leaves, near-degenerate
multilateration).

**P0-3. Joseph-form covariance update in both Kalman filters.**
Files: `mockingbird_tracks.py:286-293` and `mockingbird_tracks.py:620-625`.
Replace `P ← (I − KH) P` with

    P ← (I − KH) P (I − KH)ᵀ + K R Kᵀ
    P ← 0.5 (P + Pᵀ)                           # explicit symmetry

Cost: ~3× the flops of the short form (still microseconds). Benefit:
P stays PSD across thousands of updates without `_kf_state_looks_corrupt`
having to fire as a safety net.

### P1 — Mathematically sounder. Schedule for next iteration.

**P1-1. Move MLE to the dB-domain log-likelihood.**
File: `mockingbird_calibration.py:583-754`. Today: distance-domain WLS.
Change to: minimize

    L(p) = ½ Σ_i (rssi_i − [P0_i − 10 n_i log10‖p − x_i‖ − RX_i − mp_i(p)]) ² / σ_i²

Jacobian row for leaf i: `−10 n_i / (ln 10 · ‖p − x_i‖²) · (p − x_i)`.
Eliminates the multiplicative bias in §2.4 (~10–30 cm systematic
outward push) and makes the σ_i weights directly meaningful. The
Bancroft initialization stays as a warm start; only the inner GN loop
changes objective.

**P1-2. Pick the reference leaf in Bancroft linearization by signal
quality, not iteration order.**
File: `mockingbird_calibration.py:651` and `:809`. One-liner: sort
`items` by `(σ_{d,i}, d_i)` ascending and take index 0 as reference.

**P1-3. Bound the stationary-anchor sample count globally to ~40.**
Files: `mockingbird_tracks.py:322` and `:830` (the kalman_step ZUPT
branch and the `_entity_kalman_step` ZUPT branch). Match what
`Track.update` already does at line 486. Otherwise exit-from-stationary
is artificially sluggish after long idle periods.

**P1-4. Per-leaf scale `HUBER_K` to that leaf's σ_d.**
File: `mockingbird_calibration.py:679`. Replace `HUBER_K = 1.5` (meters)
with `HUBER_K_i = 1.345 · σ_{d,i}` evaluated per leaf inside the loop.
Restores 95% Gaussian efficiency and matches the classical Huber
recommendation.

**P1-5. Drop the redundant adaptive-α EWMA layer.**
File: `mockingbird_tracks.py:449-464`. Layers 2 (Kalman) and 4
(stationary anchor) already provide motion smoothing + stationary
locking. The EWMA layer's effect is small and obscures the math. Tune
the Kalman Q to recover whatever transient response the EWMA was
providing.

### P2 — Hygiene. Worth doing when adjacent code is touched.

**P2-1. QR (Givens) for the multilateration linear stage.**
File: `mockingbird_calibration.py:660`. Replace `AᵀWA` build + `_solve_3x3`
with weighted-QR. Saves squaring κ. Not currently producing visible error
but cheap insurance.

**P2-2. PSD-preserving covariance clamp via eigen-clamp.**
File: `mockingbird_tracks.py:363-379`. Replace pairwise correlation
clamp with eigen-decomposition + eigenvalue clamp.

**P2-3. ALS convergence check in TX/RX decomposition.**
File: `mockingbird_calibration.py:396`. Break loop when
`max(|Δtx|, |Δrx|) < 0.05 dB`.

**P2-4. Inflate fused covariance for inter-track correlation.**
File: `mockingbird_tracks.py:748` (`_fuse_entities`). For an entity
with N member tracks sharing one antenna, replace

    Σ_fused⁻¹ = Σ_i Σ_i⁻¹

with

    Σ_fused⁻¹ = (1/N_eff) Σ_i Σ_i⁻¹,   N_eff ≈ 1 + (N − 1) · (1 − ρ)

where ρ ∈ [0, 1] is an empirical inter-track RSSI correlation (estimate
from the residuals of any pair seen for >30 s). Today's code reports a
σ that is roughly `σ_track / √N`; the realistic σ is closer to
`σ_track / √N_eff`. The dashboard "23 cm σ" is therefore a lower bound,
not a confidence radius.

---

## 6. What I have *not* reviewed

- **Calibration data harvesting from SQLite** — schema, indexing,
  time-window correctness. Out of scope for math review.
- **`mockingbird-dashboard.py`** — the calling code. Some of the
  recommendations above assume the dashboard does not work around
  Σ_p miscalibration (e.g. doesn't independently rescale). Worth a
  follow-up sweep.
- **Vlad's physics model** of multipath. The IDW interpolation in
  `_interpolate_multipath` is mathematically what it is, but whether
  it is a good *physical* model is Vlad's call.
- **Per-leaf calibration data sufficiency.** Today `min_pts = 3`
  inside `_fit_per_leaf_models` is far too few for a reliable σ_i.
  Recommend raising to 6–8 once the anchor collection workflow has
  caught up, and falling back to the global `(P0, n, σ)` until then.

— Eszter
