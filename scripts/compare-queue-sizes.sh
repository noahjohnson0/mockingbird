#!/usr/bin/env bash
# Sample /api/system twice, separated by WINDOW seconds, and show drop% by
# firmware version. Useful for A/B comparing queue sizes across the fleet.
#
# Usage:  scripts/compare-queue-sizes.sh [window_seconds]   default 60

set -euo pipefail
WINDOW="${1:-60}"
PI_URL="${PI_URL:-http://mockingbird-pi:8080}"

echo "[$(date +%H:%M:%S)] sampling /api/system, then again in ${WINDOW}s"

curl -s "$PI_URL/api/system" > /tmp/qcmp_a.json

# also fetch the leaf /version for each leaf to learn its firmware
declare -A VER
for chip in 4ce184 4c36ec 4c0bdc 4db204 4ce1d8 4ceb7c 4d4384 92838c; do
    VER[$chip]=$(curl -s --max-time 2 "http://mockingbird-${chip}.local/version" 2>/dev/null || echo "?")
done

sleep "$WINDOW"
curl -s "$PI_URL/api/system" > /tmp/qcmp_b.json

python3 <<EOF
import json
a = {L["leaf"]: L for L in json.load(open("/tmp/qcmp_a.json"))["leaves"]}
b = {L["leaf"]: L for L in json.load(open("/tmp/qcmp_b.json"))["leaves"]}
versions = $(python3 -c "import sys; print(repr({${','.join([f'\"mockingbird-{k}\":\"{v}\"' for k,v in VER.items()])}}))")
print()
print(f"{'leaf':<10} {'firmware':<14} {'Δsent':>7} {'Δdrop':>7} {'drop%':>6}  obs/s  q")
print("-" * 64)
groups = {}
for k in sorted(a.keys() & b.keys()):
    chip = k.replace("mockingbird-", "")
    v = versions.get(k, "?")
    ds = (b[k].get("n_sent") or 0) - (a[k].get("n_sent") or 0)
    dd = (b[k].get("n_dropped") or 0) - (a[k].get("n_dropped") or 0)
    pct = 100 * dd / max(ds + dd, 1)
    op = b[k].get("obs_per_s") or 0
    q  = b[k].get("q_depth")
    print(f"{chip:<10} {v:<14} {ds:>7} {dd:>7} {pct:>5.1f}%  {op:>4.1f}  {q}")
    groups.setdefault(v, [0, 0])
    groups[v][0] += ds
    groups[v][1] += dd

print()
print("---- grouped by firmware version ----")
print(f"{'version':<14} {'Σsent':>7} {'Σdrop':>7} {'drop%':>6}  delivered/s/leaf")
print("-" * 56)
for v, (s, d) in sorted(groups.items()):
    n_leaves = sum(1 for k in versions.values() if k == v)
    pct = 100 * d / max(s + d, 1)
    per_leaf = s / max(n_leaves, 1) / ${WINDOW}
    print(f"{v:<14} {s:>7} {d:>7} {pct:>5.1f}%  {per_leaf:>5.1f}/s ({n_leaves} leaves)")
EOF
