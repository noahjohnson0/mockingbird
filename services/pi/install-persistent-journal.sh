#!/bin/bash
# Idempotent: switch systemd-journald from volatile (/run/log/journal) to
# persistent (/var/log/journal). Without this, evidence of the wedge is
# wiped on every reboot/power-cycle.
#
# Run as root on the Pi (the deploy script already does this).
#
# Context: docs/ops/rca/2026-05-13-pi-wlan0-wedge.md (MAC-3).

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "must be root" >&2
    exit 1
fi

if [[ -d /var/log/journal ]]; then
    echo "[persistent-journal] /var/log/journal already exists — nothing to create"
else
    echo "[persistent-journal] creating /var/log/journal"
    mkdir -p /var/log/journal
fi

# tmpfiles.d wires up correct ownership/ACLs for /var/log/journal.
echo "[persistent-journal] running systemd-tmpfiles --create"
systemd-tmpfiles --create --prefix /var/log/journal

echo "[persistent-journal] restarting systemd-journald"
systemctl restart systemd-journald

# Sanity check.
sleep 1
boots=$(journalctl --list-boots 2>/dev/null | wc -l | tr -d ' ')
echo "[persistent-journal] journalctl --list-boots now sees $boots boot(s)"
echo "[persistent-journal] done."
