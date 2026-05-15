# Numerical-stability review — 2026-05-14

**Reviewer:** Eszter.
**Scope:** `services/mockingbird_calibration.py`, `services/mockingbird_tracks.py`,
`tests/test_numerical_stability.py`.
**Verdict:** **Math checks out.** Two regression tests added to lock in
properties that the existing suite did not directly exercise.

This note exists so that future-Eszter (or anyone else reading the code six
months from now) does not redo this audit. If a behavioral change to either
module makes a claim below stop being true, update or delete the relevant
paragraph rather than letting the document drift.

---

## 1. Per-leaf TX/RX bias decomposition — identifiability

**Model** (`_fit_leaf_biases`, `mockingbird_calibration.py`):
for each ordered transmitter→receiver pair (A, B) with known geometry,

```
residual_{AB} = rssi_obs - (P0_B - 10·n_B·log10(d_{AB})) = TX_A + RX_B + noise
```

The residual vector is a sum of two per-leaf scalar effects. This is the
classic additive two-way decomposition — *with exactly one gauge freedom*:
for any constant `c`,

```
TX_i' = TX_i + c        RX_i' = RX_i - c
```

leaves every residual invariant. The system has rank `2L - 1` in `2L`
unknowns. Without an anchor it is non-identifiable; the ALS iteration would
drift along the gauge direction step by step.

**Anchor.** `_fit_leaf_biases` enforces `mean(TX) = 0` at the end of every
TX update (lines 381–384). This fixes the gauge uniquely. The
implementation does *not* re-anchor after the RX update — and shouldn't,
because once `mean(TX) = 0` is set, the gauge is closed: the next TX
update produces a new `mean(TX)` which is then re-centered, and the
fixed point is unique.

**Modeling consequence worth knowing.** With `mean(TX) = 0` as the anchor,
any *global* calibration offset (e.g. every leaf systematically reports 3 dB
hotter than the global `P0` fit predicts) gets attributed to `RX`, not split
between TX and RX. That is a reasonable convention — the global offset has
already been folded into `P0` via the path-loss fit — but it does mean
`mean(RX)` carries the bulk-calibration residual and should not be
interpreted as a per-leaf antenna property in absolute terms. Only
differences between RX biases are physically meaningful.

**Verified empirically.** Synthetic ground-truth recovery test
(`test_leaf_bias_decomposition_is_identifiable_up_to_gauge`, added in this
review) generates known TX, RX with `mean(true_TX) = 0`, runs the exact
ALS loop, and confirms:

- recovered TX matches true TX within noise (since the gauge anchors them
  identically),
- recovered RX matches true RX up to a global constant — i.e.
  `recovered_RX - true_RX` has spread < 0.1 dB across all leaves, while
  the constant itself is the bulk-calibration carry-through.

This is the correct behavior. **No fix required.**

## 2. Kalman covariance update — Joseph form, not naive

The update in `_joseph_update` (`mockingbird_tracks.py`, lines 380–448)
implements

```
P_post = (I - K H) P_prior (I - K H)ᵀ + K R Kᵀ
```

with `H = [I₃ | 0]` (we observe position only). It is **not** the naive
short form `(I - K H) P`. That distinction matters because:

- `(I - K H) P` equals Joseph form **only at the exact optimal gain**
  `K = K_opt = P Hᵀ S⁻¹`. In floating point our K is off-optimal by
  O(ε·‖P‖); the short form then drifts to *asymmetric, eventually
  indefinite* P over hundreds of updates.
- Joseph form is the literal post-update error covariance under any K,
  optimal or not, and is therefore unconditionally symmetric and PSD in
  exact arithmetic. In floating point we still pick up O(ε) asymmetry from
  the multiplies; `_joseph_update` explicitly symmetrizes
  `P ← (P + Pᵀ)/2` at the end.

`test_joseph_matches_short_form_at_optimal_gain` checks the algebraic
equivalence at `K_opt`, and `test_joseph_output_psd_under_many_iterations`
runs 500 iterations with a deliberately suboptimal `K`. I added
`test_joseph_psd_under_asymmetric_random_gain` (300 iterations, fully
random non-symmetric K) to nail down that the *exact* symmetry survives
arbitrary gain shapes — this is the regression test that will fire if
someone ever drops the trailing `P ← (P + Pᵀ)/2` line as a "perf
optimization."

**Confirmed: Joseph form. Symmetric. PSD-preserving. No fix required.**

## 3. Covariance symmetry / PSD enforcement

Three places where covariance is touched in the track module, in
processing order:

- `_clamp_pos_cov` (3×3 measurement covariance from MLE): symmetrizes the
  off-diagonals via `(C_ij + C_ji)/2`, clamps diagonal to a sane
  `[min_std², max_std²]` band, and limits off-diagonal magnitudes to keep
  correlations below ±0.95. Output is symmetric by construction and
  diagonally-dominant enough to be safely PSD.
- `_joseph_update` (6×6 state covariance): explicit symmetrize at the end
  as discussed above.
