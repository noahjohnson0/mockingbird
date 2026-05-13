# mockingbird — Test Coverage Audit (ANDY-1)

**Auditor:** Andy (SDET)
**Branch:** `andy/test-audit`
**Date:** 2026-05-13
**Scope:** services/ (Pi-side Python), scripts/ (analysis tooling), firmware/esp32-wroom-mockingbird/ (testable C++ surface)

---

## TL;DR

**There are zero tests in this repository.** ~4800 lines of production Python
(collector, calibration math, tracks/Kalman fusion, dashboard) plus ~500
lines of firmware ship with no automated verification of any kind. The
position-fusion math has been iterated on aggressively (the last 5 commits
are all centroid/MLE/Kalman tuning) with manual eyeball validation against
the live dashboard as the only signal. **This is the highest-risk part of
the codebase** — silent regressions in `multilaterate`, `mle_multilaterate`,
or `kalman_step` will produce wrong positions that look plausible, and
nothing will flag the drift.

Three starter tests landed in this branch cover the worst gaps; see
`tests/` and section 5.

---

## 1. Current coverage

### Test files in repo
```
$ find . -name "test_*.py" -o -name "*_test.py" -o -name "tests" -type d
(none — before this branch)
```

There is no `pytest.ini`, `pyproject.toml`, `conftest.py`, CI workflow, or
any other indication that automated testing has ever been set up. The
`scripts/analyze_ble_*.py` files are exploratory CLIs, not tests — they
print summaries for a human to read.

### Quality of existing tests
N/A — there are none. Quality assessment in section 4 applies to the
starter tests this branch adds, plus inline "test-like" patterns
discovered in the production code:

- `_kf_state_looks_corrupt` in `mockingbird_tracks.py` is a defensive
  guard that doubles as an assertion-of-invariants. It will be **directly
  testable** once factored — its current call site is the Kalman update
  path.
- `_clamp_pos_cov` enforces a covariance-matrix invariant (PSD-ish,
  bounded). Pure function, trivial to test, currently untested.
- `_solve_2x2`, `_solve_3x3`, `_invert_3x3` are pure linear-algebra
  primitives. Untested. These are exactly the kind of functions where
  one transposed index produces wrong-but-plausible outputs.

---

## 2. Critical uncovered paths (regression-risk ranking)

### Collector (`services/mockingbird-collector.py`)
| Path | What could silently break | Severity |
|---|---|---|
| `WriteBuffer.flush` ↔ `_write` atomic INSERTs | A logic bug in the BEGIN/COMMIT ordering or a slot reordering in `OBS_SQL` writes rows with `rssi` in the `addr_type` column. Nothing flags it; analysis just gets garbage. | **P0** |
| `handle_client` parsing of `obs` / `hello` / `hb` events | Off-by-one in tuple build, missing `leaf` guard, or accepting `obs` before `hello` writes a row with empty leaf. Indices are still satisfied, queries silently return wrong attribution. | **P0** |
| `MAX_LINE` drop path + JSON decode-error path | A malformed line currently increments `drop` and continues. If a future refactor breaks readline framing, a single bad byte could wedge the connection. No test asserts the drop-and-continue contract. | **P1** |
| TCP keepalive setsockopt block | macOS dev / older kernels swallow `OSError, AttributeError`. Easy to break when refactoring the import. | **P2** |
| `idle timeout` (45s) closure | A drop-into-finally that re-enqueues a phantom disconnect event. | **P1** |
| `_checkpoint_via_new_conn` under writer-lock contention | The whole point of the new connection is to avoid cross-thread sqlite3 issues. A future "let's reuse the existing handle for speed" change reintroduces the bug that motivated this code. | **P1** |

