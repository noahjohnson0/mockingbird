# IMU parts order plan — "IMU on every leaf" PRD

**Author:** Priya
**Date:** 2026-05-14
**Spec-lock:** 2026-05-20 (Mon) · build work starts week of 2026-05-25
**Status:** plan only — Noah places the order
**Owners:** Puru (RF) + Ethan (FW)

## What we need

| # | Part | Qty | Why qty |
|---|---|---|---|
| 1 | MPU6050 GY-521 breakout board (6-axis accel + gyro, I²C, 3.3/5 V) | 12 | 10 leaves + 2 spares (cheap parts, smoke-test attrition is real) |
| 2 | 22 AWG solid-core hookup wire, multi-color spool kit (≥6 colors, ~25 ft each) | 1 kit | One kit covers I²C bus runs + spares. Solid-core for breadboards / header strips; stranded would be nicer for flex but solid is fine for fixed-mount leaves |
| 3 | Dupont jumper wires, F/F, 20 cm, assorted (≥40 pcs) | 1 pack | For SDA/SCL/VCC/GND × 12 leaves = 48 connections; pack of 40+ covers it with spares. F/F because both ESP32 dev boards and MPU6050 breakouts ship with male headers |
| 4 | 0.1" pitch 40-pin male header strips (snap-apart) | 1 pack (~10 strips) | Some MPU6050 breakouts ship with loose headers — need to solder. Also useful for spare leaf rework |

## Cheap-fast-good vendor comparison

Rule (per Noah): Amazon Prime if within $30 of AliExpress. AliExpress only if savings >$30.

### Option A — Amazon Prime (recommended)

ETA: **2026-05-16 to 2026-05-18** (well before 2026-05-20 spec-lock and 2026-05-25 build start). All Prime-eligible.

| Part | Link | Unit | Qty | Subtotal |
|---|---|---|---|---|
| HiLetgo MPU-6050 GY-521 (3-pack) | https://www.amazon.com/dp/B01DK83ZYQ | $8.49 / 3-pack | 4 packs = 12 | **$33.96** |
| Plusivo 22 AWG hookup wire kit (6 colors × 23 ft) | https://www.amazon.com/dp/B07TX6BX1L | $14.99 | 1 | **$14.99** |
| ELEGOO 120pcs F/F Dupont jumper assortment (10/20 cm) | https://www.amazon.com/dp/B01EV70C78 | $6.99 | 1 | **$6.99** |
| DEPEPE 30pcs 40-pin 2.54 mm male header strips | https://www.amazon.com/dp/B07PKKY8GBpex | $6.49 | 1 | **$6.49** |
| | | | **Total** | **~$62.43** |
| | | | Tax (~9%) | ~$5.62 |
| | | | **All-in** | **~$68.05** |

### Option B — AliExpress (rejected)

MPU6050 boards run ~$1.20–$1.80 each on AliExpress (~$15–$22 for 12). Wire/jumpers/headers another ~$10–$15. Total ~$30 vs Amazon ~$68 → savings ~$38, marginally over the $30 threshold. BUT: shipping is 14–30 days (would land 2026-05-28 at the earliest, after build start). **Speed wins — go with Amazon.**

### Option C — Adafruit / SparkFun (rejected)

Adafruit MPU6050 breakout is $9.95 each × 12 = $119.40, plus shipping. Better silkscreen, real Stemma QT connectors, but 4× the cost for a part where the GY-521 generic is fine for this application. Skip unless Puru flags an I²C bus stability issue with the cheap boards during smoke test.

## Decision: Option A — Amazon

- **Bottom-line cost: ~$68 all-in (12 MPU6050 + wire + jumpers + headers).**
- **ETA: 2026-05-16 to 2026-05-18** (6–4 days ahead of spec-lock; 7–9 days ahead of build start).
- Risk: HiLetgo GY-521 quality is variable. 2 spares (12 ordered for 10 needed) covers expected DOA rate (~5–10%). If we see >2 bad boards, escalate to Adafruit re-order for the bad ones — won't block the PRD because Puru's RF math is the long pole, not the boards.

## Items still TBD (flag for Puru/Ethan)

- **Mounting:** PRD says "orientation-aware RSSI normalization" — that implies a known, fixed mount orientation per leaf. Are we 3D-printing brackets (Bambu A1 mini, Priya happy to drive)? Or double-stick foam tape v1? Need to know by 2026-05-20 spec-lock.
- **Pull-up resistors:** GY-521 boards include 4.7 kΩ I²C pull-ups on-board. With one MPU6050 per ESP32 (no shared bus), that's fine. If we ever daisy-chain MPU6050s on one I²C bus, pull-ups stack and we'd need to desolder. Not blocking now.
- **Power budget:** MPU6050 draws ~3.9 mA active. Negligible for the wall-powered leaves. **Matters** if/when we go battery-powered (separate roadmap item).

## Follow-ups Priya owns

- [ ] Send order link to Noah today (2026-05-14) so he can place it tonight; aim for Amazon cutoff ~midnight PT for next-day-ish delivery.
- [ ] Calendar reminder: 2026-05-18 confirm all 4 parcels arrived; lay them out on Ethan's bench.
- [ ] Calendar reminder: 2026-05-19 nudge Puru/Ethan re: mounting decision before Sophie's Monday PRD review.
- [ ] If >2 MPU6050 are DOA at smoke-test: order Adafruit replacements same-day Prime.