- `_fuse_entities` builds an information-form sum `Σᵢ Σᵢ⁻¹` and inverts.
  Each `Σᵢ` is already a Joseph-output `last_cov_3x3`; the sum of symmetric
  matrices is symmetric; the inverse of a symmetric matrix is symmetric
  (the cofactor formula in `_invert_3x3` produces an exactly symmetric
  result for symmetric input, modulo floating-point round-off, which is
  ≤ a few ulps for these dimensions). No explicit symmetrize after the
  fuse, but the resulting `Σ_fused` is then immediately fed to
  `_entity_kalman_step` which Joseph-updates from there. **Tolerable** —
  any residual asymmetry is sub-ppm and gets washed in the first update.

**No PSD drift observed across the suite. No fix required.**

## 4. Tikhonov regularization — value, rationale, and behavior

Constant: `_EPS_TIKHONOV = 1e-10` (also `_EPS_TIKHONOV_LOCAL = 1e-10` in the
tracks module — duplicated to avoid a hot-path cross-module import; the
two are kept in lock-step by `test_tikhonov_local_matches_calibration`).

**Form.** `λ = ε · |tr(M)|`, applied as `M_reg = M + λ·I`. This is
*scale-adaptive*: doubling M doubles λ. The relative perturbation is
constant at `~ε · 3` (since tr is a sum over 3 diagonals), which is what
we want — Tikhonov should act as a fraction of the spectral scale, not as
an absolute floor that depends on the units chosen.

**Why this value?**

- For a well-conditioned matrix where `λ_min(M) ≫ ε·tr(M)`, the
  perturbation is ≤ a few · 10⁻¹⁰ of the smallest eigenvalue. The solve
  is indistinguishable from the un-regularized solve in double
  precision. Tested by `test_tikhonov_noop_on_well_conditioned` and
  `test_tikhonov_solve_recovers_exact_for_well_conditioned`.
- For a rank-deficient matrix, the smallest eigenvalue is lifted to
  ≈ `λ = ε·tr(M)`, bounding the condition number after regularization at
  roughly `1/ε ≈ 10¹⁰`. That is solvable in double precision (≈ 6 digits
  of margin against the 16 in IEEE-754) without catastrophic loss of
  significance. Tested by `test_tikhonov_handles_singular_matrix`.

**Why fixed rather than adaptive?** Adaptive λ (e.g. scaled to the
estimated noise) would be marginally tighter for well-conditioned matrices
but invites a second parameter we'd need to tune and validate. The fixed
scale-relative form gets the dominant benefit (no cliff at rank
deficiency) at the cost of a sub-ppm perturbation when things are healthy
— which we can never measure anyway. **Keep fixed.**

**Trade-off acknowledged.** In a rank-deficient case Tikhonov returns the
*minimum-norm* solution along the deficient direction. For position
estimation that means "shrink toward the origin." For the MLE in
`mle_multilaterate` that is harmless because the solve produces an
*increment* `dp` on the position, not the position itself — shrinking dp
toward zero in the deficient direction is the correct conservative move.
For the Kalman innovation covariance `S` in `kalman_step`, regularizing
shrinks the gain `K = P Hᵀ S⁻¹` toward zero in the deficient direction,
which is also correct: if S is rank-deficient, the measurement has no
information in that direction and the right thing to do is trust the
prior.

## 5. Items I noticed but did not fix

- `_fuse_entities` uses `_invert_3x3` (hard `|det| < 1e-12` gate) rather
  than `_tikhonov_solve_3x3`. If a single track's covariance becomes
  nearly singular, that track is silently dropped from the fusion. This
  is fine in practice (a near-singular `last_cov_3x3` means the geometry
  for that track was degenerate, so excluding it from the fuse is
  arguably the right call), but the code path is inconsistent with the
  Tikhonov-everywhere convention elsewhere. **Leave for now.** Worth
  revisiting only if we ever see fused entity counts drop unexpectedly.
- `_joseph_update` is O(6³) = 216 inner-multiply ops per call, executed
  in pure Python. On the Pi Zero W that is the hot path of the dashboard.
  If profile ever shows this as a bottleneck, the right move is to
  vectorize via NumPy — not to switch to the short form. Ethan owns this.

## 6. Test coverage summary

| Property | Test |
| --- | --- |
| Tikhonov no-op on well-conditioned M | `test_tikhonov_noop_on_well_conditioned` |
| Tikhonov solve matches direct solve | `test_tikhonov_solve_recovers_exact_for_well_conditioned` |
| Tikhonov salvages singular system | `test_tikhonov_handles_singular_matrix` |
| Tikhonov copies aligned across modules | `test_tikhonov_local_matches_calibration` |
| Tikhonov scale invariance | `test_tikhonov_scale_invariance` |
| Joseph with K=0 is identity | `test_joseph_zero_gain_is_identity_on_P` |
| Joseph output exactly symmetric | `test_joseph_output_is_symmetric` |
| Joseph PSD under repeated suboptimal-K updates | `test_joseph_output_psd_under_many_iterations` |
| Joseph PSD under random asymmetric K (added) | `test_joseph_psd_under_asymmetric_random_gain` |
| Joseph algebraically matches short form at K_opt | `test_joseph_matches_short_form_at_optimal_gain` |
| Bias decomposition identifiable up to gauge (added) | `test_leaf_bias_decomposition_is_identifiable_up_to_gauge` |