### Calibration (`services/mockingbird_calibration.py`)
| Path | What could silently break | Severity |
|---|---|---|
| `_solve_3x3` / `_invert_3x3` | A typo in cofactor sign produces a matrix that's still 3×3, still numerically stable on most inputs, and produces position estimates that are wrong by a constant rotation. The dashboard will look "mostly right." | **P0** |
| `estimate_distance` per-leaf bias subtraction | If `rx_bias` is applied with the wrong sign or to the wrong leaf, distances skew systematically and `mle_multilaterate` converges to a wrong fixed offset. Pre-`6703762` this was the bug; nothing prevents reintroducing it. | **P0** |
| `multilaterate` ≥4-hits gate | Reducing to ≥3 (the obvious "more data, why not?" refactor) silently produces unstable z estimates because 3 spheres in 3D have two intersections — z flips between solutions. | **P0** |
| `multilaterate` bounds rejection | If `bounds` is dropped, catastrophic LS failures return absurd coords (km away). Currently caller falls back to centroid; a refactor that "simplifies" the bounds-check away breaks this safety net. | **P1** |
| `_linear_fit` singular-X branch | `sxx < 1e-9` → returns `(None, None)`. If a future change forgets to handle the None return, `fit_pathloss` crashes on a constant-distance dataset (calibrated all-at-1m). | **P1** |
| `_interpolate_multipath` IDW collapse | Empty `pair_residuals` → returns 0.0. A bug returning NaN here propagates into `estimate_distance` → all distances NaN → all positions NaN. | **P1** |
| `_device_centroid` ≥2-hit gate + denominator | Division by zero if every leaf saw the device at exactly RSSI 0 (impossible in practice, but the code doesn't gate on it). | **P2** |

### Tracks / Fusion (`services/mockingbird_tracks.py`)
| Path | What could silently break | Severity |
|---|---|---|
| `kalman_step` re-initialization on corrupt state | Removing the `_kf_state_looks_corrupt` check lets a single bad MLE measurement push velocity to 1000 m/s; the predict step then exponentially blows up the position estimate within ~3 ticks. Dashboard shows a track flying through walls. | **P0** |
| `kalman_step` >5m teleport guard | Smoothing a 10m measurement jump averages the new position with an arbitrary old one and locks the track on a wrong point for ~10 ticks. | **P0** |
| `_smooth_alpha(jump_m)` mapping | The whole "jitter doesn't leak into velocity" tuning depends on this returning MAX for tiny jumps and MIN for big jumps. A `min/max` swap silently inverts the smoothing behavior. | **P0** |
| `_is_stationary` / `_is_stationary_tuples` ZUPT | Off-by-one in `radius_m²` vs `radius_m`, or `>` vs `>=`, changes the stationary threshold. Phantom motion regression. | **P1** |
| `_clamp_pos_cov` PSD enforcement | A bug here lets a non-PSD covariance into the Kalman update; `_invert_3x3_local` either rejects (returns None) or returns garbage. | **P1** |
| `codename_for` collision rate | The whole point of the 2-char suffix is sub-1% collision at ~50 tracks. A regression to the 512-combo scheme isn't a correctness bug but it's a UX bug ("I have three Ruby Owls"). | **P2** |

### Firmware (`firmware/esp32-wroom-mockingbird/src/main.cpp`)
The firmware is C++ on FreeRTOS; testing the live `uplink_task` requires a
host-side rig with Arduino mocks. Two testable seams exist now:

| Path | What could silently break | Severity |
|---|---|---|
| `snprintf` JSON line format for `obs`/`hello`/`hb` events | Add/rename a field on-device, forget to update the collector's `msg.get("name")` keying, and the field silently becomes None in the DB. **Contract test** between firmware string format and collector parser would catch this — see backlog. | **P0** |
| `deviceHostname()` formatting (`mockingbird-XXXXXX` from MAC bytes 3..5) | A change to upper/lowercase, byte order, or width breaks every dashboard query keyed on hostname. | **P1** |
| Short-write reconnect (`wrote != n` → `g_tcp.stop()`) | Untestable without host-side TCP mock; flag for future. | **P2** |

---

## 3. Prioritized test backlog

### P0 — could silently break production right now

1. **`services/mockingbird_calibration.py::multilaterate`** — `test_multilaterate_recovers_known_position_in_clean_geometry_within_30cm`. Given 4 leaves at known corners and a device at a known point, with RSSI synthesized from `RSSI = P0 - 10n·log10(d)` (no noise), `multilaterate` must return a position within 0.30 m. Why: catches transposed-index regressions in `_solve_3x3` and broken sign conventions in the linearization.
2. **`services/mockingbird_calibration.py::multilaterate`** — `test_multilaterate_returns_none_when_fewer_than_four_positioned_leaves`. Why: the ≥4 gate is the only thing keeping 3D z-ambiguity out of the dashboard.
3. **`services/mockingbird_calibration.py::estimate_distance`** — `test_estimate_distance_inverts_pathloss_model_at_reference_rssi`. At `rssi == p0`, distance must equal 1.0 m. At `rssi == p0 - 10n`, distance must equal 10.0 m. Why: catches a sign flip on the bias subtraction or n exponent.
4. **`services/mockingbird_calibration.py::_solve_3x3`** — `test_solve_3x3_recovers_solution_for_known_system` (parametrized over identity, diagonal, dense, and singular). Why: pure-Python linear solver, regression target #1.
5. **`services/mockingbird_calibration.py::_invert_3x3`** — `test_invert_3x3_times_original_is_identity_within_1e_9`. Why: same.
6. **`services/mockingbird_tracks.py::_smooth_alpha`** — `test_smooth_alpha_returns_max_for_tiny_jump_and_min_for_huge_jump_and_monotonic_between`. Why: the entire "stationary phones stay still" property depends on this monotonic mapping.
7. **`services/mockingbird_tracks.py::_kf_state_looks_corrupt`** — `test_kf_state_corrupt_flags_nonfinite_position_velocity_and_oversized_values`. Parametrize across the 4 corruption modes. Why: this guard is what keeps a single bad MLE from blowing up the filter exponentially.
8. **`services/mockingbird-collector.py::WriteBuffer.flush`** — `test_writebuffer_flush_persists_all_pending_obs_in_one_transaction` (against in-memory sqlite). Assert: after adding N obs rows, flush returns N and SELECT COUNT(*) FROM obs returns N. Why: the column-order bug is silent and catastrophic.
9. **`services/mockingbird-collector.py::handle_client`** — `test_handle_client_ignores_obs_before_hello` (asyncio loopback). Assert: a leaf that sends `{"event":"obs",...}` without first sending hello produces 0 rows in `obs`. Why: prevents anonymous/empty-leaf rows.
10. **Firmware/collector contract** — `test_collector_parses_firmware_obs_line_format`. Use a fixed example string copy-pasted from `firmware/.../main.cpp` `snprintf` format; feed it to the collector's parse path; assert all 7 fields land in the right columns. Why: catches both sides of the wire-format contract drifting independently.

### P1 — important but won't bite this week

11. **`services/mockingbird_calibration.py::_linear_fit`** — return `(None, None)` on degenerate input; finite a, b on well-conditioned input; matches numpy.polyfit to 1e-9 on a known regression.
12. **`services/mockingbird_calibration.py::_interpolate_multipath`** — empty residuals → 0.0; single-source → exact bias; IDW weight at the source position is finite (epsilon prevents singularity).
13. **`services/mockingbird_calibration.py::_device_centroid`** — returns None for <2 hits; centroid of symmetric layout is at center; reduces to `10^(rssi/20)` heuristic when p0/n are None.
14. **`services/mockingbird_tracks.py::_clamp_pos_cov`** — clamps too-small diagonal up to min_std²; too-large diagonal down to max_std²; symmetrizes off-diagonals; correlation magnitudes ≤ 0.95 after clamp.
15. **`services/mockingbird_tracks.py::_is_stationary`/_tuples** — fewer than `ZUPT_HISTORY_N` → False; tight cluster → True; one outlier point → False; boundary (exactly radius_m away) behavior pinned.
16. **`services/mockingbird-collector.py::open_db`** — schema creation idempotent; ALTER TABLE for `location` column survives re-open; indices created with correct names (`idx_obs_ts` etc.).
17. **`services/mockingbird-collector.py::handle_client`** — malformed JSON line increments drop counter, does not break the connection.
18. **`services/mockingbird-collector.py::handle_client`** — line longer than `MAX_LINE` is dropped silently and parsing continues on the next line.
19. **`services/mockingbird_tracks.py::kalman_step`** — first call initializes state to the measurement with zero velocity; second call after a small in-place jump produces a smoothed position closer to the previous position than the measurement; >5m jump triggers re-init.
20. **`services/mockingbird_calibration.py::mle_multilaterate`** — covariance matrix returned is symmetric and PSD; clean-geometry recovery within 30 cm; degrades gracefully (returns None or large cov) on co-linear leaf geometry.

### P2 — nice-to-have, mostly cosmetic / defensive

21. **`services/mockingbird_tracks.py::codename_for`** — deterministic for same input; ≤1% collision over 100 random UUIDs.
22. **`scripts/analyze_ble_db.py`** — at least one smoke test that the script imports and the SQL queries parse against an empty schema.
23. **Firmware `deviceHostname()` format** — once a host-side test harness exists, assert the `mockingbird-%02x%02x%02x` format with a fixed MAC produces `mockingbird-4ce184`.

---

## 4. Quality issues in existing tests

There are no existing tests, so there are no multi-assert tests to split,
no hidden fixtures, no smoke-tests-pretending-to-be-units. Future tests
should watch for these patterns:

- **Don't bundle "happy path + 3 edge cases" into one parametrized test
  body with mixed assertions.** Parametrize over inputs only when the
  assertion is the same; otherwise split.
- **Don't share a single `CalibrationParams` fixture across tests that
  mutate or rely on different fields.** Build minimal fixtures per test
  so the AAA preconditions are visible at the call site.
- **Don't use `time.sleep` to test async code.** Use `asyncio` timeouts
  and explicit `await asyncio.wait_for(...)`.
- **Don't assert on the real `~/mockingbird/observations.sqlite`.** All
  DB tests use `tmp_path` or `:memory:`. The collector's `DB_PATH` is a
  module-level constant — tests must monkeypatch it.
- **Don't assert "RSSI is monotonic with distance".** It isn't, even
  in clean line-of-sight. Test the model inversion (`estimate_distance`
  is the inverse of `P0 - 10n·log10(d)`), not the physics.

---

## 5. Top 3 tests to write this week

The three starter tests below are committed in this branch under
`tests/`. They target the highest-leverage P0 regressions:

1. **`tests/test_calibration_solvers.py`** — exercises `_solve_3x3` and
   `_invert_3x3` against known systems. These are the pure-linear-algebra
   primitives that every position estimate flows through. A typo here
   silently rotates or scales every result on the dashboard.
2. **`tests/test_calibration_pathloss.py`** — exercises `estimate_distance`
   and `multilaterate` on synthetic clean geometry. Pins the model-
   inversion invariants (rssi=P0 → d=1m; rssi=P0-10n → d=10m) and the
   ≥4-hit gate.
3. **`tests/test_tracks_smoothing.py`** — exercises `_smooth_alpha`,
   `_kf_state_looks_corrupt`, and `_clamp_pos_cov`. These are the
   "stationary phones don't drift" guards.

All three are pure-function tests. No async, no fixtures beyond
`pytest.tmp_path`, no network or DB I/O. They should pass on any host
with `python3 -m pip install pytest`.

### Running them

```bash
python3 -m pip install --user pytest
cd ~/repos/mockingbird
python3 -m pytest tests/ -v
```

If imports fail because `services/` isn't on `sys.path`, the test files
prepend it themselves (see top of each file). The hyphen in the
collector filename means `from mockingbird-collector import ...` is
invalid; that test is deferred until the collector module is renamed
to `mockingbird_collector.py` (recommended — see backlog #16).

### Next batch (next sprint)

- The 4 collector tests (P0 #8, #9, #10; P1 #16-#18). These require
  renaming `services/mockingbird-collector.py` → `mockingbird_collector.py`
  so it's importable. That's a 1-line change + the systemd unit file +
  `scripts/deploy-collector.sh`.
- The `mle_multilaterate` covariance-PSD test (P1 #20). The math is
  substantial but the test is short.
- A firmware-format contract test (P0 #10). One static string
  representing a real `obs` line from the firmware, fed through the
  collector's parser logic (which must first be factored out of
  `handle_client` into a `parse_line(line) -> Optional[Msg]` function).

---

## Recommendations / blockers

- **Block-on-merge:** any change to `multilaterate`, `mle_multilaterate`,
  `kalman_step`, `_smooth_alpha`, or `_kf_state_looks_corrupt` without
  a corresponding test should not land. These functions are too easy
  to break in plausible-looking ways.
- **Refactor recommended:** rename `services/mockingbird-collector.py`
  → `mockingbird_collector.py`. Hyphenated module names are not
  importable and block testing the entire collector. One-line change.
- **Refactor recommended:** extract `parse_obs_line(bytes) -> Optional[Msg]`
  from `handle_client`. Right now the JSON parsing, validation, tuple-
  building, and DB-write enqueue are all interleaved — untestable
  without an asyncio rig. Pull the parser into a pure function and
  network-test the dispatch separately.
- **Quality stance:** the math in `mockingbird_calibration.py` and
  `mockingbird_tracks.py` is the differentiator of this project. It
  *must* have unit tests before the next round of physics tuning.
  Manual eyeball validation on a live dashboard cannot catch a 30 cm
  systematic offset, and 30 cm is the difference between "in the
  kitchen" and "at the kitchen sink."
