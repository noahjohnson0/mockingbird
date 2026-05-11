"""mockingbird_calibration — path-loss fit + multilateration.

Two responsibilities:

1. **Fit the log-distance path-loss model** from the current observation
   stream. The model:
        RSSI(d) = P0 - 10 * n * log10(d)
   where P0 is the reference RSSI at d=1m and n is the path-loss exponent
   (~2.0 in free space, 2.5–4.0 indoors). We harvest observations from
   the obs table, compute each device's weighted-centroid position from
   the currently positioned leaves, then fit (P0, n) to minimize the
   sum-squared residual across all (device, leaf) pairs.

   The bootstrap is self-consistent: even with bad initial path-loss
   params, the *relative* RSSI ratios across leaves are what determine
   the centroid, so the centroids land at roughly the right place — and
   then we use them to fit better params. One iteration is usually
   enough; we expose iterate=N for paranoia.

2. **Multilaterate** a single device's position from its per-leaf RSSI
   given a fitted (P0, n). Two-stage:
     - Linearize the system (subtract reference equation), solve least-
       squares for an initial estimate
     - One round of Gauss-Newton on the nonlinear distance equations to
       polish

Pure-Python — no numpy/scipy. Pi Zero W shouldn't pull those in just for
a 3×3 system solver.
"""

from __future__ import annotations

import math
import sqlite3
import time
from dataclasses import dataclass


@dataclass
class CalibrationParams:
    p0: float            # reference RSSI at d=1m, dBm (typically -45 to -65)
    n: float             # path-loss exponent (~2.0 free, ~2.5–3.5 indoor)
    rmse_dbm: float      # how well the model fits, in dB (lower = better)
    n_points: int        # number of (rssi, distance) tuples used
    n_devices: int       # distinct devices contributing data
    fit_ts: float        # when this fit was computed


def _solve_2x2(a, b) -> tuple[float, float]:
    """Solve 2-variable linear system a @ x = b for x using Cramer's rule.
    a is [[a11, a12], [a21, a22]]; b is [b1, b2]."""
    det = a[0][0] * a[1][1] - a[0][1] * a[1][0]
    if abs(det) < 1e-12:
        raise ValueError("singular system")
    x0 = (b[0] * a[1][1] - a[0][1] * b[1]) / det
    x1 = (a[0][0] * b[1] - b[0] * a[1][0]) / det
    return (x0, x1)


def _device_centroid(rssi_per_leaf: dict[str, int],
                     positions: dict[str, tuple[float, float, float]],
                     p0: float, n: float) -> tuple[float, float, float] | None:
    """Per-device weighted-centroid using 10^((p0-rssi)/(10n)) inverse
    distance estimate, or just 10^(rssi/20) when no path-loss given.

    Returns None if fewer than 2 positioned leaves saw this device.
    """
    hits = [(leaf, rssi) for leaf, rssi in rssi_per_leaf.items()
            if leaf in positions]
    if len(hits) < 2:
        return None
    nx = ny = nz = denom = 0.0
    for leaf, rssi in hits:
        # Weight ≈ 1/distance² so closer leaves dominate.  When (p0, n) is
        # known we can directly invert the model; otherwise default to the
        # 10^(rssi/20) heuristic (linear amplitude in air, no model).
        if p0 is not None and n is not None and n > 0:
            d_est = 10.0 ** ((p0 - rssi) / (10.0 * n))
            w = 1.0 / (d_est * d_est + 0.04)  # +0.04 to avoid blow-up near leaf
        else:
            w = 10.0 ** (rssi / 20.0)
        px, py, pz = positions[leaf]
        nx += w * px; ny += w * py; nz += w * pz; denom += w
    return (nx / denom, ny / denom, nz / denom)


