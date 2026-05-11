#!/bin/bash
# Deploy the mockingbird collector systemd service to a fresh Pi.
# Idempotent: safe to re-run.
#
# Usage:
#   bash scripts/deploy-collector.sh [pi-host]
# Defaults to pi-host = mockingbird-pi (resolved via Tailscale MagicDNS
# or local mDNS / /etc/hosts).

set -euo pipefail

PI=${1:-mockingbird-pi}
PI_PW=$(grep '^password:' ~/repos/.scratch/pi-creds.txt | cut -d' ' -f2-)

echo "[$(date +%H:%M:%S)] deploying collector to $PI"

# 1. Copy the service script + unit file
scp -q ~/repos/mockingbird/services/mockingbird-collector.py "pi@$PI:/home/pi/"
scp -q ~/repos/mockingbird/services/mockingbird-collector.service /tmp/mb-svc.service
scp -q /tmp/mb-svc.service "pi@$PI:/tmp/mb-svc.service"

# 2. Install + enable + start
ssh "pi@$PI" "echo '$PI_PW' | sudo -S bash -c '
set -e
mv /tmp/mb-svc.service /etc/systemd/system/mockingbird-collector.service
chown root:root /etc/systemd/system/mockingbird-collector.service
systemctl daemon-reload
systemctl enable --now mockingbird-collector.service
sleep 2
systemctl is-active mockingbird-collector.service
echo
ss -lntp 2>/dev/null | grep 9001 || echo \"(port 9001 not listening yet — check journalctl)\"
'"

echo
echo "[$(date +%H:%M:%S)] collector deployed; tailing the journal for 5s..."
ssh "pi@$PI" 'sudo journalctl -u mockingbird-collector.service --no-pager -n 10'
