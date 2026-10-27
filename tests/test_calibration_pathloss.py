"""Tests for the path-loss model inversion and multilateration in
mockingbird_calibration.

Behaviors covered:
  - test_estimate_distance_at_reference_rssi_returns_one_meter
        rssi == P0  =>  d == 1.0 m  (inverse of P0 - 10n·log10(1) = P0)
  - test_estimate_distance_at_p0_minus_10n_returns_ten_meters
        rssi == P0 - 10n  =>  d == 10.0 m
  - test_estimate_distance_with_rx_bias_subtracts_before_inverting
        A leaf with rx_bias = -6 dB on a -71 dBm reading produces the
        same distance as the same leaf with rx_bias = 0 on a -65 dBm
        reading. Pins the sign convention.
  - test_multilaterate_returns_none_when_fewer_than_four_positioned_leaves
        ≥4 hits is mandatory for 3D solve.
  - test_multilaterate_recovers_known_position_in_clean_geometry
        Synthetic clean data (RSSI computed from the model with no noise)
        is inverted back to the source position within 30 cm.
  - test_multilaterate_rejects_solutions_outside_bounds
        A bounds tuple narrower than the true position rejects.

The pathloss model is RSSI(d) = P0 - 10·n·log10(d). Tests synthesize
clean RSSI from a known device position, then assert that multilateration
recovers the position. No physics assumptions beyond the model itself
(in particular, NO assumption that RSSI is monotonic-with-distance in
the field — that's only true here because we're inverting a closed-form
model on synthetic data).
"""
from __future__ import annotations

import math

import pytest

from mockingbird_calibration import (
    CalibrationParams,
    estimate_distance,
    multilaterate,
)


def _make_params(p0: float = -45.0, n: float = 2.5) -> CalibrationParams:
    """Minimal CalibrationParams with just the global model populated."""
    return CalibrationParams(
        p0=p0,
        n=n,
        rmse_dbm=0.0,
        n_points=0,
        n_devices=0,
        fit_ts=0.0,
    )


def _synth_rssi(device_xyz, leaf_xyz, p0, n) -> float:
    """Compute clean RSSI from the log-distance model for a device-leaf pair.

    Returns a float, NOT an int. The production type hint on multilaterate
    says `dict[str, int]` but the math is float-tolerant, and int-rounding
    1 dB of RSSI is ~10% in linear distance for n=2.5 — enough to push a
    perfectly clean synthetic test off by several meters. For the math-
    inversion test we want to verify the algorithm itself, not the
    quantization noise of the radio. Field-realistic-noise tests are
    separately tracked (see coverage-audit.md backlog #20).
    """
    dx = device_xyz[0] - leaf_xyz[0]
    dy = device_xyz[1] - leaf_xyz[1]
    dz = device_xyz[2] - leaf_xyz[2]
    d = max(1e-3, math.sqrt(dx * dx + dy * dy + dz * dz))
    return p0 - 10.0 * n * math.log10(d)


# ---------- estimate_distance ----------

def test_estimate_distance_at_reference_rssi_returns_one_meter():
    # Arrange
    params = _make_params(p0=-45.0, n=2.5)

    # Act
    d = estimate_distance(rssi=-45, params=params)

    # Assert
    assert d == pytest.approx(1.0, abs=1e-9)


def test_estimate_distance_at_p0_minus_10n_returns_ten_meters():
    # Arrange
    p0, n = -45.0, 2.5
    params = _make_params(p0=p0, n=n)
    rssi_at_10m = int(p0 - 10.0 * n)  # -70

    # Act
    d = estimate_distance(rssi=rssi_at_10m, params=params)

    # Assert
    assert d == pytest.approx(10.0, abs=1e-9)


