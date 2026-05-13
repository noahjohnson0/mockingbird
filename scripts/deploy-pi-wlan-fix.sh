#!/bin/bash
# Deploy the MAC-3 wlan0 wedge fix to the Pi.
#
# Layers shipped by this script:
#   1. /etc/NetworkManager/conf.d/wifi-powersave.conf   (PS off by default)
#   2. /etc/modprobe.d/brcmfmac.conf                    (roamoff + feature_disable)
#   3. /usr/local/sbin/mockingbird-wlan-watchdog.sh + systemd timer/service
#   4. Persistent journal at /var/log/journal
#   5. apt install iw (for future debugging)
#
# Idempotent. Safe to re-run.
#
# Usage:
#   bash scripts/deploy-pi-wlan-fix.sh
#   PI_HOST=192.168.8.202 bash scripts/deploy-pi-wlan-fix.sh
#
# WARNING: this WILL briefly drop SSH when it bounces the Mockingbird
# connection to apply the new powersave setting. Reconnect after ~10s.
#
# Context: docs/ops/rca/2026-05-13-pi-wlan0-wedge.md

set -euo pipefail

PI_HOST="${PI_HOST:-192.168.8.202}"
PI_USER="${PI_USER:-pi}"
CREDS="${PI_CREDS_FILE:-$HOME/repos/.scratch/pi-creds.txt}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC_PI="$REPO_ROOT/services/pi"

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf '[!] %s\n' "$*" >&2; exit 1; }

# --- Preflight ---------------------------------------------------------------

[[ -f "$CREDS" ]] || die "creds file not found at $CREDS"

if ! command -v sshpass >/dev/null 2>&1; then
    die "sshpass not installed. On macOS: brew install hudochenkov/sshpass/sshpass"
fi

for f in \
    "$SRC_PI/etc-NetworkManager-conf.d-wifi-powersave.conf" \
    "$SRC_PI/etc-modprobe.d-brcmfmac.conf" \
    "$SRC_PI/wlan-watchdog.sh" \
    "$SRC_PI/mockingbird-wlan-watchdog.service" \
    "$SRC_PI/mockingbird-wlan-watchdog.timer" \
    "$SRC_PI/install-persistent-journal.sh"
do
    [[ -f "$f" ]] || die "missing source file: $f"
done

# Extract the password line robustly (matches both 'password: foo' and
# 'password:  foo  with  spaces'). Strip the leading 'password:' label
# and any surrounding whitespace.
PI_PW=$(awk -F': *' '$1=="password"{
    sub(/^[^:]*: */,"",$0);
    sub(/[[:space:]]+$/,"",$0);
    print; exit
}' "$CREDS")
[[ -n "$PI_PW" ]] || die "could not extract password from $CREDS (expected line starting with 'password:')"

# Stash the password in a tempfile mode 0600 so sshpass -f can read it.
# Trap cleans it up on any exit.
PW_TMP=$(mktemp -t mac3-pi-pw.XXXXXX)
chmod 600 "$PW_TMP"
printf '%s' "$PI_PW" > "$PW_TMP"
trap 'rm -f "$PW_TMP"' EXIT

SSH_OPTS=(
    -o StrictHostKeyChecking=accept-new
    -o ConnectTimeout=10
    -o ServerAliveInterval=10
)

SSH()  { sshpass -f "$PW_TMP" ssh "${SSH_OPTS[@]}" "$PI_USER@$PI_HOST" "$@"; }
SCP()  { sshpass -f "$PW_TMP" scp "${SSH_OPTS[@]}" "$@"; }

# Confirm we can actually reach the Pi before we start.
log "preflight: poking $PI_USER@$PI_HOST"
if ! SSH "true" 2>/dev/null; then
    die "cannot ssh to $PI_USER@$PI_HOST — is the Pi up and on the LAN?"
fi
log "preflight: ok"

# --- Stage files into /tmp on the Pi ----------------------------------------

log "staging files in /tmp on the Pi"
SCP \
    "$SRC_PI/etc-NetworkManager-conf.d-wifi-powersave.conf" \
    "$SRC_PI/etc-modprobe.d-brcmfmac.conf" \
    "$SRC_PI/wlan-watchdog.sh" \
    "$SRC_PI/mockingbird-wlan-watchdog.service" \
    "$SRC_PI/mockingbird-wlan-watchdog.timer" \
    "$SRC_PI/install-persistent-journal.sh" \
    "$PI_USER@$PI_HOST:/tmp/" >/dev/null

