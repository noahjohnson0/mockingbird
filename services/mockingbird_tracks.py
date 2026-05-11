"""mockingbird_tracks — multi-MAC track tracker.

Apple devices rotate their BLE advertised MAC about every 15 minutes (per
the Continuity / RPA spec). A passive observer sees the same physical
device as a stream of different addresses, each typically alive for ~15
min before vanishing while a new address with similar RSSI fingerprint
appears nearby.

This module bridges that. We assign each MAC to a "track" — a persistent
identity that survives address rotation. Tracks are identified by a
stable short codename (adjective + bird) derived from a UUID. The
dashboard renders tracks, not MACs.

Matching strategy (v1, RSSI-only):
  - Each track caches its recent RSSI fingerprint as
    {leaf: ewma_rssi}.
  - For each currently-visible MAC, compute its RSSI vector
    {leaf: max_rssi_in_last_window}.
  - Compare candidate MAC against every active track using RMS deviation
    over shared "in-room" leaves (rssi >= IN_ROOM_RSSI). Require >=2
    such shared leaves to compute a meaningful match.
  - If the best match has RMS <= MATCH_RMS_DBM_THRESHOLD: assign the
    MAC to that track. Else: spawn a new track.
  - Tracks unseen for FORGET_AFTER_S are archived.

This is intentionally simple — RMS-on-shared-leaves is robust to the
fact that not every leaf sees every device, and dodges normalization
problems (path-loss differences across leaves cancel out).
"""

from __future__ import annotations

import math
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable

# ---- tuning constants ----
IN_ROOM_RSSI = -78               # leaves below this don't count toward matching
MATCH_RMS_DBM_THRESHOLD = 6.0    # best-match RMS deviation cap for assignment
MIN_SHARED_LEAVES = 2            # need ≥2 in-room leaves in common
FORGET_AFTER_S = 300             # 5 min idle → track archived
EWMA_ALPHA = 0.35                # RSSI fingerprint smoothing per update
TRAIL_HISTORY_LEN = 80           # ~40 s at 500 ms poll cadence
MIN_TRAIL_DELTA_S = 0.4          # don't append a trail point more often than this

# ---- position smoothing ----
# Per-track EWMA on the raw position estimate. RSSI jitter of ±5–10 dB
# at n≈2.0 produces 3× distance error per single sample, so without
# smoothing a stationary device appears to dance around by 1–2 m. We
# adapt α based on jump distance so the filter is "soft" for small
# motion (trusts new samples) but "hard" for big jumps (treats them as
# probable noise, gives them tiny weight). Net effect: stationary
# devices stay rock-still, real motion still tracks.
SMOOTH_ALPHA_MIN  = 0.08         # for big jumps — heavy filter
SMOOTH_ALPHA_MAX  = 0.45         # for tiny jumps — barely filter at all
SMOOTH_JUMP_SOFT  = 0.40         # m, below = ~max α (full trust)
SMOOTH_JUMP_HARD  = 2.00         # m, above = ~min α (very suspicious)


def _smooth_alpha(jump_m: float) -> float:
    """Map jump magnitude to a smoothing weight α ∈ [MIN, MAX]."""
    if jump_m <= SMOOTH_JUMP_SOFT:
        return SMOOTH_ALPHA_MAX
    if jump_m >= SMOOTH_JUMP_HARD:
        return SMOOTH_ALPHA_MIN
    # Linear interp between SOFT (full trust) and HARD (high suspicion)
    t = (jump_m - SMOOTH_JUMP_SOFT) / (SMOOTH_JUMP_HARD - SMOOTH_JUMP_SOFT)
    return SMOOTH_ALPHA_MAX - t * (SMOOTH_ALPHA_MAX - SMOOTH_ALPHA_MIN)

# ---- name pool, same scheme as the client-side codenames ----
ADJECTIVES = (
    "ruby", "jade", "ash", "copper", "slate", "dusk", "dawn", "amber",
    "frost", "ember", "silver", "pearl", "onyx", "rust", "smoke", "rose",
)
BIRDS = (
    "auk", "jay", "owl", "dove", "hawk", "wren", "lark", "kite",
    "gull", "tern", "crow", "robin", "raven", "finch", "swift", "snipe",
    "ibis", "egret", "heron", "plover", "pipit", "vireo", "junco", "oriole",
    "thrush", "merlin", "falcon", "magpie", "sparrow", "swallow", "shrike", "condor",
)


