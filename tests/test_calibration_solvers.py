"""Tests for the pure linear-algebra primitives in mockingbird_calibration.

Behaviors covered:
  - test_solve_3x3_identity_recovers_b
        _solve_3x3 with I·x = b returns b
  - test_solve_3x3_known_dense_system
        _solve_3x3 with a hand-picked invertible A and b returns the
        known solution within 1e-9
  - test_solve_3x3_singular_returns_none
        A row of zeros (singular A) returns None
  - test_invert_3x3_identity_is_identity
        Inverting I returns I
  - test_invert_3x3_times_original_is_identity
        A · A^-1 = I within 1e-9 for a well-conditioned A
  - test_invert_3x3_singular_returns_none
        Singular A returns None

These five functions are the foundation of multilateration. A sign typo
in the cofactor expansion produces a matrix that still looks 3×3 and
still "multilaterates" — to the wrong point. Pin them.
"""
from __future__ import annotations

import pytest

from mockingbird_calibration import _invert_3x3, _solve_3x3


# ---------- _solve_3x3 ----------

def test_solve_3x3_identity_recovers_b():
    # Arrange
    A = [[1.0, 0.0, 0.0],
         [0.0, 1.0, 0.0],
         [0.0, 0.0, 1.0]]
    b = [3.0, -2.0, 7.5]

    # Act
    result = _solve_3x3(A, b)

    # Assert
    assert result is not None
    assert result == pytest.approx((3.0, -2.0, 7.5), abs=1e-12)


def test_solve_3x3_known_dense_system():
    # Arrange — chosen so the exact solution is (1, 2, 3).
    # Verify by hand:
    #   2*1 + 1*2 + (-1)*3 =  1
    #  -3*1 - 1*2 +  2*3   =  1
    #  -2*1 +  1*2 +  2*3  =  6
    A = [[ 2.0,  1.0, -1.0],
         [-3.0, -1.0,  2.0],
         [-2.0,  1.0,  2.0]]
    b = [1.0, 1.0, 6.0]

    # Act
    result = _solve_3x3(A, b)

    # Assert
    assert result is not None
    assert result == pytest.approx((1.0, 2.0, 3.0), abs=1e-9)


def test_solve_3x3_singular_returns_none():
    # Arrange — row 2 is 2× row 0, so rank < 3.
    A = [[1.0, 2.0, 3.0],
         [4.0, 5.0, 6.0],
         [2.0, 4.0, 6.0]]
    b = [1.0, 1.0, 1.0]

    # Act
    result = _solve_3x3(A, b)

    # Assert
    assert result is None


# ---------- _invert_3x3 ----------

def test_invert_3x3_identity_is_identity():
    # Arrange
    I = [[1.0, 0.0, 0.0],
         [0.0, 1.0, 0.0],
         [0.0, 0.0, 1.0]]

    # Act
    result = _invert_3x3(I)

    # Assert
    assert result is not None
    for i in range(3):
        for j in range(3):
            assert result[i][j] == pytest.approx(I[i][j], abs=1e-12)


def test_invert_3x3_times_original_is_identity():
    # Arrange — a well-conditioned non-symmetric matrix.
    A = [[ 4.0,  3.0,  2.0],
         [ 1.0,  2.0,  3.0],
         [ 2.0,  1.0,  4.0]]

    # Act
    A_inv = _invert_3x3(A)

    # Assert
    assert A_inv is not None
    # Compute A @ A_inv element-wise and confirm it equals I.
    for i in range(3):
        for j in range(3):
            val = sum(A[i][k] * A_inv[k][j] for k in range(3))
            expected = 1.0 if i == j else 0.0
            assert val == pytest.approx(expected, abs=1e-9), \
                f"(A·A^-1)[{i}][{j}] = {val}, expected {expected}"


def test_invert_3x3_singular_returns_none():
    # Arrange — column 2 = column 0 + column 1, so det = 0.
    A = [[1.0, 2.0, 3.0],
         [4.0, 5.0, 9.0],
         [7.0, 8.0, 15.0]]

    # Act
    result = _invert_3x3(A)

    # Assert
    assert result is None
