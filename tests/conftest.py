"""Test bootstrap: make `services/` importable so tests can do
`from mockingbird_calibration import ...` regardless of cwd.

The collector module is currently named `mockingbird-collector.py` (with
a hyphen) and is therefore NOT importable as-is — tests touching the
collector must wait for that file to be renamed. See coverage-audit.md.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SERVICES = _REPO_ROOT / "services"
if str(_SERVICES) not in sys.path:
    sys.path.insert(0, str(_SERVICES))


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------
import pytest  # noqa: E402  (sys.path mutation must come first)


@pytest.fixture
def fresh_track():
    """Build a brand-new Track with no Kalman state yet.

    The Kalman tests need a Track instance to mutate but don't care about
    track_id semantics; this keeps the AAA setup short while still letting
    each test set its own `last_cov_3x3` / trail if it needs to.
    """
    from mockingbird_tracks import Track

    def _make(track_id: str = "test-track-0001",
              now: float = 1_000_000.0) -> "Track":
        return Track(
            track_id=track_id,
            first_seen=now,
            last_seen=now,
            current_mac="aa:bb:cc:dd:ee:ff",
        )

    return _make


@pytest.fixture
def trail_at():
    """Build a list[TrailPoint] from a list of (x, y, z) tuples.

    Used by ZUPT / _is_stationary tests so each test can express its
    geometry as plain tuples in the Arrange block instead of repeating
    TrailPoint(...) noise.
    """
    from mockingbird_tracks import TrailPoint

    def _make(points: list[tuple[float, float, float]],
              t0: float = 0.0, dt: float = 0.5) -> list:
        return [TrailPoint(t0 + i * dt, x, y, z)
                for i, (x, y, z) in enumerate(points)]

    return _make
