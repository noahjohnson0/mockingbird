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
from dataclasses import dataclass, field


@dataclass
class CalibrationParams:
    p0: float            # reference RSSI at d=1m, dBm (typically -45 to -65)
    n: float             # path-loss exponent (~2.0 free, ~2.5–3.5 indoor)
    rmse_dbm: float      # how well the model fits, in dB (lower = better)
    n_points: int        # number of (rssi, distance) tuples used
    n_devices: int       # distinct devices contributing data
    fit_ts: float        # when this fit was computed
    # Per-leaf models (when computed): leaf_id → (P0_i, n_i, sigma_i)
    per_leaf: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    # Per-leaf TX bias: how much this leaf transmits HOT (+) or COLD (-)
    # vs. the model's predicted P0. Derived from leaf-to-leaf advertising
    # data: this leaf's TX bias is the mean (observed - predicted) across
    # ALL receiver leaves observing it.
    tx_bias: dict[str, float] = field(default_factory=dict)
    # Per-leaf RX bias: how much this leaf RECEIVES hot/cold compared to
    # the model. Derived from the dual decomposition: this leaf's RX bias
    # is the mean residual across all sources transmitting to it, minus
    # any TX bias of those sources.
    rx_bias: dict[str, float] = field(default_factory=dict)


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


def _harvest_calibration_points(db: sqlite3.Connection,
                                positions: dict[str, tuple[float, float, float]],
                                min_rssi: int,
                                min_distance_m: float,
                                max_distance_m: float,
                                ) -> tuple[list[float], list[float], int]:
    """Pull (rssi, log10_distance) pairs from completed calibration anchor
    points. These are ground truth — the device's position is *known*,
    not centroid-estimated — so each pair gets the model fit honestly.

    Returns (xs, ys, n_points) for use in linear regression.
    xs is -10*log10(d), ys is rssi.
    """
    now = time.time()
    xs: list[float] = []
    ys: list[float] = []
    n_points = 0
    try:
        rows = db.execute(
            "SELECT id, ts_start, ts_end, mac, x, y, z FROM calibration_points "
            "WHERE ts_end <= ? ORDER BY ts_start DESC LIMIT 100",
            (now,),
        ).fetchall()
    except sqlite3.OperationalError:
        return ([], [], 0)
    for _cid, ts_start, ts_end, mac, px, py, pz in rows:
        if ts_end <= ts_start:
            continue
        leaf_obs = db.execute(
            # single mac is highly selective — idx_obs_mac_ts seeks
            # straight to the mac's range vs scanning the whole window.
            "SELECT leaf, MAX(rssi) FROM obs INDEXED BY idx_obs_mac_ts "
            "WHERE ts >= ? AND ts <= ? AND mac = ? GROUP BY leaf",
            (ts_start, ts_end, mac),
        ).fetchall()
        if len(leaf_obs) < 2:
            continue
        n_points += 1
        for leaf, rssi in leaf_obs:
            if leaf not in positions or rssi < min_rssi:
                continue
            lx, ly, lz = positions[leaf]
            d = math.sqrt((px - lx) ** 2 + (py - ly) ** 2 + (pz - lz) ** 2)
            if d < min_distance_m or d > max_distance_m:
                continue
            xs.append(-10.0 * math.log10(d))
            ys.append(rssi)
    return (xs, ys, n_points)


