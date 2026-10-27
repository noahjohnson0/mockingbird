"""
Smoke tests for scripts/eda/*.py against a synthetic SQLite fixture.

The point of these tests is *not* to validate the numbers the scripts
produce — that's what `docs/data/eda-plan.md` is for, against real data.
The point is to catch the boring failure modes:

- A script that raises on an empty window.
- A script that raises on a "tiny but plausible" DB.
- A script that writes garbage to --out.
- A regression where someone breaks the shared CLI in _common.

We build a tiny in-memory-ish SQLite fixture (3 leaves, ~30 macs, mixed
addr_types and manuf data) and shell out to each script. Anything that
exits non-zero is a test failure.

Kept deliberately small. If a script needs richer statistical testing,
that belongs in a proper unit test of its inner functions, not here.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
EDA_DIR = REPO_ROOT / "scripts" / "eda"


def _build_fixture(path: Path, *, seed: int = 7) -> None:
    """Create a tiny but structurally complete collector DB."""
    rng = random.Random(seed)
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE obs (
            ts        REAL    NOT NULL,
            leaf      TEXT    NOT NULL,
            mac       TEXT    NOT NULL,
            rssi      INTEGER NOT NULL,
            addr_type INTEGER,
            name      TEXT,
            manuf     TEXT,
            leaf_t_ms INTEGER,
            location  TEXT
        );
        CREATE INDEX i_obs_ts ON obs(ts);
        CREATE INDEX i_obs_mac_ts ON obs(mac, ts);
        CREATE INDEX i_obs_leaf_ts ON obs(leaf, ts);

        CREATE TABLE leaf_events (
            ts    REAL NOT NULL,
            leaf  TEXT NOT NULL,
            event TEXT NOT NULL,
            info  TEXT,
            location TEXT
        );
        CREATE INDEX i_evt_ts ON leaf_events(ts);
        """
    )

    leaves = ["mockingbird-aaa", "mockingbird-bbb", "mockingbird-ccc"]
    # 5 "universal" public-MAC beacons + 20 random-MAC phones
    universal = [(f"AA:BB:CC:00:00:{i:02x}", 0, "Beacon", "004C0215") for i in range(5)]
    phones = [
        (f"DD:EE:FF:{i:02x}:{rng.randint(0,255):02x}:{rng.randint(0,255):02x}", 1, None, "060001")
        for i in range(20)
    ]

    t1 = 1_700_000_000.0  # fixed anchor for reproducibility
    t0 = t1 - 3600.0  # one-hour window of data

    # Per-leaf bias (dB): a, b, c
    bias = {"mockingbird-aaa": +2.0, "mockingbird-bbb": -1.0, "mockingbird-ccc": 0.0}

    rows = []
    # universal: each leaf sees each beacon many times across the hour
    for mac, atype, name, manuf in universal:
        true_rssi = rng.uniform(-70, -50)
        for leaf in leaves:
            for _ in range(40):
                ts = rng.uniform(t0, t1)
                r = int(round(true_rssi + bias[leaf] + rng.gauss(0, 2)))
                rows.append((ts, leaf, mac, r, atype, name, manuf, None, None))

    # phones: random subset of leaves, fewer obs
    for mac, atype, name, manuf in phones:
        seen_by = rng.sample(leaves, k=rng.choice([1, 1, 2, 3]))
        n = rng.randint(2, 15)
        true_rssi = rng.uniform(-90, -60)
        for leaf in seen_by:
            for _ in range(n):
                ts = rng.uniform(t0, t1)
                r = int(round(true_rssi + bias[leaf] + rng.gauss(0, 3)))
                rows.append((ts, leaf, mac, r, atype, name, manuf, None, None))

    con.executemany("INSERT INTO obs VALUES (?,?,?,?,?,?,?,?,?)", rows)

    # heartbeats every 10s for each leaf, plus one gap on leaf b
    for leaf in leaves:
        ts = t0
        while ts < t1:
            con.execute(
                "INSERT INTO leaf_events (ts, leaf, event, info) VALUES (?,?,?,?)",
                (ts, leaf, "hb", json.dumps({"heap": 120000})),
            )
            ts += 10.0
            if leaf == "mockingbird-bbb" and t0 + 1200 < ts < t0 + 1500:
                ts += 400.0  # induce a >5min gap
    # one disconnect on leaf c
    con.execute(
        "INSERT INTO leaf_events (ts, leaf, event, info) VALUES (?,?,?,?)",
        (t0 + 2000, "mockingbird-ccc", "disconnect", None),
    )

    con.commit()
    con.close()


