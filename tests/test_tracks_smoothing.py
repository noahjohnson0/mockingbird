"""Tests for the smoothing / sanity-check primitives in mockingbird_tracks.

Behaviors covered:
  - test_smooth_alpha_returns_max_for_tiny_jump
  - test_smooth_alpha_returns_min_for_huge_jump
  - test_smooth_alpha_is_monotonically_decreasing_between_soft_and_hard
  - test_kf_state_corrupt_flags_nonfinite_value
  - test_kf_state_corrupt_flags_huge_position
  - test_kf_state_corrupt_flags_huge_velocity
  - test_kf_state_corrupt_passes_plausible_state
  - test_clamp_pos_cov_diagonal_clamped_to_min_and_max
  - test_clamp_pos_cov_symmetrizes_offdiagonal
  - test_clamp_pos_cov_limits_correlation_magnitude

ANDY-3 additions (2026-05-14): closing the highest-risk uncovered branches
flagged in docs/test/coverage-audit.md after the ESZ-3/ESZ-4 landings.

  - test_kalman_step_first_call_initializes_state_to_measurement_with_zero_velocity
        Pins the first-call init path — measurement returned as-is, state
        seeded to [x,y,z,0,0,0]. A bug here means every track starts wrong.
  - test_kalman_step_large_jump_triggers_reinit_not_smoothing
        A >5m jump must re-seed state to the new measurement instead of
        averaging with the prior estimate (which would lock the track on
        a wrong point for ~10 ticks).
  - test_is_stationary_returns_false_with_fewer_than_history_n_points
  - test_is_stationary_returns_true_for_tight_cluster_inside_radius
  - test_is_stationary_returns_false_when_any_point_exceeds_radius
        Pins the ZUPT gate. An off-by-one between radius_m and radius_m²
        (the production code stores radius_m and squares it for comparison
        against squared distances) silently inverts the threshold.
"""
from __future__ import annotations

import math

import pytest