# --- Install (root) ----------------------------------------------------------
# Feed the sudo password over stdin via `sudo -S`. Use a single heredoc so
# we don't pay round-trip latency per command, and so the whole thing is
# transactional-ish (errexit will bail at the first failure).

log "installing on Pi (sudo block)"
SSH "sudo -S -p '' bash -s" <<EOF
$PI_PW
set -euo pipefail

echo '[install] placing configs'
install -m 644 -o root -g root /tmp/etc-NetworkManager-conf.d-wifi-powersave.conf \
    /etc/NetworkManager/conf.d/wifi-powersave.conf
install -m 644 -o root -g root /tmp/etc-modprobe.d-brcmfmac.conf \
    /etc/modprobe.d/brcmfmac.conf

echo '[install] placing watchdog script'
install -m 755 -o root -g root /tmp/wlan-watchdog.sh \
    /usr/local/sbin/mockingbird-wlan-watchdog.sh

echo '[install] placing systemd units'
install -m 644 -o root -g root /tmp/mockingbird-wlan-watchdog.service \
    /etc/systemd/system/mockingbird-wlan-watchdog.service
install -m 644 -o root -g root /tmp/mockingbird-wlan-watchdog.timer \
    /etc/systemd/system/mockingbird-wlan-watchdog.timer

echo '[install] persistent journal'
bash /tmp/install-persistent-journal.sh

echo '[install] apt install iw (for future debugging)'
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq iw >/dev/null

echo '[install] systemd reload + enable timer'
systemctl daemon-reload
systemctl enable --now mockingbird-wlan-watchdog.timer

echo '[install] cleaning /tmp'
rm -f /tmp/etc-NetworkManager-conf.d-wifi-powersave.conf \
      /tmp/etc-modprobe.d-brcmfmac.conf \
      /tmp/wlan-watchdog.sh \
      /tmp/mockingbird-wlan-watchdog.service \
      /tmp/mockingbird-wlan-watchdog.timer \
      /tmp/install-persistent-journal.sh

echo '[install] done.'
EOF

# --- Apply the new powersave setting (this will drop SSH briefly) ------------

log "bouncing the Mockingbird connection so powersave takes effect"
log "  >>> SSH may drop for ~10s. This is expected. <<<"

# We can't keep the SSH session alive across `nmcli connection down`, so
# we fire the down+up from a backgrounded shell on the Pi and disown it.
# Then we sleep, reconnect, and verify.
SSH "sudo -S -p '' bash -c 'nohup bash -c \"sleep 1; nmcli connection down Mockingbird; sleep 2; nmcli connection up Mockingbird\" >/tmp/wlan-bounce.log 2>&1 &'" <<EOF || true
$PI_PW
EOF

log "waiting 20s for the link to come back"
sleep 20

# --- Verify ------------------------------------------------------------------

log "verifying"
for i in 1 2 3 4 5; do
    if SSH "true" 2>/dev/null; then break; fi
    log "  (reconnect attempt $i/5)"
    sleep 5
done

SSH "true" 2>/dev/null || die "Pi did not come back after the bounce — investigate manually (LAN IP $PI_HOST)"

echo "----- VERIFY: powersave -----"
SSH "iw dev wlan0 get power_save || true"

echo "----- VERIFY: roamoff -----"
SSH "cat /sys/module/brcmfmac/parameters/roamoff 2>/dev/null || echo '(module not yet reloaded — reboot to apply brcmfmac options)'"

echo "----- VERIFY: watchdog timer -----"
SSH "systemctl list-timers mockingbird-wlan-watchdog.timer --no-pager"

echo "----- VERIFY: persistent journal -----"
SSH "ls -ld /var/log/journal && journalctl --list-boots --no-pager | tail -5"

log "deploy complete."
log ""
log "NOTE: brcmfmac module options (roamoff/feature_disable) only take effect"
log "      on the next module load. A reboot is the cleanest way to apply them."
log "      Recommended next step (when you can spare ~30s of mesh downtime):"
log "          sshpass -f <pwfile> ssh $PI_USER@$PI_HOST sudo reboot"
