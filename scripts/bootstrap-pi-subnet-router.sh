#!/bin/bash
# Bootstrap a Raspberry Pi (Bookworm armhf or armv7+) into the
# noahnet-pi role: Tailscale subnet router on Mockingbird WiFi,
# advertising 192.168.8.0/24.
#
# Idempotent: safe to re-run.
#
# Requires the Pi to already be reachable over SSH as `pi@<host>` with
# password auth (we use the saved password from pi-creds.txt for sudo).
# The first SSH connection should have key auth set up via ssh-copy-id.
#
# Usage:  bash scripts/bootstrap-pi-subnet-router.sh <pi-host>
# Example: bash scripts/bootstrap-pi-subnet-router.sh 192.168.0.128

set -euo pipefail

PI=${1:-${PI_HOST:-raspberrypi.local}}
SCRATCH=~/repos/.scratch

# --- pull credentials from the local scratch dir ---
[ -f "$SCRATCH/pi-creds.txt"     ] || { echo "missing $SCRATCH/pi-creds.txt" >&2; exit 1; }
[ -f "$SCRATCH/noahnet-wifi.txt" ] || { echo "missing $SCRATCH/noahnet-wifi.txt" >&2; exit 1; }

PI_PW=$(grep '^password:' "$SCRATCH/pi-creds.txt" | cut -d' ' -f2-)
SSID=$(grep ^SSID= "$SCRATCH/noahnet-wifi.txt" | cut -d= -f2-)
PSK=$(grep  ^PSK=  "$SCRATCH/noahnet-wifi.txt" | cut -d= -f2-)

echo "Bootstrapping $PI"
echo "  noahnet SSID: $SSID"
echo

# --- remote: install Tailscale, configure WiFi, enable forwarding ---
ssh "pi@$PI" "echo '$PI_PW' | sudo -S bash -s" <<REMOTE
set -euo pipefail

echo '>>> [1/4] install Tailscale (idempotent)'
if ! command -v tailscale >/dev/null; then
    curl -fsSL https://tailscale.com/install.sh | sh
else
    echo '    tailscale already installed'
fi

echo '>>> [2/4] add Mockingbird WiFi connection if missing'
if ! nmcli -t -f NAME con show | grep -qx '$SSID'; then
    nmcli con add type wifi con-name '$SSID' ifname wlan0 ssid '$SSID'
    nmcli con modify '$SSID' wifi-sec.key-mgmt wpa-psk wifi-sec.psk '$PSK'
    nmcli con modify '$SSID' connection.autoconnect yes connection.autoconnect-priority 100
fi
echo '    nmcli connections:'
nmcli -t -f NAME,TYPE,AUTOCONNECT-PRIORITY con show

echo '>>> [3/4] enable IP forwarding (persistent)'
cat > /etc/sysctl.d/99-tailscale.conf <<SYSCTL
net.ipv4.ip_forward = 1
net.ipv6.conf.all.forwarding = 1
SYSCTL
sysctl -p /etc/sysctl.d/99-tailscale.conf

echo '>>> [4/4] start tailscale and apply route advertisement'
# Apply prefs idempotently — \`tailscale set\` only updates flags, doesn't reauth.
# If the daemon is not logged in yet, \`set\` is a no-op until \`tailscale up\`
# completes the device login.
if tailscale status >/dev/null 2>&1; then
    tailscale set --advertise-routes=192.168.8.0/24 --accept-routes --hostname=noahnet-pi
    echo '    state:'
    tailscale status | head -3
else
    echo '    NOT YET LOGGED IN — run interactively:'
    echo '       sudo tailscale up --advertise-routes=192.168.8.0/24 --accept-routes --hostname=noahnet-pi'
    echo '    then open the printed URL in a browser, log in, and approve.'
fi

echo
echo '>>> done. To switch the Pi onto Mockingbird right now:'
echo '       sudo nmcli con up $SSID'
echo '    (SSH will drop; reconnect at the Pi\\'s new IP under 192.168.8.x)'
REMOTE

echo
echo "After WiFi switch, find the Pi via mDNS or ARP scan:"
echo "  arp -an | grep b8:27:eb"
echo
echo "Don't forget: approve the 192.168.8.0/24 subnet route in"
echo "  https://login.tailscale.com/admin/machines"
echo "(toggle on under 'Edit route settings' for noahnet-pi)."
