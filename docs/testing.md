# Testing methodology

This document captures the test pyramid, methodology, and the explicit
choices we've made (and not made) for testing mockingbird. It exists
so the rationale survives the inevitable "why isn't there CI?" question
six months from now, and so the test layout is a deliberate design
choice rather than an accident.

The codebase is solo-maintained. The dev machine is an M4 Max. The
production system is a Pi Zero W collector + (per #31) an M4 / N100
dashboard. The deployment is private, single-tenant. None of the
standard "team + cloud + CI/CD" cargo-cult applies. The pyramid below
is what does apply.

## The shape

```
       ┌───────────────────────────────┐
       │  D — ground-truth (manual)    │   hours, on release
       │  walked-phone accuracy runs   │
       ├───────────────────────────────┤
       │  C — regression baseline      │   ~30 s, every commit
       │  diff vs prior commit         │
       ├───────────────────────────────┤
       │  B — real-data replay         │   ~10 s, every commit
       │  frozen production captures   │
       ├───────────────────────────────┤
       │  A — math primitives (unit)   │   <1 s, every commit
       │  pure-Python, deterministic   │
       └───────────────────────────────┘
```

Wider at the bottom = many small tests. Taller upward = each tier
catches a different *class* of bug, not just a slower version of the
tier below.

## Tier A — math primitives (unit, pure-Python)

**Location:** `tests/unit/`
**Runtime:** < 1 second total on M4 Max
**Trigger:** every test invocation
**Catches:** off-by-one in Gauss-Newton, sign errors in covariance,
singular-matrix mishandling, NaN propagation, EWMA boundary mistakes.

**Examples:**

- `_solve_3x3` / `_invert_3x3`: known-answer round-trip; singular returns None
- `_linear_fit`: known regression slope on synthetic data; zero-variance returns (None, None)
- `kalman_step`: stationary update shrinks variance; pure prediction propagates state correctly
- `_clamp_pos_cov`: symmetrizes; clamps diagonal; limits correlation magnitudes
- `codename_for`: deterministic; low collision rate

**Style rules:**

- No mocks of internal modules. Import `mockingbird_calibration` and
  `mockingbird_tracks` and call them.
- At least one negative case per function (degeneracy, singular input,
  out-of-bounds).
- Property tests via `hypothesis` for invariants that should hold under
  *any* input — e.g. "every inverse is a left-inverse," "every Kalman
  update either shrinks or maintains variance."
- Monte Carlo trials with a fixed `rng_seed` fixture; bump to 10⁴+ where
  the math is non-trivial. The M4 can take it.

## Tier B — real-data replay (integration, fixture-DBs)

**Location:** `tests/replay/`
**Runtime:** ~10 s per fixture, ~30 s total
**Trigger:** every test invocation
**Catches:** silent calibration regressions, semantic position drift,
lost leaf coverage, pipeline-wide bugs that no single unit test
notices.

The principle: synthetic-noise tests are written by the same person
who wrote the model under test, so they share blind spots. Indoor RF
is dominated by multipath, body absorption, channel-37/38/39
frequency-selective fading, and Continuity MAC rotation — none of which
the simulator gets right. Real captures don't lie.

**Fixtures:**

- Live at `~/repos/.scratch/fixtures/obs-YYYYMMDD-HHMM-<tag>.sqlite`
  (gitignored — raw obs data is private and large)
- Synced from the Pi via `scripts/sync-obs-db.sh`
- Naming: ISO-date + short event tag (e.g. `obs-20260512-2200-pre-crash.sqlite`)

**Manifest:**

`tests/fixtures/manifest.yaml` (committed) records what each fixture
represents: time range, leaf set, notable events, expected calibration
band. Tests introspect the manifest rather than hard-coding fixture
paths.

**Examples:**

- `test_calibration_replay.py`: run `fit_pathloss()` on each fixture; assert
  P0 ∈ [-70, -45] dBm, n ∈ [1.8, 4.5], all 56 pair-residual cells present,
  every leaf has a per-leaf model.
- `test_tracks_replay.py`: stream obs in real timestamp order through
  `TrackStore.step()`; assert no NaN states, no covariance blow-ups,
  position σ over a 30 s stationary window < 1.5 m, entity clustering
  finds known multi-track entities.

## Tier C — regression baseline diff

**Location:** `tests/regression/`
**Runtime:** ~30 s per fixture
**Trigger:** every test invocation
**Catches:** the silent-degradation failure mode — code change makes
positions subtly worse but everything still "works" so no test fails.

The pattern: pick a fixture, run **current** code against it, run
**prior commit** code against it, diff the positions per (device,
timestamp), fail if any device's median position shifts by more than
30 cm. Implementation via Git worktrees so the working tree isn't
disturbed.

When a shift is intentional (you actually meant to change the math),
run `pytest tests/regression --accept-baseline` to commit the new
output as the new baseline in `tests/regression/baselines/`. Future
runs diff against the accepted baseline rather than against prior
commit, so multi-step changes don't drift undetected.

This is the **single most important tier** for math-change PRs. The
PR description template should include the regression diff output.

## Tier D — ground-truth accuracy (manual validation)

**Location:** `tests/manual/` + `docs/ground-truth-*.md`
**Runtime:** hours (you have to walk around)
**Trigger:** before release tags, after any Tier-1 change
**Catches:** absolute-accuracy regressions (different beast from
variance regressions — Tier B/C catch variance changes, Tier D catches
bias changes).

Tier B/C answer "is the system *consistent* across changes." Tier D
answers "is the system *correct*." A change that makes positions
consistently 2 m east of truth doesn't fail B or C. It only fails D.

**Procedure:**

- 10-20 marked physical points in the room (taped X's, photographed)
- Place a phone at each for 60 s; record the timestamp window
- After the run, query the dashboard / DB for reported position at each
  window; compute |reported - ground-truth| per point
- Output: a markdown report with per-point error, median, p95,
  histogram
- This is ticket **#25** — the eval suite. It produces Tier-D fixtures
  that become Tier-B fixtures.

## What we don't do

- **No GitHub Actions / CI.** Solo dev, single machine, `pytest`
  locally before `git push` is sufficient. Adding CI here would burn
  3-5 min per push for zero protection benefit. Revisit if a second
  contributor joins.
- **No coverage threshold gates.** `pytest-cov` for visibility is
  fine; ratcheting a number is theatre. Coverage is a useful signal,
  not a target.
- **No mocking of internal modules.** Use the real
  `mockingbird_calibration` / `mockingbird_tracks`. They're pure
  Python, no IO; mocks would only hide bugs.
- **No synthetic-only tests where a real-data version is possible.**
  If a fixture exists that exercises the same code path, prefer it.
  Synthetic for math primitives only.
- **No async test runners.** `pytest` straight; `pytest-xdist` for
  parallel if the suite ever grows past 30 s total.
- **No flaky-test retries.** A test is either deterministic or it's
  broken. Monte Carlo with a fixed seed is deterministic. Anything
  else gets debugged, not retried.

## Why this shape (the choices)

- **M4 Max means runtime is cheap.** Every per-test budget goes to
  thoroughness (more Monte Carlo trials, more hypothesis examples,
  more replay fixtures), not to "fast CI."
- **Solo dev means no merge conflicts.** No CI gate; just discipline.
  `make test` before push.
- **Hand-rolled numerical code means many small unit tests.** Each
  matrix primitive is a potential silent failure. Lots of focused
  Tier-A tests beat a few big Tier-B integration tests for catching
  *which* primitive broke.
- **RF/data-heavy domain means real-data > synthetic.** Tier B is the
  weight-bearing tier. Tier A guards primitives; Tier B guards
  pipelines.
- **Math changes are the dominant risk.** 12+ of the open tickets
  touch math directly. Tier C is what stops silent-accuracy
  degradation across those PRs.

## Tooling

```toml
# pyproject.toml (relevant subset)
[tool.pytest.ini_options]
testpaths = ["tests"]
python_files = "test_*.py"
addopts = "-q --strict-markers"
markers = [
    "slow: takes longer than 5 seconds",
    "replay: requires a fixture DB in ~/repos/.scratch/fixtures/",
    "regression: requires prior-commit comparison via Git worktree",
]

[tool.uv]
dev-dependencies = [
    "pytest>=8",
    "pytest-xdist>=3",
    "hypothesis>=6",
]
```

Optional: `pytest-cov` for ad-hoc visibility only (no gate).

## Running tests

```bash
make test          # everything
make test-unit     # just Tier A (< 1 s)
make test-replay   # Tier B (needs fixtures synced)
make test-regression  # Tier C (slow-ish)

# or directly:
pytest tests/unit -n auto       # parallel
pytest tests/replay
pytest tests/regression
pytest tests/regression --accept-baseline   # bless intentional shifts
```

## When you change math

1. `pytest tests/unit` — primitives intact?
2. `pytest tests/replay` — pipelines still converge correctly?
3. `pytest tests/regression` — any positions shift unexpectedly?
4. If positions shift:
   - **Intended?** Run with `--accept-baseline`. Commit the new baseline JSON alongside the code change with a one-line note in the commit message explaining the shift.
   - **Unintended?** Bug in your change. Debug it.
5. If you're closing a math-change ticket, **add a new test that would have failed pre-fix.** This is the single discipline that compounds: each fix permanently raises the testing floor.

## Test data lifecycle

- Fixtures: `~/repos/.scratch/fixtures/obs-YYYYMMDD-HHMM-<tag>.sqlite`. Never committed (gitignored, private data).
- Manifest: `tests/fixtures/manifest.yaml`. Committed. The contract between code and fixtures.
- Baselines: `tests/regression/baselines/<fixture>.json`. Committed. Updated only via `--accept-baseline`.
- Ground-truth runs: `tests/manual/<event-name>/` (committed metadata) + entries in `docs/ground-truth-*.md`.

## Open questions

- Do we eventually want a small `tests/firmware/` directory with
  PlatformIO Unity tests for the `is_mock` priority logic, JSON line
  serialization, MOCK self-detection? Probably yes when the firmware
  layer gets non-trivial. Today it's small enough that real-device
  testing on a flashed leaf is fine.
- Property tests via `hypothesis` are an unexplored win for the math
  layer (e.g. "every PSD covariance matrix in, every PSD covariance
  matrix out"). File-and-iterate after Tier A lands.

## Tickets

- **#29** — Tier-A unit-test scaffolding (math primitives). Blocking dependency for math-change PRs.
- **#30** — Tier-B replay + Tier-C regression baseline infrastructure. Blocking dependency for math-change PRs.
- **#25** — Tier-D ground-truth accuracy eval. Run-on-release cadence.
- **#31** — Move dashboard + math compute to M4. Enables faster Tier-B/C iteration because the math runs locally on a fast box.
