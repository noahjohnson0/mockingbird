#!/bin/bash
# /usr/local/sbin/mockingbird-wlan-watchdog.sh
#
# Safety-net watchdog for the Pi Zero W's wlan0. Runs every 60s under
# the mockingbird-wlan-watchdog.timer.
#
# Strategy: ping the Opal (192.168.8.1) 3x. If 0/3 succeed, increment a
# fail counter and escalate based on how many consecutive minutes we've
# been silent.
#
#   3 fails  (~3 min)  -> nmcli connection up Mockingbird
#   5 fails  (~5 min)  -> radio off / on cycle + reconnect
#   8 fails  (~8 min)  -> reboot
#
# Counter is reset on any successful ping.
#
# Context: docs/ops/rca/2026-05-13-pi-wlan0-wedge.md (MAC-3).

set -u

GATEWAY="${GATEWAY:-192.168.8.1}"
CONN="${CONN:-Mockingbird}"
COUNT_FILE="/run/mockingbird-wlan-fail-count"
LOG_TAG="mockingbird-wlan-watchdog"

log() {
    logger -t "$LOG_TAG" -- "$*"
}

read_count() {
    if [[ -r "$COUNT_FILE" ]]; then
        cat "$COUNT_FILE" 2>/dev/null || echo 0
    else
        echo 0
    fi
}

write_count() {
    echo "$1" > "$COUNT_FILE"
}

# 3 pings, 1s timeout each. -q quiet; we only care about exit code.
if ping -c 3 -W 1 -q "$GATEWAY" >/dev/null 2>&1; then
    prev=$(read_count)
    if [[ "$prev" -gt 0 ]]; then
        log "gateway reachable again; resetting counter (was $prev)"
    fi
    write_count 0
    exit 0
fi

prev=$(read_count)
count=$((prev + 1))
write_count "$count"
log "gateway $GATEWAY unreachable; fail count = $count"

case "$count" in
    3)
        log "escalation: nmcli connection up $CONN"
        nmcli connection up "$CONN" 2>&1 | logger -t "$LOG_TAG" || true
        ;;
    5)
        log "escalation: radio off/on cycle"
        nmcli radio wifi off 2>&1 | logger -t "$LOG_TAG" || true
        sleep 5
        nmcli radio wifi on 2>&1 | logger -t "$LOG_TAG" || true
        sleep 2
        nmcli connection up "$CONN" 2>&1 | logger -t "$LOG_TAG" || true
        ;;
    8)
        log "escalation: REBOOTING — 8 consecutive minutes with no gateway. See RCA 2026-05-13."
        # Give the journal time to flush before we go down.
        sync
        sleep 1
        systemctl reboot
        ;;
esac

exit 0