def codename_for(track_id: str) -> str:
    """Deterministic adjective+bird+suffix name from a track UUID.

    16 adjectives × 32 birds = 512 combos isn't enough for ~50 simultaneous
    tracks (birthday paradox kicks in). A 2-char hex suffix from the
    track_id bumps the space to ~131k combos, dropping collision probability
    below 1% for our scale.
    """
    h = 0
    for c in track_id:
        h = (h * 31 + ord(c)) & 0xFFFFFFFF
    a = ADJECTIVES[h % len(ADJECTIVES)]
    h2 = 0
    for c in track_id + ":bird":
        h2 = (h2 * 31 + ord(c)) & 0xFFFFFFFF
    b = BIRDS[h2 % len(BIRDS)]
    # Suffix: last 2 chars of the track_id (deterministic, human-readable)
    suffix = track_id[-2:] if len(track_id) >= 2 else "00"
    return f"{a} {b}·{suffix}"


@dataclass
class TrailPoint:
    ts: float
    x: float
    y: float
    z: float


@dataclass
class Track:
    track_id: str
    first_seen: float
    last_seen: float
    current_mac: str
    mac_history: list[str] = field(default_factory=list)   # ordered, most recent last
    fingerprint: dict[str, float] = field(default_factory=dict)  # leaf → ewma rssi
    trail: deque[TrailPoint] = field(default_factory=lambda: deque(maxlen=TRAIL_HISTORY_LEN))
    last_position: tuple[float, float, float] | None = None   # smoothed
    raw_position: tuple[float, float, float] | None = None    # latest unsmoothed
    last_trail_ts: float = 0.0

    @property
    def name(self) -> str:
        return codename_for(self.track_id)

    def update(self, mac: str, rssi_vec: dict[str, int], pos: tuple[float, float, float] | None, now: float) -> None:
        if mac != self.current_mac:
            if self.current_mac and self.current_mac != mac:
                self.mac_history.append(self.current_mac)
                if len(self.mac_history) > 8:
                    self.mac_history = self.mac_history[-8:]
            self.current_mac = mac
        self.last_seen = now
        # EWMA-update fingerprint
        for leaf, rssi in rssi_vec.items():
            prev = self.fingerprint.get(leaf)
            self.fingerprint[leaf] = (
                rssi if prev is None
                else (1 - EWMA_ALPHA) * prev + EWMA_ALPHA * rssi
            )
        if pos is not None:
            self.raw_position = pos
            # Adaptive EWMA on position: small jumps (≤ 0.4m) trusted
            # heavily, big jumps (≥ 2m) treated as probable noise.
            if self.last_position is None:
                self.last_position = pos
            else:
                lx, ly, lz = self.last_position
                jump = math.sqrt((pos[0]-lx)**2 + (pos[1]-ly)**2 + (pos[2]-lz)**2)
                a = _smooth_alpha(jump)
                self.last_position = (
                    a * pos[0] + (1 - a) * lx,
                    a * pos[1] + (1 - a) * ly,
                    a * pos[2] + (1 - a) * lz,
                )
            # Use smoothed position for the trail too — trails read as
            # smooth motion instead of noisy zigzags.
            if now - self.last_trail_ts >= MIN_TRAIL_DELTA_S:
                self.trail.append(TrailPoint(now, *self.last_position))
                self.last_trail_ts = now


def _rssi_rms_delta(a: dict[str, float], b: dict[str, int]) -> tuple[float, int]:
    """RMS deviation between two RSSI vectors, computed only over leaves
    present in BOTH with strength >= IN_ROOM_RSSI. Returns (rms, n_shared)."""
    shared = [(a[k], b[k]) for k in a.keys() & b.keys()
              if a[k] >= IN_ROOM_RSSI and b[k] >= IN_ROOM_RSSI]
    n = len(shared)
    if n == 0:
        return (math.inf, 0)
    sq = sum((x - y) ** 2 for x, y in shared)
    return (math.sqrt(sq / n), n)