@pytest.fixture(scope="module")
def fixture_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("eda") / "obs.sqlite"
    _build_fixture(path)
    return path


@pytest.fixture(scope="module")
def empty_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("eda_empty") / "obs.sqlite"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE obs (
            ts REAL NOT NULL, leaf TEXT NOT NULL, mac TEXT NOT NULL,
            rssi INTEGER NOT NULL, addr_type INTEGER, name TEXT,
            manuf TEXT, leaf_t_ms INTEGER, location TEXT
        );
        CREATE TABLE leaf_events (
            ts REAL NOT NULL, leaf TEXT NOT NULL, event TEXT NOT NULL,
            info TEXT, location TEXT
        );
        """
    )
    con.commit()
    con.close()
    return path


def _run(script: str, db: Path, *extra: str) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(EDA_DIR / script), "--db", str(db), "--since", "2h", *extra]
    return subprocess.run(cmd, capture_output=True, text=True)


PRIORITY = [
    "p1_manufacturer_breakdown.py",
    "p2_coverage_histogram.py",
    "p3_per_leaf_rssi_bias.py",
    "p4_per_leaf_rates.py",
    "p5_device_churn.py",
    "p6_heartbeat_freshness.py",
]


@pytest.mark.parametrize("script", PRIORITY)
def test_script_runs_on_fixture(script, fixture_db):
    cp = _run(script, fixture_db)
    assert cp.returncode == 0, f"{script} failed:\nstdout={cp.stdout}\nstderr={cp.stderr}"
    assert "===" in cp.stdout  # header was printed


@pytest.mark.parametrize("script", PRIORITY)
def test_script_handles_empty_window(script, empty_db):
    # Pointing at an empty DB should not crash; it should print something
    # and exit cleanly (or with a controlled SystemExit message).
    cp = _run(script, empty_db, "--since", "1h")
    # Either "(no observations)" path returns 0, or resolve_window raises
    # SystemExit("empty window") with returncode 1. Both are acceptable.
    assert cp.returncode in (0, 1), f"{script} crashed unexpectedly:\n{cp.stderr}"


def test_csv_output(fixture_db, tmp_path):
    out = tmp_path / "manuf.csv"
    cp = _run("p1_manufacturer_breakdown.py", fixture_db, "--out", str(out))
    assert cp.returncode == 0, cp.stderr
    assert out.exists() and out.stat().st_size > 0
    head = out.read_text().splitlines()[0]
    assert "manuf_id" in head and "n_obs" in head


def test_json_output(fixture_db, tmp_path):
    out = tmp_path / "cov.json"
    cp = _run("p2_coverage_histogram.py", fixture_db, "--out", str(out))
    assert cp.returncode == 0, cp.stderr
    data = json.loads(out.read_text())
    assert isinstance(data, list) and data
    assert "n_leaves" in data[0]


def test_run_all_runs(fixture_db):
    cmd = [sys.executable, str(EDA_DIR / "run_all.py"), "--db", str(fixture_db), "--since", "2h"]
    cp = subprocess.run(cmd, capture_output=True, text=True)
    assert cp.returncode == 0, f"run_all failed:\n{cp.stdout}\n{cp.stderr}"
    # All six priority headers should appear in stdout
    for s in PRIORITY:
        assert s in cp.stdout, f"run_all did not invoke {s}"


def test_bias_recovers_known_offset(fixture_db):
    # The fixture has bias {aaa: +2, bbb: -1, ccc: 0}. The script should
    # rank leaves in that order after residualizing per-MAC.
    cp = _run("p3_per_leaf_rssi_bias.py", fixture_db)
    assert cp.returncode == 0, cp.stderr
    # Slice to the bias table by header marker, then read leaf rows from it.
    lines = cp.stdout.splitlines()
    idx = None
    for i, ln in enumerate(lines):
        if "Per-leaf bias" in ln:
            idx = i
            break
    assert idx is not None, f"no bias section in output:\n{cp.stdout}"
    # stop at next section header (line starting with "-- ")
    end = len(lines)
    for j in range(idx + 1, len(lines)):
        if lines[j].startswith("-- "):
            end = j
            break
    section = lines[idx:end]
    leaf_lines = [ln for ln in section if ln.startswith("mockingbird-")]
    # Table is sorted ascending by bias_db. So aaa (+2) should be last, bbb (-1) first.
    assert leaf_lines, "no leaf rows in bias table output"
    first = leaf_lines[0].split()[0]
    last = leaf_lines[-1].split()[0]
    assert first == "mockingbird-bbb", f"expected bbb most-negative; got {first}"
    assert last == "mockingbird-aaa", f"expected aaa most-positive; got {last}"
