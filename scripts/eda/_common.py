"""
Shared helpers for the EDA scripts in this directory.

These scripts each ask one well-scoped question of the collector DB. The
common loader handles:

- CLI argument parsing (`--db`, `--since`, `--until`, `--out`, `--format`).
- Resolution of a time window. `--since` / `--until` accept either an
  absolute ISO-8601 timestamp ("2026-05-13T09:00:00") or a relative
  expression like "24h", "30m", "7d" anchored at MAX(ts) in the DB (NOT
  wall-clock — the collector clock is what generated the data, and on a
  Pi Zero W with intermittent NTP we trust the data clock over our own).
- Connection setup with a read-only URI so EDA never accidentally writes
  back to a live collector DB.
- Optional output to CSV or JSON for downstream analysis.

Design notes (Wanjiru):
- Defaults are 24h windows. Long enough to catch diurnal structure,
  short enough that queries stay quick on a Pi-sized DB.
- We resolve "now" as MAX(ts) in the DB. If the DB is stale (collector
  off for a day), `--since 24h` still yields data — that's the right
  default for offline analysis. If you want strict wall-clock, pass an
  absolute `--until`.
- Output format intentionally minimal: terminal-readable text first,
  optional machine-readable second. The point of these scripts is to
  produce findings, not dashboards.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence


DEFAULT_DB = "~/mockingbird/observations.sqlite"
DEFAULT_WINDOW = "24h"


_REL_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd])\s*$", re.IGNORECASE)
_UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def _parse_when(value: str) -> tuple[str, float]:
    """Return ('rel', seconds) or ('abs', epoch_seconds)."""
    m = _REL_RE.match(value)
    if m:
        n, unit = m.groups()
        return ("rel", float(n) * _UNITS[unit.lower()])
    # try absolute ISO
    try:
        # accept either with or without timezone; treat naive as local
        import datetime as _dt

        v = value.strip()
        if v.endswith("Z"):
            v = v[:-1] + "+00:00"
        dt = _dt.datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return ("abs", dt.timestamp())
    except Exception as e:
        raise argparse.ArgumentTypeError(
            f"could not parse '{value}' as either relative (e.g. '24h') or ISO-8601"
        ) from e


def add_common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--db",
        default=DEFAULT_DB,
        help=f"path to observations.sqlite (default: {DEFAULT_DB})",
    )
    p.add_argument(
        "--since",
        default=DEFAULT_WINDOW,
        help=(
            "start of the window. Relative ('24h', '30m') is relative to "
            "MAX(ts) in the DB, NOT wall-clock. Absolute is ISO-8601. "
            f"Default: {DEFAULT_WINDOW}."
        ),
    )
    p.add_argument(
        "--until",
        default=None,
        help=(
            "end of the window. Same parsing as --since. "
            "Default: MAX(ts) in the DB."
        ),
    )
    p.add_argument(
        "--out",
        default=None,
        help="optional output file (.csv or .json) for the primary table",
    )
    p.add_argument(
        "--format",
        default=None,
        choices=("csv", "json"),
        help="force output format. Inferred from --out extension otherwise.",
    )


@dataclass
class Window:
    t0: float
    t1: float
    db_path: Path

    @property
    def span_s(self) -> float:
        return max(0.0, self.t1 - self.t0)


def resolve_db_path(arg: str) -> Path:
    return Path(os.path.expanduser(arg))


def open_db(path: Path) -> sqlite3.Connection:
    """Open the collector DB read-only. Fails fast if the file is missing."""
    if not path.exists():
        raise SystemExit(f"DB not found: {path}")
    uri = f"file:{path}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def resolve_window(con: sqlite3.Connection, since: str, until: str | None) -> Window:
    """Pick (t0, t1) from CLI args, anchored at MAX(ts) for relative values."""
    row = con.execute("SELECT MAX(ts) AS t_max, MIN(ts) AS t_min FROM obs").fetchone()
    t_max = row["t_max"]
    if t_max is None:
        # empty DB; fall back to wall-clock so the script still runs
        t_max = time.time()

    if until is None:
        t1 = float(t_max)
    else:
        kind, val = _parse_when(until)
        t1 = (t_max - val) if kind == "rel" else val

    kind, val = _parse_when(since)
    t0 = (t1 - val) if kind == "rel" else val

    if t0 >= t1:
        raise SystemExit(f"empty window: since={since} until={until} → [{t0}, {t1}]")
    # db_path filled in by caller
    return Window(t0=float(t0), t1=float(t1), db_path=Path())


def format_ts(ts: float | None) -> str:
    if ts is None:
        return "—"
    import datetime as _dt

    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def print_header(title: str, window: Window) -> None:
    print(f"\n=== {title} ===")
    print(
        f"window: {format_ts(window.t0)}  →  {format_ts(window.t1)}  "
        f"({window.span_s/3600:.2f} h)"
    )
    print(f"db:     {window.db_path}")
    print()


def print_table(
    rows: Sequence[dict[str, Any]],
    columns: Sequence[str] | None = None,
    max_rows: int | None = 30,
) -> None:
    """Minimal aligned-column printer. No external deps."""
    if not rows:
        print("(no rows)")
        return
    cols = list(columns) if columns else list(rows[0].keys())
    str_rows = [[_fmt(r.get(c)) for c in cols] for r in rows]
    widths = [max(len(c), *(len(sr[i]) for sr in str_rows)) for i, c in enumerate(cols)]
    sep = "  "
    print(sep.join(c.ljust(widths[i]) for i, c in enumerate(cols)))
    print(sep.join("-" * w for w in widths))
    shown = str_rows if max_rows is None else str_rows[:max_rows]
    for sr in shown:
        print(sep.join(sr[i].ljust(widths[i]) for i in range(len(cols))))
    if max_rows is not None and len(str_rows) > max_rows:
        print(f"... ({len(str_rows) - max_rows} more rows)")


def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        if abs(v) >= 1e6 or (v != 0 and abs(v) < 1e-3):
            return f"{v:.3g}"
        return f"{v:.3f}".rstrip("0").rstrip(".") or "0"
    return str(v)


def write_output(rows: Iterable[dict[str, Any]], out: str | None, fmt: str | None) -> None:
    if not out:
        return
    rows = list(rows)
    if fmt is None:
        ext = Path(out).suffix.lower().lstrip(".")
        fmt = ext if ext in ("csv", "json") else "csv"
    if fmt == "json":
        with open(out, "w") as f:
            json.dump(rows, f, indent=2, default=str)
    else:
        if not rows:
            Path(out).write_text("")
            return
        with open(out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    print(f"\nwrote {len(rows)} rows → {out}")


def standard_setup(parser: argparse.ArgumentParser) -> tuple[argparse.Namespace, sqlite3.Connection, Window]:
    args = parser.parse_args()
    db_path = resolve_db_path(args.db)
    con = open_db(db_path)
    window = resolve_window(con, args.since, args.until)
    window.db_path = db_path
    return args, con, window
