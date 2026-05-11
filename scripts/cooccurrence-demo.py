"""Find device co-occurrence clusters: MACs that appear together in time +
space (similar RSSI vector) are likely the same person's gadgets.

Even though Apple Continuity rotates MACs every ~15 min, within a single
window you'll see a phone+watch+AirPods cluster moving together with
similar 4-leaf RSSI fingerprints.
"""
import sqlite3, time
from collections import defaultdict

db = sqlite3.connect("/home/pi/mockingbird/observations.sqlite")
now = time.time()
WINDOW = 1800  # last 30 min

# Bucket each MAC into a 10s bucket → set of (leaf, avg_rssi) tuples.
# Two MACs that share most of their (bucket, leaf, rssi±5) signatures
# are likely co-located gadgets.

rows = db.execute("""
    SELECT mac, leaf, cast(ts/10 as int) as bucket, AVG(rssi) as r
    FROM obs
    WHERE ts >= ? - ?
    GROUP BY mac, leaf, bucket
""", (now, WINDOW)).fetchall()

# Group by (bucket, leaf, rssi_bin) — same key = co-located in that 10s window
fp = defaultdict(set)  # (bucket, leaf, rssi_bin) → {mac, ...}
mac_fp = defaultdict(set)  # mac → {(bucket, leaf, rssi_bin), ...}
for mac, leaf, bucket, r in rows:
    rbin = int(r // 6) * 6  # 6-dB bins
    key = (bucket, leaf, rbin)
    fp[key].add(mac)
    mac_fp[mac].add(key)

# Find MAC pairs that share ≥3 fingerprints AND aren't always-present (we
# want mobile/transient devices, not the Govee thermometer).
def n_buckets(m):
    return len({b for (b,_,_) in mac_fp[m]})

# Filter: only MACs that appeared in < 80% of buckets (mobile/transient)
total_buckets = max(n_buckets(m) for m in mac_fp) if mac_fp else 1
mobile = [m for m in mac_fp if n_buckets(m) < 0.8 * total_buckets and n_buckets(m) >= 3]

# Score pairs by Jaccard
pairs = {}
for m1 in mobile:
    for m2 in mobile:
        if m1 >= m2: continue
        a, b = mac_fp[m1], mac_fp[m2]
        inter = len(a & b)
        if inter < 3: continue
        union = len(a | b)
        pairs[(m1, m2)] = (inter / union, inter, len(a), len(b))

if not pairs:
    print("No strong co-occurrence pairs found in this window (need more data or more movement)")
else:
    print(f"=== device pairs co-located in last 30 min (top 15 by Jaccard, ≥3 shared buckets) ===")
    print(f"  {'MAC A':17}  {'MAC B':17}  {'J%':>4}  shared  A_buckets  B_buckets")
    for (m1, m2), (j, inter, a, b) in sorted(pairs.items(), key=lambda x: -x[1][0])[:15]:
        print(f"  {m1}  {m2}  {j*100:>3.0f}%  {inter:>6}  {a:>9}  {b:>9}")

# Also: hourly histogram of observation count (when is this household busy?)
print()
print("=== observation rate per minute, last 30 min (presence heatbar) ===")
rows = db.execute("""
    SELECT cast((? - ts)/60 as int) as min_ago, COUNT(*) as n
    FROM obs
    WHERE ts >= ? - 1800
    GROUP BY min_ago ORDER BY min_ago
""", (now, now)).fetchall()
for mins, n in rows:
    bar_n = min(40, n // 60)  # 60 obs/bar
    print(f"  {mins:>2} min ago  {n:>5}  {'▓' * bar_n}")