def fit_pathloss(db: sqlite3.Connection,
                 positions: dict[str, tuple[float, float, float]],
                 window_s: int = 30,
                 iterate: int = 2,
                 min_rssi: int = -85,
                 min_distance_m: float = 0.3,
                 max_distance_m: float = 12.0) -> CalibrationParams | None:
    """Fit P0, n. Prefers ground-truth calibration anchor points when
    available; falls back to bootstrap-from-centroid otherwise.

    positions maps leaf_id → (x,y,z). Returns CalibrationParams or None
    if too little data.
    """
    if len(positions) < 3:
        return None
    now = time.time()

    # ---- Path 1: ground-truth calibration points ----
    # If the user has captured ≥3 calibration anchors, fit DIRECTLY from
    # those (rssi, real-distance) pairs. No bootstrap, no centroid bias,
    # no iterative refinement needed.
    cxs, cys, n_anchors = _harvest_calibration_points(
        db, positions, min_rssi, min_distance_m, max_distance_m,
    )
    if n_anchors >= 3 and len(cxs) >= 8:
        p0, n = _linear_fit(cxs, cys)
        if p0 is None:
            return None
        # Drop top 15% residuals (multipath outliers) and refit.
        resids = [(abs(y - (p0 + n * x)), x, y) for x, y in zip(cxs, cys)]
        resids.sort()
        keep = resids[: int(len(resids) * 0.85)]
        cxs_k = [r[1] for r in keep]
        cys_k = [r[2] for r in keep]
        if len(cxs_k) >= 8:
            p0k, nk = _linear_fit(cxs_k, cys_k)
            if p0k is not None:
                p0, n = p0k, nk
        N_INDOOR_MIN, N_INDOOR_MAX = 1.8, 5.0
        if not (N_INDOOR_MIN <= n <= N_INDOOR_MAX):
            n = min(N_INDOOR_MAX, max(N_INDOOR_MIN, 2.5))
            p0 = sum(y - n * x for x, y in zip(cxs_k, cys_k)) / len(cxs_k)
        residuals = [y - (p0 + n * x) for x, y in zip(cxs_k, cys_k)]
        rmse = math.sqrt(sum(r * r for r in residuals) / len(residuals))
        per_leaf = _fit_per_leaf_models(db, positions, now, p0, n)
        tx_bias, rx_bias = _fit_leaf_biases(db, positions, p0, n, per_leaf, now)
        return CalibrationParams(
            p0=round(p0, 2), n=round(n, 3), rmse_dbm=round(rmse, 2),
            n_points=len(cxs_k), n_devices=n_anchors, fit_ts=now,
            per_leaf=per_leaf, tx_bias=tx_bias, rx_bias=rx_bias,
        )

    # ---- Path 2: bootstrap from current observation centroids ----
    # Pull recent obs, aggregate (device, leaf) → max(rssi) in Python
    leaf_names = list(positions.keys())
    placeholders = ",".join("?" * len(leaf_names))
    rows = db.execute(
        # leaf IN (~8 leaves) — per-leaf range via idx_obs_leaf_ts beats
        # full idx_obs_ts scan + post-filter.
        f"SELECT mac, leaf, MAX(rssi) FROM obs INDEXED BY idx_obs_leaf_ts "
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
    # ---- Per-leaf models ----
    # For each receiver leaf, fit its own (P0_i, n_i, sigma_i) from the
    # subset of inter-leaf + anchor data where that leaf was the RECEIVER.
    # Each leaf has its own antenna pattern, RF chain gain, and noise
    # characteristics — capturing those individually drops RMSE substantially
    # for that leaf's contribution to multilateration.
    per_leaf = _fit_per_leaf_models(db, positions, now, p0, n)
    return CalibrationParams(
        p0=round(p0, 2),
        n=round(n, 3),
        rmse_dbm=round(rmse, 2),
        n_points=n_points_final,
        n_devices=n_devs_final,
        fit_ts=now,
        per_leaf=per_leaf,
    )


def _fit_leaf_biases(db, positions, fallback_p0, fallback_n, per_leaf,
                     now: float, window_s: int = 90,
                     ) -> tuple[dict[str, float], dict[str, float]]:
    """Decompose leaf-pair residuals into per-leaf TX bias + RX bias.

    For each ordered pair (A=transmitter, B=receiver) with known
    distance d_AB and observed mean RSSI rssi_AB:
        residual_AB = rssi_AB - (P0_A - 10·n_A·log10(d_AB))
                    = TX_A + RX_B + noise

    With 56 such equations (8×7 directional pairs) and 16 unknowns
    (TX_i, RX_i per leaf), this is over-determined. We use a simple
    iterative ALS-style decomposition:
      - Initialize TX_i = mean residual when leaf i transmits
      - Initialize RX_i = mean residual when leaf i receives, minus
        the average TX bias of sources
      - Iterate until convergence

    Returns ({leaf_id: tx_bias_dbm}, {leaf_id: rx_bias_dbm}).
    """
    if len(positions) < 3:
        return ({}, {})
    # Build the (tx_leaf, rx_leaf, residual) list from anchored cal points
    # (the leaf-advert anchor points have transmitter = leaf at known position)
    anchor_rows = db.execute(
        "SELECT mac, x, y, z, ts_start, ts_end, label FROM calibration_points "
        "WHERE ts_end <= ? AND label LIKE 'leaf-advert%' ORDER BY ts_start DESC",
        (now,),
    ).fetchall()
    # Build a lookup from "leaf BLE BD_ADDR" → "leaf_id"
    # Each leaf's BD_ADDR comes from the mac column in its leaf-advert anchor.
    mac_to_leaf: dict[str, str] = {}
    for mac, ax, ay, az, _ts_s, _ts_e, label in anchor_rows:
        leaf_id = (label or "").replace("leaf-advert · ", "").strip()
        if leaf_id in positions:
            mac_to_leaf[mac] = leaf_id
    if not mac_to_leaf:
        return ({}, {})
    residuals: list[tuple[str, str, float]] = []   # (tx, rx, residual)
    for mac, ax, ay, az, ts_s, ts_e, _label in anchor_rows:
        tx = mac_to_leaf.get(mac)
        if not tx:
            continue
        # For each receiver leaf, mean RSSI of obs of this mac in window
        rx_rows = db.execute(
            # single mac is far more selective than any leaf set;
            # idx_obs_mac_ts seeks the mac's range directly.
            "SELECT leaf, AVG(rssi), COUNT(*) FROM obs INDEXED BY idx_obs_mac_ts "
            "WHERE mac = ? AND ts >= ? AND ts <= ? GROUP BY leaf",
            (mac, ts_s, ts_e),
        ).fetchall()
        for rx, mean_rssi, cnt in rx_rows:
            if rx == tx or cnt < 3 or rx not in positions:
                continue
            rxp = positions[rx]
            d = math.sqrt((ax - rxp[0])**2 + (ay - rxp[1])**2 + (az - rxp[2])**2)
            if d < 0.3 or d > 12.0:
                continue
            # Predict using per-leaf model of the RECEIVER's path-loss
            if rx in per_leaf:
                p0_i, n_i, _sigma = per_leaf[rx]
            else:
                p0_i, n_i = fallback_p0, fallback_n
            predicted = p0_i - 10.0 * n_i * math.log10(d)
            residuals.append((tx, rx, mean_rssi - predicted))
    if len(residuals) < 4:
        return ({}, {})
    # ALS decomposition: alternate updating tx_bias (holding rx fixed)
    # and rx_bias (holding tx fixed). Each is a simple per-leaf mean of
    # (residual - other_side_bias). Constrain mean(tx) = 0 so the system
    # is identifiable (otherwise tx and rx can absorb arbitrary constants).
    tx_bias = {leaf: 0.0 for leaf in positions}
    rx_bias = {leaf: 0.0 for leaf in positions}
    for _ in range(20):
        # Update tx_bias: for each leaf, mean of (residual - rx_bias[receiver])
        # over all pairs where leaf is the transmitter
        new_tx: dict[str, list[float]] = {leaf: [] for leaf in positions}
        for tx, rx, r in residuals:
            new_tx[tx].append(r - rx_bias.get(rx, 0.0))
        for leaf in tx_bias:
            if new_tx[leaf]:
                tx_bias[leaf] = sum(new_tx[leaf]) / len(new_tx[leaf])
        # Constrain mean(tx) = 0
        m = sum(tx_bias.values()) / len(tx_bias)
        for leaf in tx_bias:
            tx_bias[leaf] -= m
        # Update rx_bias: per-leaf mean of (residual - tx_bias[transmitter])
        new_rx: dict[str, list[float]] = {leaf: [] for leaf in positions}
        for tx, rx, r in residuals:
            new_rx[rx].append(r - tx_bias.get(tx, 0.0))
        for leaf in rx_bias:
            if new_rx[leaf]:
                rx_bias[leaf] = sum(new_rx[leaf]) / len(new_rx[leaf])
    return ({k: round(v, 2) for k, v in tx_bias.items()},
            {k: round(v, 2) for k, v in rx_bias.items()})


def _fit_per_leaf_models(db, positions, now, fallback_p0, fallback_n,
                         window_s: int = 90, min_pts: int = 3,
                         ) -> dict[str, tuple[float, float, float]]:
    """For each leaf, fit its own path-loss model from data where it was
    the RECEIVER. Returns {leaf_id: (P0_i, n_i, sigma_i)}.

    Uses BOTH anchor points (devices at known positions like phone walk
    or other leaves' broadcasts) AND any leaf-advert calibration_points.
    The dataset is "all rssi observations on this receiver leaf from
    sources whose positions we know exactly."
    """
    out: dict[str, tuple[float, float, float]] = {}
    if len(positions) < 2:
        return out

    # Collect anchor points: each cal_point has (mac, x,y,z, ts_window)
    anchor_rows = db.execute(
        "SELECT mac, x, y, z, ts_start, ts_end FROM calibration_points "
        "WHERE ts_end <= ? ORDER BY ts_start DESC LIMIT 200",
        (now,),
    ).fetchall()
    if not anchor_rows:
        return out

    for receiver_leaf, leaf_pos in positions.items():
        xs: list[float] = []   # = -10 * log10(distance)
        ys: list[float] = []   # = rssi
        for mac, ax, ay, az, ts_s, ts_e in anchor_rows:
            # Distance from anchor's known position to THIS receiver leaf
            dx, dy, dz = ax - leaf_pos[0], ay - leaf_pos[1], az - leaf_pos[2]
            d = math.sqrt(dx * dx + dy * dy + dz * dz)
            if d < 0.3 or d > 12.0:
                continue
            # All obs of this MAC at this receiver leaf in the anchor's window
            rssi_row = db.execute(
                # mac is ~1-of-many-thousands; idx_obs_mac_ts is the
                # tightest seek even with the leaf= further filter.
                "SELECT AVG(rssi), COUNT(*) FROM obs INDEXED BY idx_obs_mac_ts "
                "WHERE leaf = ? AND mac = ? AND ts >= ? AND ts <= ?",
                (receiver_leaf, mac, ts_s, ts_e),
            ).fetchone()
            if not rssi_row or not rssi_row[1] or rssi_row[1] < 1:
                continue
            mean_rssi = rssi_row[0]
            if mean_rssi < -90:
                continue
            xs.append(-10.0 * math.log10(d))
            ys.append(mean_rssi)
        if len(xs) < min_pts:
            # Not enough data for this leaf — use global as fallback.
            # Sigma defaults to a reasonable ~5 dB so it isn't trusted too much.
            out[receiver_leaf] = (fallback_p0, fallback_n, 5.0)
            continue
        p0_i, n_i = _linear_fit(xs, ys)
        if p0_i is None:
            out[receiver_leaf] = (fallback_p0, fallback_n, 5.0)
            continue
        # Clamp n_i to indoor plausibility before computing residuals
        if not (1.5 <= n_i <= 5.5):
            n_i = min(5.5, max(1.5, fallback_n))
            p0_i = sum(y - n_i * x for x, y in zip(xs, ys)) / len(xs)
        residuals = [y - (p0_i + n_i * x) for x, y in zip(xs, ys)]
        sigma = math.sqrt(sum(r * r for r in residuals) / len(residuals))
        out[receiver_leaf] = (round(p0_i, 2), round(n_i, 3), round(sigma, 2))
    return out


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


def estimate_distance(rssi: int, params: CalibrationParams, leaf: str | None = None) -> float:
    """Invert the log-distance model: d = 10^((P0 - rssi_unbiased)/(10n)).

    Model: rssi_measured = P0 - 10 n log10(d) + rx_bias_leaf + noise.
    A leaf with rx_bias = -6 reports 6 dB COLDER than a reference leaf at
    the same distance, so to invert we first remove that offset:
        rssi_unbiased = rssi_measured - rx_bias_leaf
    THEN invert the path-loss equation. Subtract-before-invert is required;
    applying the bias inside the exponent (i.e. baking it into P0) is
    algebraically equivalent only when there is no per-leaf P0/n override,
    so we do it explicitly here to keep the semantics correct in all paths.

    Uses per-leaf (P0, n) when available; falls back to global.
    """
    if leaf is not None and leaf in params.per_leaf:
        p0, n, _sigma = params.per_leaf[leaf]
    else:
        p0, n = params.p0, params.n
    rx_b = params.rx_bias.get(leaf, 0.0) if (leaf is not None and params.rx_bias) else 0.0
    rssi_unbiased = rssi - rx_b
    return 10.0 ** ((p0 - rssi_unbiased) / (10.0 * n))


def mle_multilaterate(rssi_per_leaf: dict[str, int],
                      positions: dict[str, tuple[float, float, float]],
                      params: CalibrationParams,
                      bounds: tuple[float, float, float, float, float, float] | None = None,
                      ) -> dict | None:
    """Maximum-likelihood multilateration with per-leaf variance weighting.

    Model:
      rssi_i = P0_i - 10 n_i log10(d_i) + noise_i,  noise_i ~ N(0, sigma_i)
      d_i = ||p - x_i||
    Each leaf's distance estimate has its OWN variance, derived from
    that leaf's residuals in the path-loss fit. Closer-/better-calibrated
    leaves get more weight in the position estimate.

    Algorithm:
      1. Bancroft-style algebraic warm start in the distance domain (one
         shot, no iteration) using d_i = 10^((P0_i - rssi_i)/(10n_i)) as
         the linearization point. This anchors GN in the right basin.
      2. 2-3 Gauss-Newton iterations on the **dB-domain** log-likelihood:
                r_i = rssi_obs_i - rssi_predicted(x; P0_i, n_i, b_rx_i)
                w_i = 1 / sigma_i^2          (constant in dB; correct MLE
                                              under Gaussian-in-dB noise)
         IRLS Huber on dB residuals (k = 3 dB) for outlier rejection.
      3. Position covariance Σ_p = (Jᵀ W J)⁻¹ from the final Jacobian.

    Why dB-domain rather than distance-domain WLS:
      The noise model is Gaussian in dB. The transform d = 10^((P0-rssi)
      /(10n)) is exponential and biased: with σ_rssi = 5 dB and n = 2.5,
      E[d] / d_true ≈ exp(½ (σ ln10 / 10n)²) ≈ 1.11 — i.e. an 11% radial
      bias that pushes every position estimate systematically outward.
      Minimizing residuals in dB removes this bias entirely; the residuals
      are then exactly the noise variables that the model says are
      Gaussian, and OLS on Gaussian residuals is the MLE.

    Returns dict with x/y/z, residual_rms, n_leaves_used, AND a 3×3 cov
    matrix `cov` that the dashboard can render as an uncertainty ellipsoid.
    """
    # Build per-leaf records carrying everything we need for both the
    # algebraic warm start (needs a distance estimate) and the dB-domain
    # GN refinement (needs the corrected observation + per-leaf P0, n, σ).
    # Each record: (pos, d_init, w_db, rssi_corr, p0_i, n_i, sigma_i)
    items: list[tuple[tuple[float, float, float], float, float,
                       float, float, float, float]] = []
    for leaf, rssi in rssi_per_leaf.items():
        if leaf not in positions:
            continue
        if leaf in params.per_leaf:
            p0_i, n_i, sigma_i = params.per_leaf[leaf]
        else:
            p0_i, n_i, sigma_i = params.p0, params.n, params.rmse_dbm or 5.0
        if n_i <= 0:
            continue
        # Bias-correct the observed RSSI using the receiver leaf's RX
        # bias. (TX bias is a property of the TRANSMITTER, which here is
        # the unknown device — we don't know its TX bias, so we don't
        # correct for it. Each device's TX power gets absorbed into the
        # path-loss fit if it differs much from the calibration source.)
        rx_b = params.rx_bias.get(leaf, 0.0) if params.rx_bias else 0.0
        rssi_corr = rssi - rx_b
        d_init = 10.0 ** ((p0_i - rssi_corr) / (10.0 * n_i))
        if not math.isfinite(d_init) or d_init <= 0 or d_init > 30:
            continue
        # dB-domain weight: w = 1/σ². Clamp σ from below to avoid one
        # overconfident leaf dominating.
        sig = max(sigma_i, 0.5)
        w_db = 1.0 / (sig * sig)
        items.append((positions[leaf], d_init, w_db, rssi_corr, p0_i, n_i, sig))
    if len(items) < 4:
        return None

    # ---- Stage 1: weighted algebraic linearization (Bancroft-style) ----
    # Subtract the first leaf's range equation from each of the others to
    # cancel the ||p||² nonlinearity, giving a linear system in (x,y,z).
    # We use distance-domain weights here just for the warm start — this
    # is biased (the whole point of the ticket) but it's fast, algebraic,
    # and lands us in the right basin for the unbiased GN below.
    (x0, y0, z0), d0, _, _, _, _, _ = items[0]
    c0 = x0 * x0 + y0 * y0 + z0 * z0 - d0 * d0
    A: list[list[float]] = []
    b_vec: list[float] = []
    wvec: list[float] = []
    for (xi, yi, zi), di, _w_db, _rs, _p0, _n, _sig in items[1:]:
        A.append([2 * (x0 - xi), 2 * (y0 - yi), 2 * (z0 - zi)])
        b_vec.append(c0 - (xi * xi + yi * yi + zi * zi - di * di))
        # warm-start weight: rough inverse-distance-variance, only used
        # to bias the linear init towards close (high-SNR) leaves.
        wvec.append(1.0 / max(di * di, 0.25))
    AtWA = [[0.0] * 3 for _ in range(3)]
    AtWb = [0.0, 0.0, 0.0]
    for r in range(len(A)):
        w = wvec[r]
        for i in range(3):
            AtWb[i] += w * A[r][i] * b_vec[r]
            for j in range(3):
                AtWA[i][j] += w * A[r][i] * A[r][j]
    p = _solve_3x3(AtWA, AtWb)
    if p is None:
        return None

    # ---- Stage 2: Gauss-Newton on the dB-domain log-likelihood ----
    # Model:  rssi_pred(x) = P0_i - 10 n_i log10(d_i),   d_i = ||x - x_i||
    # Residual (in dB):    r_i = rssi_obs_i - rssi_pred_i(x)
    # Weights:             w_i = 1 / σ_i²        (constant in dB)
    #
    # Jacobian: with u_i = x - x_i and d_i = ||u_i||,
    #   ∂d_i/∂x = u_i / d_i
    #   ∂rssi_pred/∂d_i = -10 n_i / (ln(10) d_i)
    #   ∂rssi_pred/∂x   = -10 n_i / (ln(10) d_i²) · u_i
    # We linearize the PREDICTION (not the residual): obs ≈ pred(x) + J·dp,
    # so J_i := ∂rssi_pred/∂x_i = -(10 n_i / (ln10 d_i²)) · (x - x_i)ᵀ.
    # GN normal equations then read JᵀWJ · dp = JᵀW · r with r = obs - pred,
    # and dp adds directly to x. (Sign matters — got bitten here once.)
    #
    # IRLS Huber on dB residuals (k = 3 dB ≈ 0.6 σ_typical) provides the
    # same multipath-outlier robustness the distance-domain version had,
    # but now the threshold has a unit that doesn't drift with distance.
    LN10 = math.log(10.0)
    # Huber threshold in dB: residuals up to k_db keep full weight, beyond
    # get scaled by k/|r|. We want this to be ≈ 2σ — close enough to all
    # plausible Gaussian samples that we don't asymmetrically prune the
    # bulk, but tight enough to clip true multipath outliers (which show
    # up as 15-25 dB residuals). With σ_RSSI in the 3-5 dB range typical
    # for indoor BLE, 10 dB is the right operating point.
    HUBER_K_DB = 10.0
    irls_scale = [1.0] * len(items)  # multiplicative on base w_db
    # Bancroft is enough; 2-3 GN steps converge in normal noise.
    # 8 iterations max with step-norm early exit. In benign noise (σ≤3 dB)
    # this converges in 2-3 steps; in heavy noise (σ=5 dB) the Bancroft
    # warm start can be 2-5 m off and we sometimes need 6-8 GN steps.
    # Cost is trivial (6×6 normal-equation builds per iteration).
    for it in range(8):
        J: list[list[float]] = []
        r_vec: list[float] = []
        ws: list[float] = []
        for k, ((xi, yi, zi), _d_init, w_db, rssi_corr, p0_i, n_i, _sig) in enumerate(items):
            dx, dy, dz = p[0] - xi, p[1] - yi, p[2] - zi
            dist2 = dx * dx + dy * dy + dz * dz
            dist = math.sqrt(dist2)
            if dist < 0.05:
                # Singular near the leaf — skip. Should be vanishingly
                # rare since leaves are typically ≥1 m from the device.
                continue
            rssi_pred = p0_i - 10.0 * n_i / LN10 * math.log(dist)
            r = rssi_corr - rssi_pred
            # J := ∂rssi_pred/∂x = -(10n/(ln10·d²)) · (x - x_leaf)
            jac_scale = -10.0 * n_i / (LN10 * dist2)
            J.append([jac_scale * dx, jac_scale * dy, jac_scale * dz])
            r_vec.append(r)
            ws.append(w_db * irls_scale[k])
        if len(J) < 3:
            break
        JtWJ = [[0.0] * 3 for _ in range(3)]
        JtWr = [0.0, 0.0, 0.0]
        for k in range(len(J)):
            w = ws[k]
            for i in range(3):
                JtWr[i] += w * J[k][i] * r_vec[k]
                for j in range(3):
                    JtWJ[i][j] += w * J[k][i] * J[k][j]
        dp = _solve_3x3(JtWJ, JtWr)
        if dp is None:
            break
        step_norm = math.sqrt(dp[0]**2 + dp[1]**2 + dp[2]**2)
        # Trust-region clip: GN in log-space can overstep when the warm
        # start is bad. 2 m per iteration is generous indoors.
        if step_norm > 2.0:
            scale = 2.0 / step_norm
            dp = (dp[0] * scale, dp[1] * scale, dp[2] * scale)
        p = (p[0] + dp[0], p[1] + dp[1], p[2] + dp[2])
        # IRLS reweight on dB residuals at the NEW p.
        irls_scale = []
        for ((xi, yi, zi), _d_init, _w_db, rssi_corr, p0_i, n_i, _sig) in items:
            dx, dy, dz = p[0] - xi, p[1] - yi, p[2] - zi
            dist = math.sqrt(dx * dx + dy * dy + dz * dz)
            if dist < 0.05:
                irls_scale.append(0.0)
                continue
            rssi_pred = p0_i - 10.0 * n_i / LN10 * math.log(dist)
            r = abs(rssi_corr - rssi_pred)
            huber = 1.0 if r <= HUBER_K_DB else (HUBER_K_DB / max(r, 1e-3))
            irls_scale.append(huber)
        if step_norm < 0.005:
            break

    # Bounds check
    if bounds is not None:
        if not (bounds[0] <= p[0] <= bounds[1]
                and bounds[2] <= p[1] <= bounds[3]
                and bounds[4] <= p[2] <= bounds[5]):
            return None

    # ---- Stage 3: position covariance Σ_p = (JᵀWJ)⁻¹ ----
    # Same JtWJ matrix from final iteration; invert it for the cov.
    # Tikhonov regularization: add eps*tr(M)*I to JtWJ before inversion.
    # This is a no-op for well-conditioned matrices (eps=1e-12 of the
    # spectral scale) but bounds the worst-case condition number when the
    # geometry is near-degenerate (e.g., all leaves nearly co-linear).
    # Replaces the prior |det|<1e-12 hard gate, which produced a binary
    # "answer / no answer" cliff at exactly the wrong moment.
    cov_3x3 = _invert_3x3(_tikhonov_regularize_3x3(JtWJ)) or [[0.25, 0, 0], [0, 0.25, 0], [0, 0, 0.25]]

    # Residual RMS for diagnostic, reported in BOTH domains:
    #  - rmse_db is what the MLE objective actually minimizes (use this
    #    for goodness-of-fit checks against σ_RSSI)
    #  - rmse_m is the distance-domain residual against the biased d_init
    #    estimates, kept for backwards compatibility with the dashboard.
    sq_db = 0.0
    sq_m = 0.0
    for (xi, yi, zi), d_init, _w_db, rssi_corr, p0_i, n_i, _sig in items:
        dist = math.sqrt((p[0]-xi)**2 + (p[1]-yi)**2 + (p[2]-zi)**2)
        sq_m += (dist - d_init) ** 2
        if dist > 0.05:
            rssi_pred = p0_i - 10.0 * n_i / LN10 * math.log(dist)
            sq_db += (rssi_corr - rssi_pred) ** 2
    rmse_m = math.sqrt(sq_m / len(items))
    rmse_db = math.sqrt(sq_db / len(items))

    return {
        "x": p[0], "y": p[1], "z": p[2],
        "rmse_m": rmse_m,
        "rmse_db": rmse_db,
        "n_leaves_used": len(items),
        "cov": cov_3x3,  # 3×3 position covariance for uncertainty ellipsoid
    }


def _invert_3x3(M: list[list[float]]) -> list[list[float]] | None:
    """Invert a 3×3 matrix by cofactor expansion."""
    a, b, c = M[0]
    d, e, f = M[1]
    g, h, i = M[2]
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(det) < 1e-12:
        return None
    inv_det = 1.0 / det
    return [
        [(e * i - f * h) * inv_det, (c * h - b * i) * inv_det, (b * f - c * e) * inv_det],
        [(f * g - d * i) * inv_det, (a * i - c * g) * inv_det, (c * d - a * f) * inv_det],
        [(d * h - e * g) * inv_det, (b * g - a * h) * inv_det, (a * e - b * d) * inv_det],
    ]


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


# Tikhonov regularization scale. With eps_tik=1e-10, the added ridge is
# 1e-10 * tr(M) on each diagonal entry — utterly negligible compared to
# eigenvalues of a well-conditioned M, but enough to lift the smallest
# eigenvalue off zero when the geometry is rank-deficient. The bound on
# the condition number after regularization is κ(M_reg) ≤ (λ_max + eps*tr(M))
# / (eps*tr(M)) ≈ 1/eps when M is degenerate, so κ ≤ ~1e10 — solvable in
# double precision without catastrophic loss of significance.
_EPS_TIKHONOV = 1e-10


def _tikhonov_regularize_3x3(M: list[list[float]]) -> list[list[float]]:
    """Return M + eps*tr(M)*I as a fresh 3x3. Scale-invariant: doubling M
    doubles the ridge, so eps acts on the relative spectral scale rather
    than an absolute threshold. Works for any symmetric PSD M (the case
    that actually arises: JᵀWJ, innovation covariances, Fisher info)."""
    tr = M[0][0] + M[1][1] + M[2][2]
    # tr can be zero only if M is identically zero; in that case a
    # vanishing ridge is correct (the solve will still fail, which is the
    # honest answer). Use abs(tr) so we tolerate the rare numerically-
    # negative trace from drift in a PSD update.
    lam = _EPS_TIKHONOV * abs(tr)
    return [
        [M[0][0] + lam, M[0][1],       M[0][2]      ],
        [M[1][0],       M[1][1] + lam, M[1][2]      ],
        [M[2][0],       M[2][1],       M[2][2] + lam],
    ]


def _tikhonov_solve_3x3(M: list[list[float]],
                        b: list[float]
                        ) -> tuple[float, float, float] | None:
    """Solve M·x = b after Tikhonov-regularizing M.

    For callers that need a *solve* (not the inverse matrix), this is
    strictly better than _solve_3x3 + det-gate: well-conditioned M passes
    through unchanged (ridge ≈ machine-epsilon · ‖M‖), and rank-deficient
    M yields the ridge-regression solution instead of None. The output is
    the minimum-norm solution along the deficient directions, which is
    the right answer when "we have no information here" is the truth.
    """
    return _solve_3x3(_tikhonov_regularize_3x3(M), b)
