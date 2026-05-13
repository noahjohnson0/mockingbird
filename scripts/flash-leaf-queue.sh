#!/usr/bin/env bash
# Flash a single mockingbird leaf over OTA with a chosen queue depth, then
# verify the device came back online with the expected version.
#
# Usage:  scripts/flash-leaf-queue.sh <chipid> <queue_size>
#         scripts/flash-leaf-queue.sh 4ce184 256
#
# The corresponding platformio env (ota / ota-q128 / ota-q256 / ota-q512)
# is selected automatically from queue_size.

set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo "usage: $0 <chipid> <queue_size>"
    echo "  chipid     short MAC tail, e.g. 4ce184"
    echo "  queue_size one of 64, 128, 256, 512"
    exit 1
fi

CHIP="$1"
QSIZE="$2"
# Accept either chip-id (resolves via mDNS) or raw IP. Mac→Tailnet→Pi-subnet
# routing doesn't always carry mDNS through, so IP is the reliable form.
if [[ "$CHIP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    HOST="$CHIP"
else
    HOST="mockingbird-${CHIP}.local"
fi
VERSION="0.5.0-q${QSIZE}"

case "$QSIZE" in
    64)  ENV="ota" ;;
    128) ENV="ota-q128" ;;
    256) ENV="ota-q256" ;;
    512) ENV="ota-q512" ;;
    *)   echo "queue_size must be 64/128/256/512, got: $QSIZE"; exit 1 ;;
esac

cd "$(dirname "$0")/../firmware/esp32-wroom-mockingbird"

echo "[$(date +%H:%M:%S)] flashing ${CHIP} → ${VERSION} (env=${ENV})"
pio run -e "$ENV" -t upload --upload-port "$HOST" 2>&1 | tail -6

echo
echo "[$(date +%H:%M:%S)] waiting 15s for reboot…"
sleep 15

for i in 1 2 3 4 5; do
    if resp=$(curl -s --max-time 3 "http://${HOST}/version" 2>/dev/null); then
        echo "  attempt $i: /version = $resp"
        if [[ "$resp" == "$VERSION" ]]; then
            echo "[$(date +%H:%M:%S)] ✓ ${CHIP} confirmed at ${VERSION}"
            exit 0
        fi
    else
        echo "  attempt $i: no response yet"
    fi
    sleep 3
done

echo "[$(date +%H:%M:%S)] ✗ ${CHIP} did not confirm new version — check logs"
exit 1