from mockingbird_tracks import (
    SMOOTH_ALPHA_MAX,
    SMOOTH_ALPHA_MIN,
    SMOOTH_JUMP_HARD,
    SMOOTH_JUMP_SOFT,
    ZUPT_HISTORY_N,
    _clamp_pos_cov,
    _is_stationary,
    _kf_state_looks_corrupt,
    _smooth_alpha,
    kalman_step,
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


# ---------- kalman_step (ANDY-3) ----------
#
# kalman_step is the function every position estimate flows through after
# multilateration. The audit flagged the first-call init and the >5m
# teleport guard as P0 paths that had no direct test. These two pin the
# behaviors a refactor is most likely to break.

def test_kalman_step_first_call_initializes_state_to_measurement_with_zero_velocity(fresh_track):
    # Arrange — brand-new Track with no kf_state yet.
    track = fresh_track(now=1_000_000.0)
    measurement = (2.5, 3.0, 1.5)
    now = 1_000_000.0

    # Act — first call must seed state from the measurement.
    returned = kalman_step(track, measurement, meas_cov_3x3=None, now=now)

    # Assert — returned position equals measurement exactly (no smoothing
    # against a non-existent prior), state is [x,y,z,0,0,0], and kf_last_t
    # is stamped so the next call's dt is computable. This is the
    # invariant the audit calls out: any "clever" change that averages
    # against a prior on the first call would silently bias every track's
    # opening position toward (0,0,0).
    assert returned == measurement
    assert track.kf_state is not None, "first call must initialize kf_state"
    assert track.kf_state[0:3] == [measurement[0], measurement[1], measurement[2]]
    assert track.kf_state[3:6] == [0.0, 0.0, 0.0], (
        f"first-call velocity must be exactly zero, got {track.kf_state[3:6]}"
    )
    assert track.kf_last_t == now


def test_kalman_step_large_jump_triggers_reinit_not_smoothing(fresh_track):
    # Arrange — a track that has been sitting at (0,0,1) for one step,
    # then receives a measurement 10m away. The >5m guard must re-init
    # the filter at the new measurement; otherwise the Kalman update
    # averages prior and new and the track gets stuck halfway between
    # them for ~10 ticks (visible to the user as a track teleporting
    # through walls and then crawling toward its real location).
    track = fresh_track(now=1_000_000.0)
    near = (0.0, 0.0, 1.0)
    kalman_step(track, near, meas_cov_3x3=None, now=1_000_000.0)  # seed
    # Sanity: state seeded near the origin.
    assert track.kf_state[0:3] == [0.0, 0.0, 1.0]
    far = (10.0, 10.0, 1.0)  # ~14.1 m away — well past the 5 m threshold

    # Act
    returned = kalman_step(track, far, meas_cov_3x3=None, now=1_000_000.5)

    # Assert — on re-init the function returns the new measurement
    # verbatim (same contract as a brand-new track), and state is reseeded
    # there with zero velocity. A bug that DROPPED the >5m guard would
    # instead return a point between `near` and `far` and leave a
    # non-zero velocity in the state.
    assert returned == far, (
        f"large jump must re-init at the new measurement, got {returned} "
        f"(expected {far}); a non-far return means the >5m guard regressed"
    )
    assert track.kf_state[0:3] == [far[0], far[1], far[2]]
    assert track.kf_state[3:6] == [0.0, 0.0, 0.0], (
        f"re-init must zero velocity, got {track.kf_state[3:6]}"
    )


# ---------- _is_stationary (ANDY-3) ----------
#
# ZUPT (zero-velocity update) is what kills phantom motion. The audit
# flags this as P1 because the off-by-one risk between `radius_m` and
# `radius_m²` would silently invert the threshold — tracks would be
# called "stationary" when they're moving, freezing the velocity state
# at zero and locking the dashboard position to a stale anchor.

def test_is_stationary_returns_false_with_fewer_than_history_n_points(trail_at):
    # Arrange — only ZUPT_HISTORY_N - 1 points; not enough history to
    # decide stationarity yet. The function must return False so the
    # Kalman update isn't ZUPT-clamped on insufficient evidence.
    pts = trail_at([(0.0, 0.0, 1.0)] * (ZUPT_HISTORY_N - 1))

    # Act
    result = _is_stationary(pts, radius_m=0.7)

    # Assert
    assert result is False


def test_is_stationary_returns_true_for_tight_cluster_inside_radius(trail_at):
    # Arrange — exactly ZUPT_HISTORY_N points, max deviation from
    # centroid is 0.05 m — well inside the 0.7 m default radius.
    pts = trail_at([
        (1.00, 2.00, 1.00),
        (1.05, 2.00, 1.00),
        (1.00, 2.05, 1.00),
        (1.00, 2.00, 1.05),
    ])
    assert len(pts) == ZUPT_HISTORY_N

    # Act
    result = _is_stationary(pts, radius_m=0.7)

    # Assert
    assert result is True


def test_is_stationary_returns_false_when_any_point_exceeds_radius(trail_at):
    # Arrange — ZUPT_HISTORY_N points, three clustered near (0,0,0) and
    # one moved 2 m away on x. The centroid sits at ~(0.5, 0, 0); the
    # outlier is then ~1.5 m from centroid, well outside the 0.7 m
    # radius. The function must reject the cluster as not stationary.
    pts = trail_at([
        (0.0, 0.0, 0.0),
        (0.0, 0.0, 0.0),
        (0.0, 0.0, 0.0),
        (2.0, 0.0, 0.0),
    ])
    assert len(pts) == ZUPT_HISTORY_N

    # Act
    result = _is_stationary(pts, radius_m=0.7)

    # Assert — if this flips True after a refactor, an off-by-one
    # between `radius_m` and `radius_m²` has snuck in.
    assert result is False
