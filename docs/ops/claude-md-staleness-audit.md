# CLAUDE.md staleness audit

**Auditor:** Priya Kapoor (Chief of Staff)
**Date:** 2026-05-13
**Method:** Read CLAUDE.md line by line. Compared against actual repo state on `priya/ops-audit` (forked from the current worktree tip `b476a48`) and cross-checked against sibling branches (`andy/test-audit`, `eth/perf-review`, `puru/rf-review`, `ant/q3-vision`, `soph/q3-prds`, `docs/test-pyramid`) plus `git log --all`.

I am not fixing anything here — just flagging. Each item is tagged with a suggested verifier. Some "stale" items are actually "ahead of CLAUDE.md" (work has landed but the doc didn't get updated), which is a different flavour of stale but still needs reconciling.

---

## Suspected stale or contradicted facts

### 1. Firmware version baseline says `v0.3.1-stream`, but a whole new firmware capability ("Tier-1 firmware: leaves advertise BLE → ground-truth inter-leaf calibration matrix") has landed since
- **CLAUDE.md says:** ESP32 leaves are running `v0.3.1-stream` firmware (line ~287, ~308).
- **Repo says:** commit `7992962` ("Tier-1 firmware: leaves advertise BLE → ground-truth inter-leaf calibration matrix") and `8e8a451` ("Firmware A/B: parameterize obs-queue depth + per-leaf flash workflow") both touch firmware. The current `platformio.ini` still pins `MOCKINGBIRD_FW_VERSION='"0.3.1-stream"'` on this worktree, but the on-device fleet may now be running newer flashed builds.
- **Verifier:** Vlad / Ethan — confirm actual flashed version on the fleet and bump the doc.

### 2. "First planned capability: distributed BLE sensing" is way past tense
- **CLAUDE.md says:** "First planned capability: distributed BLE sensing" (line ~24).
- **Repo says:** BLE sensing is shipped. We're well into multilateration, Kalman fusion, entity clustering, person fingerprinting, auto-calibration, live dashboard, motion detection, and "watch-noah" tracking. See commits `1a873d0`, `a1a72b0`, `ac1fad0`, `5f7c3f1`, `6703762`.
- **Verifier:** Anthony / Sophie — needs a "Current capabilities" rewrite, not a tweak.

### 3. "Next moves on deck" list is almost entirely done
- **CLAUDE.md says:** Items 1, 2, 4, 5, 6 of "Next moves" describe flashing the first ESP32, building distributed BLE sensing, deciding ESP32 firmware approach, flashing a proof-of-concept.
- **Repo says:** All done. Eight leaves are flashed and streaming. Firmware is PlatformIO/Arduino with NimBLE-Arduino (confirmed in `platformio.ini`). Item 3 (rewrite `bootstrap-glinet-router.sh` / write `bootstrap-pi-tailscale.sh`) is still open. Item 4 about reflashing `192.168.0.172` is unverifiable — see #9 below.
- **Verifier:** Sophie — rewrite this whole section into a real backlog.

### 4. "Future capabilities" list is partially shipped
- **CLAUDE.md says (line ~342):** "Real trilateration", "Live TUI / web dashboard" listed as *future*.
- **Repo says:** Trilateration via MLE multilateration (`1a873d0`, `d54ac85`) and the Three.js dashboard at `:8080` (`f62b7d7` and many follow-ups) are both shipped.
- **Verifier:** Anthony — move these from "future" to "shipped" in `docs/roadmap.md` (which is the authoritative list per CLAUDE.md's own pointer).

### 5. Services directory inventory in CLAUDE.md is silent on calibration + tracks modules
- **CLAUDE.md says:** Mentions the Pi collector service (`mockingbird-collector.service`) and dashboard.
- **Repo says:** On branches `andy/test-audit`, `eth/perf-review`, `puru/rf-review` there are two additional Python modules: `services/mockingbird_calibration.py` and `services/mockingbird_tracks.py`. These do not exist on this worktree's tip but are real and reviewed (Ethan's perf review, Puru's RF calibration review are dedicated to them).
- **Verifier:** Ethan — add these modules to the CLAUDE.md services section once the relevant branches merge to main.

### 6. The repo has TWO firmware trees and CLAUDE.md is muddy about which is live
- **CLAUDE.md says (line ~177):** "ESP32 firmware in `main/`: never been built (ESP-IDF not installed, hardware target is S3 which Noah doesn't own yet)."
- **Repo says:** Correct — `main/` is the unbuilt ESP-IDF/S3 tree. The *live* firmware is in `firmware/esp32-wroom-mockingbird/` (PlatformIO/Arduino). CLAUDE.md never names this directory explicitly. A new reader would not know where to look.
- **Verifier:** Vlad — add a one-liner naming `firmware/esp32-wroom-mockingbird/` as the live tree.

### 7. Pi IP `192.168.8.202` is described as authoritative but flagged as DHCP
- **CLAUDE.md says:** Pi at `192.168.8.202/24 (DHCP — may renumber)`. Used in `platformio.ini` as `MOCKINGBIRD_COLLECTOR_HOST='"192.168.8.202"'` and in CLAUDE.md ~10 times.
- **Repo says:** This is a working configuration but every leaf has the IP baked into firmware. If DHCP renumbers the Pi, the entire fleet goes dark until reflashed.
- **Verifier:** Macca — either reserve a DHCP lease on the Opal for the Pi's MAC, or move leaves to using the mDNS hostname `mockingbird-pi.local`. Then update CLAUDE.md to say "static reservation" not "DHCP — may renumber".

### 8. SD-card-backup note may be obsolete
- **CLAUDE.md says (line ~167):** "16 GB SD card from the original Bullseye-era install is set aside as backup".
- **Repo says:** Unverifiable from the repo. This is a physical-shelf state question.
- **Verifier:** Noah — does the 16 GB card still exist and is it actually backup-worthy now that the 64 GB has months of state on it?

### 9. ESP32 at `192.168.0.172` on the entropy network
- **CLAUDE.md says (line ~62, ~339):** "One unit on entropy at `192.168.0.172` (MAC `58:e6:c5:6f:4a:dc`) with old pre-mockingbird firmware — needs reflash to join the mesh."
- **Repo says:** No way to verify from the repo. Eight leaves are deployed; up to two AITRIP boards are unflashed per the inventory math. Either this rogue unit is one of those, or it's a separate ninth board still pinned to entropy.
- **Verifier:** Noah / Vlad — ping `192.168.0.172`. If it responds, reflash it. If it doesn't, delete this line from CLAUDE.md.

### 10. "~2 unflashed AITRIP boards remain" — arithmetic is shaky
- **CLAUDE.md says:** "started with 10; 8 deployed; 1 of the deployed 8 is the non-AITRIP Espressif unit". That gives 10 AITRIP - 7 deployed AITRIP = 3 AITRIP remaining, plus 0 Espressif remaining. So it's ~3, not ~2.
- **Verifier:** Noah — physically count the unflashed boards on the desk.

### 11. Tailscale IPs presented without an "as of" date
- **CLAUDE.md says:** `mockingbird-pi` at `100.83.26.55` / `fd7a:115c:a1e0::5838:1a37`.
- **Repo says:** Stable enough in practice, but tailnet IPs change if you delete + re-add a node. No "verified on YYYY-MM-DD" stamp anywhere.
- **Verifier:** Macca — add a "last verified" date to the network topology block, even if the value is unchanged.

### 12. Credentials file `tailscale-authkey` flagged "empty/unused"
- **CLAUDE.md says (line ~190):** "`tailscale-authkey` (0600) | (empty/unused — we authed the Pi via interactive URL instead)"
- **Repo says:** Probably stale — if it's empty and unused, it should be deleted, not memorialised. At minimum CLAUDE.md should not document a dead-end file.
- **Verifier:** Noah — delete the file, delete the line. Or repurpose the file and update the line.

### 13. `scripts/bootstrap-glinet-router.sh` deletion note is now ancient history
- **CLAUDE.md says (line ~331):** "Earlier `scripts/bootstrap-glinet-router.sh` is **deleted** (was wrong architecture)."
- **Repo says:** Fine, but this is the kind of note that ages badly. The deletion is settled fact; we don't need to memorialise it forever. A reader six months from now will wonder why this is in the project memory at all.
- **Verifier:** Sophie — prune at the next CLAUDE.md cleanup pass.

### 14. `docs/roadmap.md` is named in CLAUDE.md as the source of truth but is also stale
- **CLAUDE.md says (line ~341):** "Future capabilities (full list in `docs/roadmap.md`)".
- **Repo says:** `docs/roadmap.md` is real (`7eb8a7a` adds the person-fingerprinting plan), but I noticed Phases 1–3 of person fingerprinting are partially shipped (`ac1fad0` Entity clustering: merge co-located tracks into person-level identities; auto-detect phone via 4-leaf walk in `b0342bf`). Phase status hasn't been moved from "Planned" to "in flight" or "shipped" as the roadmap's own preamble instructs.
- **Verifier:** Sophie — drive a roadmap status-pass with whoever owns each phase.

### 15. "Pi runs Tailscale" persists despite branch activity
- **CLAUDE.md says:** Pi is the subnet router. Confirmed live with `100.83.26.55`.
- **Repo says:** Consistent. No contradiction. Logging here as **verified** so the next auditor doesn't waste cycles re-checking it.

### 16. CLAUDE.md never mentions the `tools/ota_serve.py` helper
- **CLAUDE.md says:** Nothing.
- **Repo says:** `tools/ota_serve.py` exists and is presumably part of the OTA workflow.
- **Verifier:** Vlad — either document or remove.

### 17. CLAUDE.md never mentions `tests/`
- **CLAUDE.md says:** Nothing about testing strategy.
- **Repo says:** Andy's branch `andy/test-audit` lands `tests/conftest.py` + three real test files plus `docs/test/coverage-audit.md`. The `docs/test-pyramid` branch lands `docs/testing.md`. Once these merge, CLAUDE.md needs a one-paragraph "How we test" section.
- **Verifier:** Andy — add a small testing section after his audit lands on main.

---

## Suspected verified-still-true (recorded so we don't re-check)

- Hardware inventory (ESP32 chip family, GL-SFT1200 SoC = SiFlower little-endian, Pi Zero W rev 1.1 armhf) — physically grounded, low risk of bit-rot.
- "Gotchas learned" section — historical lessons, none of which expire.
- Credentials file paths in `~/repos/.scratch/` — verifier would need Noah's machine; cannot audit from the repo.
- Network topology ASCII diagram — matches CLAUDE.md's other claims.

---

## Bottom line

**17 items flagged.** Of those:
- 5 are "doc is behind shipped work" (items 2, 3, 4, 5, 14)
- 6 are "doc has an unverifiable physical-world claim that needs an eyeball" (items 1, 7, 8, 9, 10, 11)
- 4 are "doc has a small inaccuracy or muddiness" (items 6, 12, 13, 16)
- 1 is a doc gap (item 17 — testing)
- 1 is a verified-still-true note (item 15)

I would not say CLAUDE.md is *broken* — it's lived-in and largely true. It's just been outpaced by an extremely productive quarter and needs a 30-minute reconciliation pass, ideally driven by Sophie with the engineers each confirming their own area.
