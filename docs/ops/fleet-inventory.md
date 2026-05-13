# ESP32 leaf fleet inventory

**Maintainer:** Priya Kapoor (Chief of Staff)
**Source of truth:** `CLAUDE.md` "Current state (as of last commit)" section
**Last reconciled:** 2026-05-13

This is the operational view of the leaf fleet — one row per board, with the details I need to keep track of for parts, RMA, OTA pushes, and "wait, which one is in the closet?" questions.

## Deployed leaves (8 total)

| # | Hostname | MAC | Last-seen IP | Flashed | Batch | Firmware | Notes |
|---|---|---|---|---|---|---|---|
| 1 | `mockingbird-4ce184` | `8c:94:df:4c:e1:84` | `192.168.8.244` | pre-2026-05-11 | AITRIP | v0.3.1-stream | First unit identified by MAC; baseline reference board |
| 2 | `mockingbird-4c0bdc` | `8c:94:df:4c:0b:dc` | `192.168.8.196` | pre-2026-05-11 | AITRIP | v0.3.1-stream | — |
| 3 | `mockingbird-4c36ec` | `8c:94:df:4c:36:ec` | `192.168.8.219` | pre-2026-05-11 | AITRIP | v0.3.1-stream | — |
| 4 | `mockingbird-4db204` | `8c:94:df:4d:b2:04` | `192.168.8.168` | pre-2026-05-11 | AITRIP | v0.3.1-stream | — |
| 5 | `mockingbird-4ce1d8` | `8c:94:df:4c:e1:d8` | `192.168.8.178` | 2026-05-11 | AITRIP | v0.3.1-stream | — |
| 6 | `mockingbird-4ceb7c` | `8c:94:df:4c:eb:7c` | `192.168.8.126` | 2026-05-11 | AITRIP | v0.3.1-stream | — |
| 7 | `mockingbird-92838c` | `30:76:f5:92:83:8c` | `192.168.8.204` | 2026-05-11 | **Espressif** (different batch) | v0.3.1-stream | OUI is Espressif, not AITRIP. Watch this one for behavioural differences; only non-AITRIP unit in service |
| 8 | `mockingbird-4d4384` | `8c:94:df:4d:43:84` | `192.168.8.107` | 2026-05-11 | AITRIP | v0.3.1-stream | — |

All eight stream BLE observations as newline-delimited JSON over TCP to `mockingbird-pi:9001`. Per CLAUDE.md: heap stays at 110–125 KB free under continuous heavy scanning, zero drops, zero crashes in the latest 60-second 4-way test.

## Out-of-mesh ESP32s

| Hostname | MAC | IP | State | Notes |
|---|---|---|---|---|
| _(unknown — pre-mockingbird fw)_ | `58:e6:c5:6f:4a:dc` | `192.168.0.172` | On entropy network, **not** on Mockingbird | Has old custom servo firmware from `~/repos/esp32_demo`. Needs reflash to join the mesh. Possibly a 9th board; possibly one of the "unflashed AITRIP" inventory below — needs Noah to physically confirm. |

## Unflashed inventory

CLAUDE.md says "~2 unflashed AITRIP boards remain in the pack". The arithmetic doesn't quite check out: pack started at 10 AITRIP units; 7 AITRIP are deployed (8 total deployed minus 1 Espressif); so **3 AITRIP boards should remain unflashed**, not 2 — *unless* the entropy unit at `192.168.0.172` is one of the original 10, in which case 2 is correct.

**Suggested action:** Noah does a physical count next time he's at his desk. Flagged in [claude-md-staleness-audit.md](claude-md-staleness-audit.md) item 10.

## Fleet totals

- **Deployed and streaming:** 8
- **AITRIP boards deployed:** 7
- **Espressif boards deployed:** 1
- **Boards out-of-mesh (pre-mockingbird fw):** 1
- **Unflashed boards (stated):** ~2 (probably 3 — see above)
- **Total ESP32-WROOM-32 units accounted for:** 10–11

## Hardware spec (applies to all leaves)

- Chip: ESP32-D0WD-V3 (rev 3.1), plain dual-core ESP32, no -S3
- Flash: 4 MB
- PSRAM: none (WROOM-32, not WROVER)
- USB bridge: Silicon Labs CP2102 (`0x10c4:0xea60`)
- Mac device nodes: `/dev/cu.SLAB_USBtoUART` and `/dev/cu.usbserial-0001`

## Known operational risks

1. **Collector IP `192.168.8.202` is hardcoded in firmware** (`platformio.ini`: `MOCKINGBIRD_COLLECTOR_HOST='"192.168.8.202"'`). If DHCP renumbers the Pi, the entire fleet goes silent until reflashed. Mitigation: get Macca to pin a DHCP reservation, or migrate the firmware to use the mDNS hostname `mockingbird-pi.local`.
2. **One non-AITRIP unit in the mesh** (`mockingbird-92838c`) — should monitor whether its behaviour drifts from the AITRIP cohort. Useful as an A/B sample, but also a confound if it misbehaves.
3. **Mixed flash dates** — boards 1–4 are pre-2026-05-11, boards 5–8 are 2026-05-11. If we ever ship a fleet-wide firmware bump, do a roll-forward sweep so they all run the same image.
4. **OTA push path** is `pio run -e ota -t upload --upload-port mockingbird-<chipid>.local`. Requires the pushing machine to be on Mockingbird or reachable via the Tailscale subnet route. Document this in any runbook before someone tries to push from the wrong network.