def fit_pathloss(db: sqlite3.Connection,
                 positions: dict[str, tuple[float, float, float]],
                 window_s: int = 30,
                 iterate: int = 2,
                 min_rssi: int = -85,
                 min_distance_m: float = 0.5,
                 max_distance_m: float = 12.0) -> CalibrationParams | None:
    """Fit P0, n to recent observations. positions maps leaf_id → (x,y,z).

    Returns CalibrationParams or None if too little data to fit.
    """
    if len(positions) < 3:
        return None
    now = time.time()

    # Pull recent obs, aggregate (device, leaf) → max(rssi) in Python
    leaf_names = list(positions.keys())
    placeholders = ",".join("?" * len(leaf_names))
    rows = db.execute(
        f"SELECT mac, leaf, MAX(rssi) FROM obs INDEXED BY idx_obs_ts "
        f"WHERE ts >= ? AND leaf IN ({placeholders}) GROUP BY mac, leaf",
        (now - window_s, *leaf_names),
    ).fetchall()
    by_mac: dict[str, dict[str, int]] = {}
    for mac, leaf, rssi in rows:
        if rssi < min_rssi:
            continue
        by_mac.setdefault(mac, {})[leaf] = rssi
    # Keep only devices seen by ≥3 positioned leaves (better-constrained)
    devices = {mac: rssis for mac, rssis in by_mac.items() if len(rssis) >= 3}
    if len(devices) < 5:
        return None

    # Iterative refinement with outlier rejection.
    # 1. Start with the amplitude heuristic to compute first centroids
    # 2. Fit (p0, n)
    # 3. Discard the worst ~15% of residuals (multipath, NLOS, reflections)
    # 4. Refit on the kept set. Repeat `iterate` times.
    p0: float | None = None
    n: float | None = None
    rmse = float("nan")
    n_points_final = 0
    n_devs_final = 0
    for _it in range(iterate + 1):
        xs: list[float] = []
        ys: list[float] = []
        device_centroids: dict[str, tuple[float, float, float]] = {}
        for mac, rssis in devices.items():
            c = _device_centroid(rssis, positions, p0, n)
            if c is None:
                continue
            device_centroids[mac] = c
            for leaf, rssi in rssis.items():
                lx, ly, lz = positions[leaf]
                d = math.sqrt((c[0] - lx) ** 2 + (c[1] - ly) ** 2 + (c[2] - lz) ** 2)
                if d < min_distance_m or d > max_distance_m:
                    continue
                xs.append(-10.0 * math.log10(d))
                ys.append(rssi)
        if len(xs) < 10:
            return None
        # ---- Initial fit ----
        p0_new, n_new = _linear_fit(xs, ys)
        if p0_new is None:
            return None
        # ---- Drop top 15% by absolute residual (robust to multipath) ----
        resids = [(abs(y - (p0_new + n_new * x)), x, y) for x, y in zip(xs, ys)]
        resids.sort()
        keep = resids[: int(len(resids) * 0.85)]
        xs_k = [r[1] for r in keep]
        ys_k = [r[2] for r in keep]
        if len(xs_k) >= 10:
            p0_k, n_k = _linear_fit(xs_k, ys_k)
            if p0_k is not None:
                p0_new, n_new = p0_k, n_k
        # ---- Clamp n to physically plausible indoor range ----
        # Centroid bootstrap biases distances toward strong-RSSI leaves,
        # which can produce n outside the physical range [2.0, 4.5].
        # If we landed there, fix n to a typical indoor value and refit P0
        # alone (1-D mean): P0 = mean(rssi + 10n·log10(d_i)) on kept set.
        N_INDOOR_MIN, N_INDOOR_MAX = 2.0, 4.5
        if not (N_INDOOR_MIN <= n_new <= N_INDOOR_MAX):
            n_new = min(N_INDOOR_MAX, max(N_INDOOR_MIN, 2.5))
            # y - n*x = P0 (no slope to fit); mean of (y - n*x) is P0
            p0_new = sum(y - n_new * x for x, y in zip(xs_k, ys_k)) / len(xs_k)
        residuals = [y - (p0_new + n_new * x) for x, y in zip(xs_k, ys_k)]
        rmse = math.sqrt(sum(r * r for r in residuals) / len(residuals)) if residuals else float("inf")
        p0, n = p0_new, n_new
        n_points_final = len(xs_k)
        n_devs_final = len(device_centroids)

    assert p0 is not None and n is not None
    return CalibrationParams(
        p0=round(p0, 2),
        n=round(n, 3),
        rmse_dbm=round(rmse, 2),
        n_points=n_points_final,
        n_devices=n_devs_final,
        fit_ts=now,
    )


def _linear_fit(xs: list[float], ys: list[float]) -> tuple[float | None, float | None]:
    """Plain linear regression y = a + b*x; returns (a, b) or (None, None)."""
    n_pts = len(xs)
    if n_pts < 2:
        return (None, None)
    mx = sum(xs) / n_pts
    my = sum(ys) / n_pts
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx < 1e-9:
        return (None, None)
    b = sxy / sxx
    a = my - b * mx
    return (a, b)


def estimate_distance(rssi: int, params: CalibrationParams) -> float:
    """Invert the log-distance model: d = 10^((P0 - rssi)/(10n))."""
    return 10.0 ** ((params.p0 - rssi) / (10.0 * params.n))