class TrackStore:
    """In-process registry of active tracks. Singleton in the dashboard
    process. Thread-safety: only one HTTP handler runs at a time in our
    ThreadingHTTPServer-with-per-request-cache setup, but we still guard
    mutations behind a coarse lock since /api/live calls can interleave
    with /api/leaves estimate calls."""

    def __init__(self) -> None:
        self.tracks: dict[str, Track] = {}
        self.mac_to_track: dict[str, str] = {}
        import threading
        self._lock = threading.Lock()

    def step(self, devices: Iterable[dict], now: float | None = None,
             calibration=None, positions: dict | None = None,
             multilat_bounds: tuple | None = None) -> list[dict]:
        """Assign every input device to a track (existing or new); return
        an enriched list with track metadata.

        If calibration + positions are given, also re-positions each
        device using *the track's accumulated RSSI fingerprint* (which
        has more leaf coverage than any single short-lived MAC) instead
        of the per-MAC fingerprint. Returns smoothed positions.
        """
        if now is None:
            now = time.time()
        out: list[dict] = []
        with self._lock:
            self._gc(now)
            # Tracks already claimed in this step — Apple Continuity rotates
            # MACs one-at-a-time, so two simultaneously-live MACs are NOT
            # the same physical device, no matter how similar their RSSI.
            claimed: set[str] = set()
            for d in devices:
                mac: str = d["mac"]
                rssi_vec = {hit["leaf"]: hit["rssi"] for hit in d.get("leaves", [])}
                # Initial position: whatever centroid /api/live computed.
                # We may overwrite below using the TRACK's fingerprint
                # (which has wider leaf coverage than this single MAC).
                pos = (d.get("x"), d.get("y"), d.get("z"))
                if pos[0] is None:
                    pos = None
                # 1. Quick path: MAC is already on a known track
                tid = self.mac_to_track.get(mac)
                if tid and tid in self.tracks:
                    track = self.tracks[tid]
                    track.update(mac, rssi_vec, pos, now)
                    claimed.add(track.track_id)
                # 2. Slow path: try to match into an existing track
                else:
                    track = self._best_match(rssi_vec, claimed, now)
                    if track is None:
                        tid = uuid.uuid4().hex[:12]
                        track = Track(
                            track_id=tid, first_seen=now, last_seen=now,
                            current_mac=mac,
                        )
                        self.tracks[tid] = track
                    track.update(mac, rssi_vec, pos, now)
                    self.mac_to_track[mac] = track.track_id
                    claimed.add(track.track_id)

                # ---- track-level multilat using accumulated fingerprint ----
                pos_method = d.get("pos_method", "centroid")
                if calibration is not None and positions is not None:
                    # Track fingerprint: {leaf: ewma_rssi}; rounded ints OK
                    track_fp = {leaf: int(round(r)) for leaf, r in track.fingerprint.items()
                                if leaf in positions}
                    if len(track_fp) >= 4:
                        # Lazy import to avoid circular dep
                        from mockingbird_calibration import multilaterate
                        res = multilaterate(track_fp, positions, calibration, bounds=multilat_bounds)
                        if res is not None:
                            px, py, pz, _rmse = res
                            # Pump through the smoother (replaces last_position EWMA)
                            track.update(mac, {}, (px, py, pz), now)
                            pos_method = "multilat-track"

                trail = [{"ts": p.ts, "x": p.x, "y": p.y, "z": p.z} for p in track.trail]
                smoothed = track.last_position
                enriched = {
                    **d,
                    "raw_x": d.get("x"), "raw_y": d.get("y"), "raw_z": d.get("z"),
                    "x": smoothed[0] if smoothed else d.get("x"),
                    "y": smoothed[1] if smoothed else d.get("y"),
                    "z": smoothed[2] if smoothed else d.get("z"),
                    "track_id": track.track_id,
                    "track_name": track.name,
                    "track_age_s": round(now - track.first_seen, 1),
                    "track_macs": list(track.mac_history) + [track.current_mac],
                    "track_leaves": len(track.fingerprint),
                    "trail": trail,
                    "pos_method": pos_method,
                }
                out.append(enriched)
        return out

    def _best_match(self, rssi_vec: dict[str, int], claimed: set[str], now: float) -> Track | None:
        """Pick the closest existing track (by RSSI fingerprint RMS) that:
          - has at least MIN_SHARED_LEAVES in-room leaves in common
          - is below MATCH_RMS_DBM_THRESHOLD on RMS deviation
          - has NOT already been claimed by another MAC in this step
            (one physical device → one current MAC at a time)
          - has NOT been seen in the last MAC_HANDOFF_GAP_S (avoids
            merging two co-located distinct devices whose RSSI happens
            to look similar; rotation is typically a clean handoff with
            a small dead window)
        """
        MAC_HANDOFF_GAP_S = 1.0
        best: Track | None = None
        best_rms = math.inf
        for track in self.tracks.values():
            if track.track_id in claimed:
                continue
            if now - track.last_seen < MAC_HANDOFF_GAP_S:
                continue
            rms, n = _rssi_rms_delta(track.fingerprint, rssi_vec)
            if n < MIN_SHARED_LEAVES:
                continue
            if rms < best_rms:
                best_rms = rms
                best = track
        if best is not None and best_rms <= MATCH_RMS_DBM_THRESHOLD:
            return best
        return None

    def _gc(self, now: float) -> None:
        dead = [tid for tid, t in self.tracks.items() if now - t.last_seen > FORGET_AFTER_S]
        for tid in dead:
            t = self.tracks.pop(tid, None)
            if not t:
                continue
            for mac in [t.current_mac, *t.mac_history]:
                if self.mac_to_track.get(mac) == tid:
                    self.mac_to_track.pop(mac, None)


# Module-level singleton — imported and reused by the dashboard handler.
store = TrackStore()
