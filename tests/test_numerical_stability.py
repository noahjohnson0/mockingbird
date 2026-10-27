"""Numerical-stability tests for ESZ-3.

Two concerns:
  1. Joseph-form Kalman covariance update preserves symmetry and PSD-ness
     under many iterations, where the short form (I - K H) P does not.
  2. Tikhonov regularization is a no-op for well-conditioned matrices and
     a graceful degradation for rank-deficient ones — replacing the prior
     |det| < 1e-12 hard gate.
"""
from __future__ import annotations

import math

import pytest

from mockingbird_calibration import (
    _invert_3x3,
    _solve_3x3,
    _tikhonov_regularize_3x3,
    _tikhonov_solve_3x3,
)
from mockingbird_tracks import (
    _invert_3x3_local,
    _joseph_update,
    _tikhonov_regularize_3x3_local,
)


# ----------------------------------------------------------------------
# Tikhonov regularization
# ----------------------------------------------------------------------


def _max_abs(M):
    return max(abs(v) for row in M for v in row)


def test_tikhonov_noop_on_well_conditioned():
    """For a well-conditioned PSD matrix, the ridge perturbation is sub-ppm."""
    M = [[4.0, 0.5, 0.1], [0.5, 3.0, -0.2], [0.1, -0.2, 2.0]]
    M_reg = _tikhonov_regularize_3x3(M)
    # The added ridge is eps * tr = 1e-10 * 9 ≈ 9e-10
    for i in range(3):
        for j in range(3):
            diff = abs(M_reg[i][j] - M[i][j])
            # Off-diagonals are unchanged; diagonals shift by ≤ 1e-9.
            assert diff <= 1e-8


def test_tikhonov_solve_recovers_exact_for_well_conditioned():
    """Solve M·x = b should match _solve_3x3 to within ridge-scale accuracy."""
    M = [[4.0, 0.5, 0.1], [0.5, 3.0, -0.2], [0.1, -0.2, 2.0]]
    b = [1.0, 2.0, 3.0]
    x_ref = _solve_3x3(M, b)
    x_tik = _tikhonov_solve_3x3(M, b)
    assert x_ref is not None and x_tik is not None
    for a, c in zip(x_ref, x_tik):
        assert abs(a - c) < 1e-8


def test_tikhonov_handles_singular_matrix():
    """A truly singular matrix returns *something* finite from the Tikhonov
    solve, rather than the None that the bare _solve_3x3 would produce."""
    # Rank-1: outer product of [1,1,1] with itself, scaled.
    M = [[1.0, 1.0, 1.0], [1.0, 1.0, 1.0], [1.0, 1.0, 1.0]]
    b = [3.0, 3.0, 3.0]
    assert _solve_3x3(M, b) is None
    x = _tikhonov_solve_3x3(M, b)
    assert x is not None
    for c in x:
        assert math.isfinite(c)
    # And the residual should be tiny (b lies in range of M).
    r0 = sum(M[0][j] * x[j] for j in range(3)) - b[0]
    assert abs(r0) < 1e-6


def test_tikhonov_local_matches_calibration():
    """The two _tikhonov_regularize_3x3 helpers must agree (they are
    defined twice to avoid the cross-module import in the hot Kalman path)."""
    M = [[4.0, 0.5, 0.1], [0.5, 3.0, -0.2], [0.1, -0.2, 2.0]]
    A = _tikhonov_regularize_3x3(M)
    B = _tikhonov_regularize_3x3_local(M)
    for i in range(3):
        for j in range(3):
            assert abs(A[i][j] - B[i][j]) < 1e-15


def test_tikhonov_scale_invariance():
    """Ridge scales with M, so the *relative* perturbation is constant."""
    M = [[4.0, 0.5, 0.1], [0.5, 3.0, -0.2], [0.1, -0.2, 2.0]]
    Mr = _tikhonov_regularize_3x3(M)
    M10 = [[10 * v for v in row] for row in M]
    Mr10 = _tikhonov_regularize_3x3(M10)
    # Ridge in M10 should be 10× ridge in M, matching its scale.
    ridge_M = Mr[0][0] - M[0][0]
    ridge_M10 = Mr10[0][0] - M10[0][0]
    assert abs(ridge_M10 - 10 * ridge_M) < 1e-12


# ----------------------------------------------------------------------
# Joseph form
# ----------------------------------------------------------------------


def _eye6():
    return [[1.0 if i == j else 0.0 for j in range(6)] for i in range(6)]


def _zero_K():
    return [[0.0] * 3 for _ in range(6)]


