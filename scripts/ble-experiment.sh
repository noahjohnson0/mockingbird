#!/bin/bash
# Run a simultaneous BLE capture across all reachable mockingbird leaves,
# then transfer to the Pi and analyze pairwise.
#
# Usage:
#   bash scripts/ble-experiment.sh [duration_s] [leaf...]
#
# Examples:
#   bash scripts/ble-experiment.sh                 # 60s, all discovered leaves
#   bash scripts/ble-experiment.sh 30              # 30s, all discovered leaves
#   bash scripts/ble-experiment.sh 60 192.168.8.244 192.168.8.196   # explicit

set -euo pipefail

DUR=${1:-60}
shift || true

# Discover leaves if none were given. We rely on ARP (already populated by
# the burst-ping in /mockingbird's `nodes` output) and an mDNS sanity check.
if [ $# -eq 0 ]; then
    # Refresh ARP first
    for n in $(seq 2 254); do
        ping -c 1 -W 80 -t 1 192.168.8.$n >/dev/null 2>&1 &
    done
    wait 2>/dev/null
    LEAVES=$(arp -an | awk '/192\.168\.8\./ && /8c:94:df/ {gsub(/[()]/, "", $2); print $2}')
else
    LEAVES="$*"
fi

if [ -z "$LEAVES" ]; then
    echo "no mockingbird leaves found on 192.168.8.0/24 — flash one first" >&2
    exit 1
fi

echo "=== leaves: ==="; echo "$LEAVES" | sed 's/^/  /'
echo

OUT=~/repos/.scratch/ble-captures
mkdir -p "$OUT"
TS=$(date +%Y%m%dT%H%M%S)

# --- 1. simultaneous scan/reset ---
echo "=== POST /scan/reset to all leaves ==="
for ip in $LEAVES; do
    curl -s -X POST --max-time 5 "http://$ip/scan/reset" &
done
wait
echo

# --- 2. wait the capture window ---
echo "=== capturing for ${DUR}s ==="
for r in $(seq "$DUR" -10 1); do
    [ $((r % 10)) -eq 0 ] && echo "  ${r}s..."
    sleep 1
done

# --- 3. pull results ---
echo "=== pulling /scan/result ==="
declare -a FILES
for ip in $LEAVES; do
    HOST=$(curl -s --max-time 5 "http://$ip/version" >/dev/null && \
           curl -s --max-time 10 "http://$ip/" | python3 -c "import sys,json; print(json.load(sys.stdin)['hostname'])")
    SUFFIX=$(echo "$HOST" | sed 's/^mockingbird-//')
    F="$OUT/${SUFFIX}-${TS}.json"
    curl -s --max-time 30 "http://$ip/scan/result" > "$F"
    SZ=$(ls -lh "$F" | awk '{print $5}')
    echo "  $ip  →  $F  ($SZ)"
    FILES+=("$F")
done

# --- 4. transfer + analyze on the Pi ---
echo
echo "=== copy to Pi and analyze ==="
scp -q "${FILES[@]}" ~/repos/mockingbird/scripts/analyze_ble_capture.py \
    pi@mockingbird-pi:/tmp/

if [ ${#FILES[@]} -ge 2 ]; then
    A=$(basename "${FILES[0]}")
    B=$(basename "${FILES[1]}")
    ssh pi@mockingbird-pi "python3 /tmp/analyze_ble_capture.py /tmp/$A /tmp/$B"
    if [ ${#FILES[@]} -gt 2 ]; then
        echo
        echo "(showing pair 1↔2; analyzer is currently 2-input — extend for N when you add a 3rd leaf)"
    fi
else
    echo "only one leaf reachable — skipping pairwise analysis, but the JSON is at $FILES"
fi
