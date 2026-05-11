#!/usr/bin/env python3
"""Record physical (x, y, z) coordinates for each leaf, in metres,
relative to a chosen room origin. Stored in a `leaf_position` table
in the collector's SQLite DB so the analyzer can do real trilateration.

Recommended origin convention:
  • Pick the corner of the room nearest the door (most natural reference).
  • x = distance from one wall (positive away from it)
  • y = distance from the perpendicular wall
  • z = height from floor

A Bosch GLM (or any laser tape) makes this trivial: stand at the corner,
shoot to each leaf, record x and y; then point up to leaf to get z, or
just measure to floor and subtract from the room's vertical reach.

Usage:
  ssh pi@mockingbird-pi 'python3 /home/pi/set-leaf-position.py list'
  ssh pi@mockingbird-pi 'python3 /home/pi/set-leaf-position.py set mockingbird-4ce184 1.2 0.5 1.4 "desk corner"'
"""

from __future__ import annotations

import sqlite3
import sys

DB = "/home/pi/mockingbird/observations.sqlite"


def setup(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS leaf_position (
            leaf      TEXT PRIMARY KEY,
            x         REAL NOT NULL,
            y         REAL NOT NULL,
            z         REAL NOT NULL,
            units     TEXT DEFAULT 'm',
            notes     TEXT,
            updated_ts REAL DEFAULT (strftime('%s','now'))
        );
    """)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2

    db = sqlite3.connect(DB, isolation_level=None)
    setup(db)
    cmd = sys.argv[1]

    if cmd == "list":
        rows = list(db.execute(
            "SELECT leaf, x, y, z, units, notes FROM leaf_position ORDER BY leaf"
        ))
        if not rows:
            print("(no positions recorded yet)")
        else:
            print(f"  {'leaf':28} {'x':>7} {'y':>7} {'z':>7} {'units':>6}  notes")
            for leaf, x, y, z, units, notes in rows:
                print(f"  {leaf:28} {x:>7.2f} {y:>7.2f} {z:>7.2f} {units:>6}  {notes or ''}")
        # Also show any leaves we have obs for but no recorded position
        seen = list(db.execute(
            "SELECT DISTINCT leaf FROM obs WHERE leaf NOT IN (SELECT leaf FROM leaf_position)"
        ))
        if seen:
            print()
            print("  unpositioned leaves seen in obs:")
            for (leaf,) in seen:
                print(f"    {leaf}")
        return 0

    if cmd == "set":
        if len(sys.argv) < 6:
            print("usage: set <leaf> <x> <y> <z> [notes]", file=sys.stderr)
            return 2
        leaf = sys.argv[2]
        x, y, z = float(sys.argv[3]), float(sys.argv[4]), float(sys.argv[5])
        notes = " ".join(sys.argv[6:]) if len(sys.argv) > 6 else None
        db.execute("""
            INSERT INTO leaf_position(leaf, x, y, z, notes, updated_ts)
            VALUES (?, ?, ?, ?, ?, strftime('%s','now'))
            ON CONFLICT(leaf) DO UPDATE SET
              x=excluded.x, y=excluded.y, z=excluded.z,
              notes=excluded.notes, updated_ts=excluded.updated_ts
        """, (leaf, x, y, z, notes))
        print(f"  set {leaf}  ({x}, {y}, {z}) m  notes={notes!r}")
        return 0

    if cmd == "delete":
        if len(sys.argv) < 3:
            print("usage: delete <leaf>", file=sys.stderr)
            return 2
        db.execute("DELETE FROM leaf_position WHERE leaf=?", (sys.argv[2],))
        print(f"  deleted {sys.argv[2]}")
        return 0

    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