def test_joseph_zero_gain_is_identity_on_P():
    """K = 0 ⇒ P stays exactly P. Easy sanity check on the matrix algebra."""
    P = [[2.0 if i == j else 0.1 for j in range(6)] for i in range(6)]
    # Symmetrize
    for i in range(6):
        for j in range(i + 1, 6):
            P[j][i] = P[i][j]
    K = _zero_K()
    R = [[1.0 if i == j else 0.0 for j in range(3)] for i in range(3)]
    P_new = _joseph_update(P, K, R)
    for i in range(6):
        for j in range(6):
            assert abs(P_new[i][j] - P[i][j]) < 1e-12


def test_joseph_output_is_symmetric():
    """Joseph form must produce an exactly-symmetric covariance."""
    # Pick a non-trivial K and P.
    P = [[1.5 if i == j else 0.05 * (i + j) for j in range(6)] for i in range(6)]
    for i in range(6):
        for j in range(i + 1, 6):
            P[j][i] = P[i][j]
    K = [[0.3, 0.05, 0.0], [0.05, 0.3, 0.05], [0.0, 0.05, 0.3],
         [0.1, 0.0, 0.0], [0.0, 0.1, 0.0], [0.0, 0.0, 0.1]]
    R = [[0.04, 0.0, 0.0], [0.0, 0.04, 0.0], [0.0, 0.0, 0.04]]
    P_new = _joseph_update(P, K, R)
    for i in range(6):
        for j in range(6):
            assert P_new[i][j] == P_new[j][i]


def test_joseph_output_psd_under_many_iterations():
    """Repeated updates with a sub-optimal K must keep P positive
    semi-definite. The short form (I - K H) P fails this test; Joseph
    passes. We check PSD via all-positive diagonals and the 1×1, 2×2,
    3×3 leading-principal-minor determinants."""
    P = [[1.0 if i == j else 0.0 for j in range(6)] for i in range(6)]
    R = [[0.25, 0.0, 0.0], [0.0, 0.25, 0.0], [0.0, 0.0, 0.25]]
    # Deliberately *sub-optimal* gain — the short-form (I-KH)P breaks here.
    K = [[0.4, 0.0, 0.0], [0.0, 0.4, 0.0], [0.0, 0.0, 0.4],
         [0.05, 0.0, 0.0], [0.0, 0.05, 0.0], [0.0, 0.0, 0.05]]
    for _ in range(500):
        P = _joseph_update(P, K, R)
    # All diagonals strictly positive.
    for i in range(6):
        assert P[i][i] > 0.0
    # 2×2 and 3×3 leading principal minors of the position block.
    det2 = P[0][0] * P[1][1] - P[0][1] * P[1][0]
    assert det2 > -1e-12
    a, b, c = P[0][0], P[0][1], P[0][2]
    d, e, f = P[1][0], P[1][1], P[1][2]
    g, h, ii = P[2][0], P[2][1], P[2][2]
    det3 = a * (e * ii - f * h) - b * (d * ii - f * g) + c * (d * h - e * g)
    assert det3 > -1e-12


def test_joseph_matches_short_form_at_optimal_gain():
    """At K = P Hᵀ S⁻¹, Joseph form and (I - K H) P are algebraically
    equal. Verifying this nails down that the Joseph implementation is
    correct, not just symmetric."""
    # H = [I_3 | 0]; pick a small P and R, compute K_opt, compare.
    P = [[1.0 if i == j else 0.2 for j in range(6)] for i in range(6)]
    for i in range(6):
        for j in range(i + 1, 6):
            P[j][i] = P[i][j]
    R = [[0.1, 0.0, 0.0], [0.0, 0.1, 0.0], [0.0, 0.0, 0.1]]
    # S = H P Hᵀ + R = P[0:3,0:3] + R
    S = [[P[i][j] + R[i][j] for j in range(3)] for i in range(3)]
    S_inv = _invert_3x3_local(S)
    assert S_inv is not None
    K = [[0.0] * 3 for _ in range(6)]
    for i in range(6):
        for j in range(3):
            for k in range(3):
                K[i][j] += P[i][k] * S_inv[k][j]
    # Short form: P_short = (I - K H) P
    P_short = [row[:] for row in P]
    for i in range(6):
        for j in range(6):
            term = 0.0
            for k in range(3):
                term += K[i][k] * P[k][j]
            P_short[i][j] -= term
    # Joseph form
    P_joseph = _joseph_update(P, K, R)
    # Symmetrize the short form for fair comparison (in finite precision
    # the short form is ~symmetric at optimal K).
    for i in range(6):
        for j in range(i + 1, 6):
            avg = 0.5 * (P_short[i][j] + P_short[j][i])
            P_short[i][j] = avg
            P_short[j][i] = avg
    for i in range(6):
        for j in range(6):
            assert abs(P_short[i][j] - P_joseph[i][j]) < 1e-9


