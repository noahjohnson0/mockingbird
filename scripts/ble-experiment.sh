#!/bin/bash
# `mockingbird ble` — query the Pi-side observation DB for a recent window
# and print the analysis. The leaves stream continuously now (firmware
# v0.3+), so there's no POST/wait/GET — just pick a time window.
#
# Usage:
#   bash scripts/ble-experiment.sh                # default: last 60s
#   bash scripts/ble-experiment.sh 5m             # last 5 minutes
#   bash scripts/ble-experiment.sh 1h             # last hour

set -euo pipefail

WIN=${1:-60s}

scp -q ~/repos/mockingbird/scripts/analyze_ble_db.py pi@mockingbird-pi:/home/pi/
ssh pi@mockingbird-pi "python3 /home/pi/analyze_ble_db.py --last '$WIN'"
