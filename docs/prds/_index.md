# PRD Index — Q3 capabilities

Curated by Sophie Lefèvre, 2026-05-13. These are the next three highest-leverage capabilities on top of the shipped BLE-sensing substrate.

| PRD | Status | Owner | Estimate | Depends on |
|---|---|---|---|---|
| [Person fingerprinting — Phases 1 & 2](person-fingerprinting-phase-1-2.md) | Draft | Wanjiru (DS) | L | — |
| [IMU on every leaf](imu-on-leaves.md) | Draft | Puru (RF) + Ethan (FW) | M | Hardware order |
| [Presence & anomaly alerts](presence-anomaly-alerts.md) | Draft | Ethan (SWE) | S-M | Person fingerprinting + IMU |

## Sequencing rationale

1. **Person fingerprinting** first — it unblocks every "who" question downstream. Phase 1 needs no new hardware; we have the data already.
2. **IMU** in parallel — independent track (hardware + firmware), no dependency on fingerprinting, closes the residual accuracy gap and adds tamper detection. Order the parts now so the engineering work isn't blocked by shipping lead time.
3. **Alerts** last — it is the smallest PRD by effort but the largest by user-perceived value. Gating it on the two upstream PRDs is correct; shipping alerts on top of anonymous clusters and no tamper data would be a supermarket croissant.

## Review cadence

Weekly PRD review every Monday 10:00 — Sophie chairs, all owners attend with status + blockers. Spec-lock deadline for all three: **2026-05-20**.
