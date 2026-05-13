"""Test bootstrap: make `services/` importable so tests can do
`from mockingbird_calibration import ...` regardless of cwd.

The collector module is currently named `mockingbird-collector.py` (with
a hyphen) and is therefore NOT importable as-is — tests touching the
collector must wait for that file to be renamed. See coverage-audit.md.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SERVICES = _REPO_ROOT / "services"
if str(_SERVICES) not in sys.path:
    sys.path.insert(0, str(_SERVICES))