def test_joseph_psd_under_asymmetric_random_gain():
    """Joseph form must remain symmetric AND PSD even with a Kalman gain
    that has no special structure (no symmetry, no relation to any
    "optimal K"). This locks in the unconditional-PSD guarantee — if a
    future change ever drops the symmetrization at the end of
    `_joseph_update`, this test will catch it (the matrix multiplications
    accrue O(eps) asymmetry per step, which compounds to visible
    asymmetry over hundreds of iterations)."""
    import random
    rng = random.Random(42)
    P = [[1.0 if i == j else 0.0 for j in range(6)] for i in range(6)]
    R = [[0.25 if i == j else 0.0 for j in range(3)] for i in range(3)]
    K = [[rng.uniform(-0.2, 0.4) for _ in range(3)] for _ in range(6)]
    for _ in range(300):
        P = _joseph_update(P, K, R)
    # Exact symmetry (we explicitly average + assign).
    for i in range(6):
        for j in range(6):
            assert P[i][j] == P[j][i]
    # All diagonals strictly positive.
    for i in range(6):
        assert P[i][i] > 0.0
    # 3×3 position-block leading principal minor non-negative.
    a, b, c = P[0][0], P[0][1], P[0][2]
    d, e, f = P[1][0], P[1][1], P[1][2]
    g, h, ii = P[2][0], P[2][1], P[2][2]
    det3 = a * (e * ii - f * h) - b * (d * ii - f * g) + c * (d * h - e * g)
    assert det3 > -1e-9


def test_leaf_bias_decomposition_is_identifiable_up_to_gauge():
    """The additive model r_{AB} = TX_A + RX_B has a one-parameter gauge:
    adding c to every TX and subtracting c from every RX leaves the
    residuals invariant. `_fit_leaf_biases` anchors the gauge by enforcing
    mean(TX) = 0 per ALS iteration. With that anchor the fixed point is
    unique. This test reproduces the ALS loop on synthetic ground truth
    and checks that:
      (a) recovered TX matches true TX (which was already mean-zero) within
          noise,
      (b) recovered RX matches true RX up to a global constant (gauge),
          i.e. (recovered_RX - true_RX) is the SAME for every leaf.
    Without the mean(TX) = 0 anchor the solution drifts arbitrarily."""
    import random
    rng = random.Random(0)
    leaves = ["A", "B", "C", "D"]
    true_tx = {"A": 2.0, "B": -1.0, "C": 0.5, "D": -1.5}     # already mean-zero
    true_rx = {"A": 1.0, "B": 3.0, "C": -0.5, "D": -0.5}     # mean = 0.75
    residuals = []
    for tx in leaves:
        for rx in leaves:
            if tx == rx:
                continue
            residuals.append((tx, rx, true_tx[tx] + true_rx[rx] + rng.gauss(0, 0.05)))
    # Replicate the ALS loop from _fit_leaf_biases exactly.
    tx_bias = {l: 0.0 for l in leaves}
    rx_bias = {l: 0.0 for l in leaves}
    for _ in range(20):
        new_tx = {l: [] for l in leaves}
        for tx, rx, r in residuals:
            new_tx[tx].append(r - rx_bias.get(rx, 0.0))
        for l in tx_bias:
            if new_tx[l]:
                tx_bias[l] = sum(new_tx[l]) / len(new_tx[l])
        m = sum(tx_bias.values()) / len(tx_bias)
        for l in tx_bias:
            tx_bias[l] -= m
        new_rx = {l: [] for l in leaves}
        for tx, rx, r in residuals:
            new_rx[rx].append(r - tx_bias.get(tx, 0.0))
        for l in rx_bias:
            if new_rx[l]:
                rx_bias[l] = sum(new_rx[l]) / len(new_rx[l])
    # (a) Recovered TX is mean-zero AND close to true TX.
    assert abs(sum(tx_bias.values())) < 1e-9
    for l in leaves:
        assert abs(tx_bias[l] - true_tx[l]) < 0.1
    # (b) Recovered RX matches true RX up to a single global constant
    #     across all leaves. Compute (recovered - true) per leaf and
    #     check the spread is tight (the constant offset itself can be
    #     anything; we care that it's the SAME for every leaf).
    diffs = [rx_bias[l] - true_rx[l] for l in leaves]
    spread = max(diffs) - min(diffs)
    assert spread < 0.1, f"rx diffs not constant across leaves: {diffs}"
