#!/usr/bin/env python3
"""Live 'is Noah moving?' watcher — v4.

Two complementary movement signals, EITHER triggers MOVING:

  1. SWING — top-3 σ across in-room devices over the last 20 s.
     Catches in-room transit (walking from desk to closet, etc.) where
     devices' peak-RSSI leaf shifts.

  2. COUNT-DROP — change in number of devices in 'in-room' RSSI range
     vs the 2-minute baseline. Catches walking OUT of the room (body-
     worn devices drop below -75 dBm and disappear) and walking back IN
     (count rebounds toward baseline).

Loosened IN_ROOM_RSSI to -75 dBm so devices stay in the eligible set
during a slow walk-away instead of vanishing the moment σ would peak.
"""

import collections
import sqlite3
import statistics
import time

DB = "/home/pi/mockingbird/observations.sqlite"
WINDOW_S = 20
POLL_S = 2
IN_ROOM_RSSI = -75
MIN_SAMPLES = 4
MIN_PAIRS = 3

MOVE_SWING = 6.0           # top-3 median σ → "MOVING"
STAT_SWING = 4.0           # below this → calm
COUNT_DROP_FRAC = 0.30     # 30% deviation from baseline = movement event
SUSTAIN = 2                # consecutive ticks before flipping state

# Rolling baseline of in-room MAC counts (last ~2 min @ 2s poll = 60 samples)
baseline = collections.deque(maxlen=60)

last_state = None
candidate = None
state_count = 0

while True:
    now = time.time()
    db = sqlite3.connect(DB)
    rows = db.execute("""
        SELECT mac, leaf, rssi
        FROM obs
        WHERE ts >= ? AND rssi >= ? AND manuf LIKE '4c00%'
    """, (now - WINDOW_S, IN_ROOM_RSSI)).fetchall()
    db.close()

    grouped: dict[tuple[str, str], list[int]] = {}
    for mac, leaf, rssi in rows:
        grouped.setdefault((mac, leaf), []).append(rssi)
    eligible = {k: rs for k, rs in grouped.items() if len(rs) >= MIN_SAMPLES}
    stds = [(statistics.stdev(rs), mac, leaf) for (mac, leaf), rs in eligible.items()]
    stds.sort(reverse=True)
    macs = {mac for (mac, _) in eligible}
    n_macs = len(macs)

    # Signal 1: swing
    if len(stds) >= MIN_PAIRS:
        swing = statistics.median([s[0] for s in stds[:3]])
    else:
        swing = 0.0

    # Signal 2: count delta vs baseline (excluding the most recent N samples
    # so the baseline reflects "before the current event")
    baseline.append(n_macs)
    if len(baseline) >= 15:
        # use the median of older samples as baseline
        older = list(baseline)[:-10]
        base_count = statistics.median(older) if older else n_macs
    else:
        base_count = n_macs
    count_delta_frac = (n_macs - base_count) / max(base_count, 1)

    # State decision
    moving = swing >= MOVE_SWING or abs(count_delta_frac) >= COUNT_DROP_FRAC
    if n_macs < MIN_PAIRS:
        new_state = "AWAY"
    elif moving:
        new_state = "MOVING"
    elif swing < STAT_SWING and abs(count_delta_frac) < 0.10:
        new_state = "stationary"
    else:
        new_state = "settling"

    # Hysteresis
    if new_state == candidate:
        state_count += 1
    else:
        candidate = new_state
        state_count = 1
    state = last_state or new_state
    if state_count >= SUSTAIN:
        state = candidate

    marker = " →" if state != last_state else "  "
    bar_w = min(40, int(swing * 4))
    bar = "█" * bar_w + "·" * (40 - bar_w)
    delta_arrow = "↓" if count_delta_frac < -0.1 else "↑" if count_delta_frac > 0.1 else " "

    ts = time.strftime("%H:%M:%S")
    print(f"{marker} {ts}  {state:<11} swing={swing:>4.1f}  macs={n_macs:>2}/{int(base_count):>2}{delta_arrow}  {bar}",
          flush=True)
    last_state = state
    time.sleep(POLL_S)
