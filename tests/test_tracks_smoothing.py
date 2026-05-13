"""Tests for the smoothing / sanity-check primitives in mockingbird_tracks.

Behaviors covered:
  - test_smooth_alpha_returns_max_for_tiny_jump
        Below SMOOTH_JUMP_SOFT, the smoothing weight = MAX (trust the
        measurement fully — barely smooth)
  - test_smooth_alpha_returns_min_for_huge_jump
        Above SMOOTH_JUMP_HARD, the weight = MIN (suspect the measurement
        — smooth heavily)
  - test_smooth_alpha_is_monotonically_decreasing_between_soft_and_hard
        Strictly non-increasing as jump magnitude grows from SOFT to HARD
  - test_kf_state_corrupt_flags_nonfinite_value
  - test_kf_state_corrupt_flags_huge_position
  - test_kf_state_corrupt_flags_huge_velocity
  - test_kf_state_corrupt_passes_plausible_state
        Normal values pass the guard.
  - test_clamp_pos_cov_diagonal_clamped_to_min_and_max
  - test_clamp_pos_cov_symmetrizes_offdiagonal
  - test_clamp_pos_cov_limits_correlation_magnitude

These five primitives are the "stationary phones don't fly through walls"
safety net. Each is a pure function — no fixtures needed.
"""
from __future__ import annotations

import math

import pytest

from mockingbird_tracks import (
    SMOOTH_ALPHA_MAX,
    SMOOTH_ALPHA_MIN,
    SMOOTH_JUMP_HARD,
    SMOOTH_JUMP_SOFT,
    _clamp_pos_cov,
    _kf_state_looks_corrupt,
    _smooth_alpha,
)


# ---------- _smooth_alpha ----------

def test_smooth_alpha_returns_max_for_tiny_jump():
    # Arrange
    tiny_jump_m = SMOOTH_JUMP_SOFT / 2.0  # well below the soft threshold

    # Act
    alpha = _smooth_alpha(tiny_jump_m)

    # Assert
    assert alpha == pytest.approx(SMOOTH_ALPHA_MAX, abs=1e-12)


def test_smooth_alpha_returns_min_for_huge_jump():
    # Arrange
    huge_jump_m = SMOOTH_JUMP_HARD * 5.0  # well above the hard threshold

    # Act
    alpha = _smooth_alpha(huge_jump_m)

    # Assert
    assert alpha == pytest.approx(SMOOTH_ALPHA_MIN, abs=1e-12)


def test_smooth_alpha_is_monotonically_decreasing_between_soft_and_hard():
    # Arrange — 20 samples spanning [SOFT, HARD]
    n = 20
    jumps = [SMOOTH_JUMP_SOFT + i * (SMOOTH_JUMP_HARD - SMOOTH_JUMP_SOFT) / (n - 1)
             for i in range(n)]

    # Act
    alphas = [_smooth_alpha(j) for j in jumps]

    # Assert — each successive alpha must be ≤ the previous one.
    for i in range(1, n):
        assert alphas[i] <= alphas[i - 1] + 1e-12, (
            f"smooth_alpha is not monotonically non-increasing: "
            f"alpha({jumps[i-1]:.3f})={alphas[i-1]:.4f} < "
            f"alpha({jumps[i]:.3f})={alphas[i]:.4f}"
        )


# ---------- _kf_state_looks_corrupt ----------

# State layout: [x, y, z, vx, vy, vz]

def test_kf_state_corrupt_flags_nonfinite_value():
    # Arrange
    state = [0.0, 0.0, 0.0, float("nan"), 0.0, 0.0]

    # Act
    corrupt = _kf_state_looks_corrupt(state)

    # Assert
    assert corrupt is True


def test_kf_state_corrupt_flags_huge_position():
    # Arrange — position component above 50m guard
    state = [9999.0, 0.0, 0.0, 0.0, 0.0, 0.0]

    # Act
    corrupt = _kf_state_looks_corrupt(state)

    # Assert
    assert corrupt is True


def test_kf_state_corrupt_flags_huge_velocity():
    # Arrange — velocity component above 5 m/s guard (faster than walking)
    state = [0.0, 0.0, 0.0, 50.0, 0.0, 0.0]

    # Act
    corrupt = _kf_state_looks_corrupt(state)

    # Assert
    assert corrupt is True


def test_kf_state_corrupt_passes_plausible_state():
    # Arrange — a plausible state: device at (2,3,1), walking ~1 m/s in +x
    state = [2.0, 3.0, 1.0, 1.0, 0.0, 0.0]

    # Act
    corrupt = _kf_state_looks_corrupt(state)

    # Assert
    assert corrupt is False


# ---------- _clamp_pos_cov ----------

def test_clamp_pos_cov_diagonal_clamped_to_min_and_max():
    # Arrange — diagonal entries below min_std² and above max_std²
    min_std, max_std = 0.1, 5.0
    C = [
        [0.0001, 0.0, 0.0],   # below min_std² = 0.01
        [0.0, 1.0,    0.0],   # in-range
        [0.0, 0.0,    100.0], # above max_std² = 25
    ]

    # Act
    clamped = _clamp_pos_cov(C, min_std=min_std, max_std=max_std)

    # Assert
    assert clamped[0][0] == pytest.approx(min_std * min_std, abs=1e-12)
    assert clamped[1][1] == pytest.approx(1.0, abs=1e-12)
    assert clamped[2][2] == pytest.approx(max_std * max_std, abs=1e-12)


def test_clamp_pos_cov_symmetrizes_offdiagonal():
    # Arrange — asymmetric off-diagonals; well within correlation limit so
    # only the symmetrization (averaging) is exercised here.
    C = [
        [1.0, 0.2, 0.4],
        [0.0, 1.0, 0.1],   # C[1][0] != C[0][1]
        [0.0, 0.3, 1.0],   # C[2][0] != C[0][2] and C[2][1] != C[1][2]
    ]

    # Act
    clamped = _clamp_pos_cov(C, min_std=0.1, max_std=5.0)

    # Assert — each off-diagonal pair must now be equal (= average of the
    # two original values).
    assert clamped[0][1] == pytest.approx(clamped[1][0], abs=1e-12)
    assert clamped[0][2] == pytest.approx(clamped[2][0], abs=1e-12)
    assert clamped[1][2] == pytest.approx(clamped[2][1], abs=1e-12)
    # And the symmetrized value is the mean of the inputs.
    assert clamped[0][1] == pytest.approx(0.5 * (0.2 + 0.0), abs=1e-12)


def test_clamp_pos_cov_limits_correlation_magnitude():
    # Arrange — off-diagonal that would imply correlation 1.0 (degenerate).
    # With var=1 on the diagonal, limit = 0.95 * sqrt(1*1) = 0.95.
    C = [
        [1.0, 1.0, 0.0],
        [1.0, 1.0, 0.0],
        [0.0, 0.0, 1.0],
    ]

    # Act
    clamped = _clamp_pos_cov(C, min_std=0.1, max_std=5.0)

    # Assert — implied correlation must now be ≤ 0.95.
    corr = clamped[0][1] / math.sqrt(clamped[0][0] * clamped[1][1])
    assert abs(corr) <= 0.95 + 1e-9, (
        f"correlation {corr:.4f} exceeds 0.95 cap"
    )
