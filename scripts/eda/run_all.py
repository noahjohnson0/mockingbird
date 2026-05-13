#!/usr/bin/env python3
"""
Top-level EDA runner.

Runs the priority analyses in order and prints a single report-style
summary to stdout. Use this when you want a "what does the data say?"
snapshot before opening any of the individual scripts.

The priority order matches the EDA plan in docs/data/eda-plan.md:
    1. p1_manufacturer_breakdown  — what's the population?
    2. p2_coverage_histogram      — is trilateration even viable?
    3. p3_per_leaf_rssi_bias      — is calibration working?
    4. p4_per_leaf_rates          — operational baseline
    5. p5_device_churn            — static vs. transient mix
    6. p6_heartbeat_freshness     — mesh reliability

USAGE
-----
    python scripts/eda/run_all.py
    python scripts/eda/run_all.py --since 6h
    python scripts/eda/run_all.py --db /tmp/snapshot.sqlite --since 30m
"""

from __future__ import annotations

import argparse
import runpy
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Order matters: priority sequence per the EDA plan.
PRIORITY_SCRIPTS = [
    ("p1_manufacturer_breakdown.py", "Priority 1 — Manufacturer / advertiser breakdown"),
    ("p2_coverage_histogram.py",     "Priority 2 — Coverage histogram"),
    ("p3_per_leaf_rssi_bias.py",     "Priority 3 — Per-leaf RSSI bias"),
    ("p4_per_leaf_rates.py",         "Per-leaf observation rates"),
    ("p5_device_churn.py",           "Device churn"),
    ("p6_heartbeat_freshness.py",    "Heartbeat freshness"),
]


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--db", default="~/mockingbird/observations.sqlite")
    p.add_argument("--since", default="24h")
    p.add_argument("--until", default=None)
    p.add_argument(
        "--only",
        default=None,
        help="comma-separated script-name prefixes to run (e.g. 'p1,p2,p3')",
    )
    p.add_argument(
        "--stop-on-error",
        action="store_true",
        help="abort on first script error (default: continue)",
    )
    args = p.parse_args()

    only = {s.strip() for s in args.only.split(",")} if args.only else None

    print("=" * 72)
    print("mockingbird EDA — run_all report")
    print(f"  db:    {args.db}")
    print(f"  since: {args.since}  until: {args.until or 'MAX(ts)'}")
    print("=" * 72)

    failures = []
    for script, title in PRIORITY_SCRIPTS:
        if only and not any(script.startswith(prefix) for prefix in only):
            continue
        print(f"\n\n{'#' * 72}")
        print(f"# {title}")
        print(f"# {script}")
        print("#" * 72)

        argv = [str(HERE / script), "--db", args.db, "--since", args.since]
        if args.until:
            argv += ["--until", args.until]

        old_argv = sys.argv
        sys.argv = argv
        try:
            runpy.run_path(str(HERE / script), run_name="__main__")
        except SystemExit as e:
            if e.code not in (None, 0):
                failures.append((script, f"exit {e.code}"))
                if args.stop_on_error:
                    break
        except Exception as e:
            failures.append((script, repr(e)))
            print(f"\n[run_all] {script} raised: {e!r}")
            if args.stop_on_error:
                break
        finally:
            sys.argv = old_argv

    print("\n\n" + "=" * 72)
    if failures:
        print("FAILURES:")
        for s, msg in failures:
            print(f"  {s}: {msg}")
        sys.exit(1)
    else:
        print("All requested EDA scripts completed.")
    print("=" * 72)


if __name__ == "__main__":
    main()