def multilaterate(rssi_per_leaf: dict[str, int],
                  positions: dict[str, tuple[float, float, float]],
                  params: CalibrationParams,
                  bounds: tuple[float, float, float, float, float, float] | None = None,
                  ) -> tuple[float, float, float, float] | None:
    """Estimate (x, y, z, residual_rms) from per-leaf RSSI.

    Returns None if fewer than 4 positioned leaves saw the device (need
    ≥4 for a 3D solve — 3 is degenerate ambiguity in z).
    Uses linearize-then-Gauss-Newton.

    bounds: optional (xmin, xmax, ymin, ymax, zmin, zmax). Solutions
    falling outside this box are rejected (returns None). The dashboard
    passes "room + 1m margin" so that catastrophic linearization
    failures fall back to the centroid.
    """
    hits = [(leaf, rssi) for leaf, rssi in rssi_per_leaf.items()
            if leaf in positions]
    if len(hits) < 4:
        return None

    # Compute distance estimates
    dists: list[tuple[tuple[float, float, float], float]] = []
    for leaf, rssi in hits:
        d = estimate_distance(rssi, params)
        if not math.isfinite(d) or d <= 0 or d > 30:
            continue
        dists.append((positions[leaf], d))
    if len(dists) < 4:
        return None

    # ---- Stage 1: linearize against reference (leaf 0) ----
    # ||p - x_i||² = d_i²  →  expand  →  subtract i=0 equation:
    #   2 (x_0 - x_i)·p = (||x_0||² - d_0²) - (||x_i||² - d_i²)
    # Build over-determined linear system A·p = b, solve via normal eqns.
    (x0, y0, z0), d0 = dists[0]
    c0 = x0 * x0 + y0 * y0 + z0 * z0 - d0 * d0
    A: list[list[float]] = []
    b: list[float] = []
    for (xi, yi, zi), di in dists[1:]:
        A.append([2 * (x0 - xi), 2 * (y0 - yi), 2 * (z0 - zi)])
        ci = xi * xi + yi * yi + zi * zi - di * di
        b.append(c0 - ci)
    # Normal equations: AtA·p = Atb
    AtA = [[0.0]*3 for _ in range(3)]
    Atb = [0.0, 0.0, 0.0]
    for r in range(len(A)):
        for i in range(3):
            Atb[i] += A[r][i] * b[r]
            for j in range(3):
                AtA[i][j] += A[r][i] * A[r][j]
    # Solve 3×3 via Gaussian elimination (small enough, no numpy)
    p = _solve_3x3(AtA, Atb)
    if p is None:
        return None

    # ---- Stage 2: 1 Gauss-Newton iteration on actual distances ----
    # Move toward the point that best matches d_i's directly. Skip if
    # already very good.
    for _ in range(2):
        J: list[list[float]] = []
        r: list[float] = []
        for (xi, yi, zi), di in dists:
            dx, dy, dz = p[0] - xi, p[1] - yi, p[2] - zi
            dist = math.sqrt(dx * dx + dy * dy + dz * dz)
            if dist < 0.05:
                continue
            J.append([dx / dist, dy / dist, dz / dist])
            r.append(di - dist)
        if len(J) < 3:
            break
        # Solve J·dp = r in least-squares: dp = (Jt·J)^-1 Jt·r
        JtJ = [[0.0]*3 for _ in range(3)]
        Jtr = [0.0, 0.0, 0.0]
        for rIdx in range(len(J)):
            for i in range(3):
                Jtr[i] += J[rIdx][i] * r[rIdx]
                for j in range(3):
                    JtJ[i][j] += J[rIdx][i] * J[rIdx][j]
        dp = _solve_3x3(JtJ, Jtr)
        if dp is None:
            break
        p = (p[0] + dp[0], p[1] + dp[1], p[2] + dp[2])

    # Bounds check: catastrophic least-squares blow-ups (ill-conditioned
    # geometry, severe multipath, etc.) produce coords thousands of
    # meters away. Reject and let the caller fall back to centroid.
    if bounds is not None:
        if not (bounds[0] <= p[0] <= bounds[1]
                and bounds[2] <= p[1] <= bounds[3]
                and bounds[4] <= p[2] <= bounds[5]):
            return None

    # Final residual RMS
    sq = 0.0
    for (xi, yi, zi), di in dists:
        dx, dy, dz = p[0] - xi, p[1] - yi, p[2] - zi
        sq += (math.sqrt(dx * dx + dy * dy + dz * dz) - di) ** 2
    rmse = math.sqrt(sq / len(dists))
    return (p[0], p[1], p[2], rmse)


def _solve_3x3(A: list[list[float]], b: list[float]) -> tuple[float, float, float] | None:
    """Gauss-elimination 3x3 linear solver. Returns None if singular."""
    # Copy + augment
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for i in range(3):
        # Pivot: find row with max absolute value in column i below row i
        piv = i
        for r in range(i + 1, 3):
            if abs(M[r][i]) > abs(M[piv][i]):
                piv = r
        if abs(M[piv][i]) < 1e-12:
            return None
        if piv != i:
            M[i], M[piv] = M[piv], M[i]
        # Eliminate
        for r in range(i + 1, 3):
            f = M[r][i] / M[i][i]
            for c in range(i, 4):
                M[r][c] -= f * M[i][c]
    # Back-substitute
    x = [0.0, 0.0, 0.0]
    for i in range(2, -1, -1):
        s = M[i][3]
        for j in range(i + 1, 3):
            s -= M[i][j] * x[j]
        x[i] = s / M[i][i]
    return (x[0], x[1], x[2])
