#!/bin/sh
# Install the journald disk-usage cap drop-in on the Pi.
# Idempotent: re-running is safe.
#
# Run on the Pi (or pipe through ssh):
#   ssh pi@mockingbird-pi 'sudo sh -s' < scripts/install-journald-cap.sh
#
# Rollback:
#   sudo rm /etc/systemd/journald.conf.d/mockingbird.conf
#   sudo systemctl restart systemd-journald

set -eu

DROPIN_DIR=/etc/systemd/journald.conf.d
DROPIN_FILE="$DROPIN_DIR/mockingbird.conf"

mkdir -p "$DROPIN_DIR"

cat > "$DROPIN_FILE" <<'EOF'
# Managed by mockingbird repo: scripts/install-journald-cap.sh
# Source of truth: services/journald-mockingbird.conf
[Journal]
SystemMaxUse=500M
EOF

chmod 0644 "$DROPIN_FILE"

systemctl restart systemd-journald

echo "Installed $DROPIN_FILE and restarted systemd-journald."
echo "Current journal usage:"
journalctl --disk-usage
