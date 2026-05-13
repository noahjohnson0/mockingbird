# Loose ends

**Maintainer:** Priya Kapoor (Chief of Staff)
**Date:** 2026-05-13

Things that were started, things that were promised, things that are half-done. Cross-referenced from CLAUDE.md's "Next moves on deck" section and the recent `git log --all` history.

Owners are *suggested* — please push back if I've assigned to the wrong person.

---

## From CLAUDE.md "Next moves on deck"

### LE-1: Rewrite or retire `scripts/bootstrap-glinet-router.sh`
- **Status:** Half-done. CLAUDE.md says the old script was deleted as "wrong architecture". A replacement (`scripts/bootstrap-pi-tailscale.sh`) was named as a follow-up but does not exist in the repo. We do have `scripts/bootstrap-pi-subnet-router.sh`, which may already cover this — needs confirmation.
- **Suggested owner:** Macca
- **Action:** Either declare `bootstrap-pi-subnet-router.sh` the canonical bootstrap and update CLAUDE.md to say so, or actually write `bootstrap-pi-tailscale.sh`. Don't leave both names floating.

### LE-2: Reflash the rogue ESP32 at `192.168.0.172` onto Mockingbird
- **Status:** Started — board was identified months ago (MAC `58:e6:c5:6f:4a:dc`, running old `esp32_demo` servo firmware). Never reflashed.
- **Suggested owner:** Vlad (or Noah, if it's physically with him)
- **Action:** Ping the IP. If alive, reflash with current `firmware/esp32-wroom-mockingbird/` build and add it to the fleet. If dead, remove the line from CLAUDE.md.

### LE-3: Tailnet-peer reachability test
- **Status:** Promised in CLAUDE.md item 4 of "Next moves": "Confirm reachability of the Pi from another tailnet peer via its new 192.168.8.x address." No evidence this was verified end-to-end from a non-Mac peer (phone, Windows server).
- **Suggested owner:** Macca
- **Action:** Ping `192.168.8.202` from a tailnet peer that is NOT on the Mockingbird LAN. Document the result with a date. Add to CLAUDE.md.

### LE-4: ESP32-S3 firmware decision deferred indefinitely
- **Status:** The `main/` tree is an unbuilt ESP-IDF/S3 firmware design. CLAUDE.md item 5 of "Next moves" asks us to decide whether to keep it or refactor for LAN-only on WROOM-32. The PlatformIO path won, but `main/` is still sitting there.
- **Suggested owner:** Anthony (architectural call) + Vlad (execution if we keep it)
- **Action:** Either delete `main/` (and the `CMakeLists.txt` / `partitions.csv` / `sdkconfig.*` at repo root that go with it), or add a README explaining why it's parked. Right now a new contributor sees two firmware trees and gets confused — see staleness audit item 6.

---

## From recent activity / review branches

### LE-5: Andy's test backlog (`ANDY-1`)
- **Status:** Half-done. Andy landed 22 starter tests on `andy/test-audit` covering the highest-leverage P0 gaps. His coverage audit lists **20 more tests** (10 P0, 8 P1, 2 P2) that haven't been written.
- **Suggested owner:** Andy, with Sophie tracking through the backlog
- **Action:** Get `andy/test-audit` merged to main. Then triage the remaining 20 tests into Sprint commitments.

### LE-6: Ethan's perf + correctness review of collector + tracks stack (`ETH-1`)
- **Status:** Branch `eth/perf-review` exists; PR/landing status unknown from this worktree.
- **Suggested owner:** Ethan, with Sophie chasing the merge
- **Action:** Read the review, file any follow-up tickets it spawned, then merge.

### LE-7: Puru's RF calibration model review (`PURU-1`)
- **Status:** Branch `puru/rf-review` exists. Same as above — review done, follow-ups unclear.
- **Suggested owner:** Puru, with Sophie chasing the merge
- **Action:** Same as LE-6.

### LE-8: Vlad's physics review (`vlad/physics-review`)
- **Status:** Commit `c5d82ed` lands "docs(physics): propagation review — RSSI from first principles + CRLB floor". Branch hasn't merged to main as of `b476a48`.
- **Suggested owner:** Vlad
- **Action:** Merge to main. Cross-link from the roadmap so future RF work cites it.

### LE-9: Anthony's Q3 technical vision (`ANT-1`)
- **Status:** Commit `8aa3286` ("ANT-1: 2026 Q3 technical vision") on branch `ant/q3-vision`. Not merged.
- **Suggested owner:** Anthony
- **Action:** Sophie should pull this into the planning cycle. If the vision is final, it should be on main where everyone reads it, not buried on a branch.

### LE-10: Sophie's Q3 PRDs (`soph/q3-prds`)
- **Status:** Commit `72a3a73` ("Q3 PRDs: person fingerprinting, IMU on leaves, presence alerts"). Three PRDs, all unmerged.
- **Suggested owner:** Sophie
- **Action:** Merge. Then assign engineers to each PRD. The IMU PRD in particular has a hardware dependency (MPU6050 wiring) that needs to land in my procurement queue — flagged separately as LE-12 below.

### LE-11: Test pyramid documentation (`docs/test-pyramid` branch)
- **Status:** Commit `91ac54e` ("docs/testing.md: capture test pyramid, methodology, and choices"). Has a remote tracking branch (`origin/docs/test-pyramid`) so this is closer to landing than the others.
- **Suggested owner:** Andy
- **Action:** Merge. Reference from CLAUDE.md (see staleness audit item 17).

### LE-12: IMU procurement for Q3
- **Status:** Implied by Sophie's PRDs and CLAUDE.md "Future capabilities" — MPU6050 sensors at ~$2 each, wired via I²C to each WROOM-32. Nothing ordered yet, no vendor selected.
- **Suggested owner:** Priya (me!)
- **Action:** I'll spec a part (MPU6050 vs. MPU6500 vs. ICM-20948 — different price/performance tiers), confirm I²C pin availability on the WROOM-32 with Vlad, and order 10 + spares once the IMU PRD is approved.

---

## Roadmap status drift

### LE-13: Person fingerprinting phase status in `docs/roadmap.md`
- **Status:** Roadmap preamble explicitly says "Move items to 'in flight' when started, to 'shipped' once landed in main, and to 'deferred' if explicitly chosen not to pursue." Phases 1–3 of person fingerprinting are partially shipped (commits `ac1fad0`, `b0342bf`, etc.) but still listed under "Planned."
- **Suggested owner:** Sophie
- **Action:** Do a roadmap status pass. Move shipped phases to shipped.

---

## Summary by owner

| Owner | Loose ends |
|---|---|
| Sophie | LE-1 (track), LE-5 (track), LE-6 (chase), LE-7 (chase), LE-10, LE-13 |
| Macca | LE-1, LE-3 |
| Vlad | LE-2 (or Noah), LE-4 (exec), LE-8 |
| Anthony | LE-4 (decide), LE-9 |
| Andy | LE-5, LE-11 |
| Ethan | LE-6 |
| Puru | LE-7 |
| Priya | LE-12 |
| Noah | LE-2 (if physical) |

**13 loose ends total.** Most cluster around "branches reviewed, not merged" (LE-5 through LE-11) — that's a single planning meeting away from being unstuck. Sophie, want me to send the invite?