def test_estimate_distance_with_rx_bias_subtracts_before_inverting():
    # Arrange — leaf-A receives 6 dB COLD (its rx_bias = -6), so a real
    # -71 reading from leaf-A is equivalent to a -65 reading from a
    # zero-bias leaf at the same distance. Both should produce the same
    # distance estimate.
    p0, n = -45.0, 2.5
    params_biased = _make_params(p0=p0, n=n)
    params_biased.rx_bias = {"leaf-A": -6.0}
    params_biased.per_leaf = {"leaf-A": (p0, n, 1.0)}

    params_clean = _make_params(p0=p0, n=n)

    # Act
    d_biased = estimate_distance(rssi=-71, params=params_biased, leaf="leaf-A")
    d_clean  = estimate_distance(rssi=-65, params=params_clean)

    # Assert
    assert d_biased == pytest.approx(d_clean, rel=1e-9)


# ---------- multilaterate ----------

def test_multilaterate_returns_none_when_fewer_than_four_positioned_leaves():
    # Arrange — only 3 leaves; 3D solve is underdetermined (z ambiguous).
    p0, n = -45.0, 2.5
    params = _make_params(p0=p0, n=n)
    positions = {
        "L1": (0.0, 0.0, 1.0),
        "L2": (5.0, 0.0, 1.0),
        "L3": (0.0, 5.0, 1.0),
    }
    rssi = {"L1": -60, "L2": -60, "L3": -60}

    # Act
    result = multilaterate(rssi, positions, params)

    # Assert
    assert result is None


def test_multilaterate_recovers_known_position_in_clean_geometry():
    # Arrange — 4 leaves at non-coplanar positions (z-diverse, so z is
    # observable), device at an asymmetric known point. RSSI synthesized
    # cleanly from the model as floats — see _synth_rssi docstring on why
    # int-quantization would dominate the residual.
    #
    # Z-diversity matters: 4 coplanar leaves leave z degenerate. The
    # production code requires ≥4 hits but doesn't enforce z-diversity;
    # that's the caller's job (the dashboard places leaves at varied
    # heights). This test pins the math path on non-degenerate geometry.
    p0, n = -45.0, 2.5
    params = _make_params(p0=p0, n=n)
    positions = {
        "L1": (0.0, 0.0, 2.5),
        "L2": (3.5, 0.0, 0.4),
        "L3": (0.2, 4.2, 2.3),
        "L4": (3.1, 3.8, 0.6),
    }
    true_pos = (2.1, 1.3, 1.0)
    rssi = {leaf: _synth_rssi(true_pos, pos, p0, n)
            for leaf, pos in positions.items()}

    # Act
    result = multilaterate(rssi, positions, params)

    # Assert
    assert result is not None, "clean 4-leaf geometry must produce a solution"
    x, y, z, rmse = result
    err = math.sqrt((x - true_pos[0])**2 +
                    (y - true_pos[1])**2 +
                    (z - true_pos[2])**2)
    assert err < 0.30, (
        f"recovered position ({x:.2f},{y:.2f},{z:.2f}) is {err*100:.0f}cm "
        f"from truth {true_pos}; expected < 30cm in noise-free synthetic data"
    )


def test_multilaterate_rejects_solutions_outside_bounds():
    # Arrange — same non-degenerate clean geometry, but bounds box
    # explicitly excludes the true y=1.3. The solver finds the right
    # answer but the bounds check should reject it.
    p0, n = -45.0, 2.5
    params = _make_params(p0=p0, n=n)
    positions = {
        "L1": (0.0, 0.0, 2.5),
        "L2": (3.5, 0.0, 0.4),
        "L3": (0.2, 4.2, 2.3),
        "L4": (3.1, 3.8, 0.6),
    }
    true_pos = (2.1, 1.3, 1.0)
    rssi = {leaf: _synth_rssi(true_pos, pos, p0, n)
            for leaf, pos in positions.items()}
    # Bounds that exclude y=1.3
    bounds = (0.0, 4.0, 0.0, 1.0, 0.0, 2.5)

    # Act
    result = multilaterate(rssi, positions, params, bounds=bounds)

    # Assert
    assert result is None, (
        "solution falls outside the supplied bounds box; multilaterate "
        "must return None so callers fall back to centroid"
    )
