# mockingbird — project memory for Claude Code

This file is what Claude reads when starting a new session in this repo. It
captures decisions, hardware, and gotchas that aren't obvious from the code
alone. Keep it terse and current — if a fact here stops being true, fix it.

## Project intent

A general-purpose home mesh network platform. Capabilities get built on top
of it over time; the network itself is the substrate. Built from the parts
Noah already has on his desk:

- 1 × **GL.iNet GL-SFT1200 "Opal"** as the network anchor. Joins
  `entropy-5G` upstream over WiFi-as-WAN, rebroadcasts its own `mockingbird`
  SSID on 2.4 GHz, and runs **Tailscale as a subnet router** advertising
  `192.168.8.0/24` so the whole mockingbird LAN is reachable from the tailnet.
- 1 × **Raspberry Pi Zero W** as the processing/storage backend. Joins
  `mockingbird` over WiFi. No on-device Tailscale — reaches the tailnet via
  the Opal's subnet route.
- 10 × **ESP32-WROOM-32** boards composed into leaves (sensors, actuators,
  controllers). A "leaf" is a logical role, not necessarily one board —
  see "Node patterns" below.

**Shipped capability:** distributed BLE sensing — ESP32s scan
advertisements, Pi aggregates + de-dupes + RSSI-fuses. Built on top of
that: MLE multilateration with per-leaf TX/RX bias decomposition, Kalman
fusion + entity clustering (rotating-MAC tracks merge into person-level
identities), live Three.js web dashboard (`services/dashboard.html` /
`mockingbird-dashboard.service` on `:8080`), motion detection, and
auto-calibration. See `docs/roadmap.md` for what's shipped vs in-flight
and `docs/prds/` for the next three Q3 capabilities.

The repo started as `esp32-fw` (firmware-only) and was renamed to `mockingbird`
when the scope expanded to include the Opal bridge and surrounding
infrastructure. **Live firmware lives at `firmware/esp32-wroom-mockingbird/`**
(PlatformIO + Arduino + NimBLE-Arduino, targets plain ESP32-D0WD-V3). The
original ESP-IDF tree in `main/` is unbuilt and reserved for *future*
ESP32-S3 hardware — see "Why not Tailscale on each ESP32" below.

## Node patterns

A leaf is a sensing/actuating role. Each role gets composed from one or
more ESP32 boards plus wires between them. Two patterns in use:

### Single-chip generalist
One ESP32 time-slicing its single 2.4 GHz radio between WiFi STA (to the
Opal) and BLE scanning. Optional ESP-NOW for peer-to-peer signaling with
neighbors — peers must share a channel, which is automatic since all
mockingbird members associate to the same AP.

- Trade-off: BLE scan duty cycle drops to ~30–70% when WiFi is busy.
- Use when: node is comfortably in WiFi range, you want spatial diversity
  across many cheap nodes, and BLE duty cycle isn't critical.

### Paired specialist (BLE scanner + WiFi uplink)
Two ESP32s wired together via UART (TX/RX/GND). One is BLE-only with
WiFi disabled — ~100% BLE scan duty cycle. The other is WiFi-only,
associated to `mockingbird`, forwarding observations to the Pi over MQTT.
UART at 1 Mbps is ~10× the bandwidth a BLE-observation stream needs.

- Trade-off: 2× boards per logical node, 2× power, extra wiring.
- Use when: BLE fidelity matters (capturing rare/short-burst advertisers,
  clean RSSI for trilateration), OR the BLE node is physically out of
  WiFi range and a UART cable to a partner is cleaner than running a
  multi-hop mesh.

### Picking the mix
With 10 boards, a sensible starting deployment is roughly:
- 6 single-chip nodes for spatial coverage and RSSI diversity
- 2 paired specialists (= 4 boards) at remote or critical-coverage spots

Multi-hop wireless mesh (ESP-MESH-LITE etc.) is on the table for the
future but not chosen — wired pairing covers the current out-of-range
case with less code and better BLE fidelity.

## Hardware inventory

### ESP32 nodes (10× WROOM-32, AITRIP pack)
- Chip: **ESP32-D0WD-V3** (rev 3.1) — plain dual-core ESP32, no -S3
- Flash: 4 MB
- PSRAM: **none** (this is the WROOM-32, not the WROVER)
- USB bridge: Silicon Labs **CP2102** (`0x10c4:0xea60`)
- Mac device nodes: `/dev/cu.SLAB_USBtoUART` and `/dev/cu.usbserial-0001`
  (both point at the same chip)
- First unit identified by MAC: `8c:94:df:4c:e1:84`
- One unit already on WiFi at `192.168.0.172` (MAC `58:e6:c5:6f:4a:dc`) —
  appears to have been flashed from the `~/repos/esp32_demo` PlatformIO
  project (Arduino framework, custom servo code) before this project began.
  Will move to `mockingbird` SSID + 192.168.8.x once reflashed.

### GL.iNet GL-SFT1200 "Opal" travel router (WiFi AP + WAN bridge)
Role: broadcasts the **Mockingbird** SSID (the mockingbird LAN) on 2.4 GHz with
NAT, with WiFi-as-WAN upstream to `entropy-5G`. **Does NOT run Tailscale** —
won't fit (see Gotchas). The Pi runs Tailscale instead and advertises the
subnet for tailnet peers.
- Model: **GL-SFT1200** (codename "Opal"), AC1200 dual-band travel router
- SoC: **SiFlower SF19A28** (MIPS interAptiv, 32-bit, little-endian), 128 MB
  RAM, 16 MB flash. *Not* MediaTek MT7621A — I had that wrong earlier.
- WAN: 1× gigabit Ethernet, or WiFi-as-WAN (Repeater mode) to upstream
- LAN: 1× gigabit Ethernet, 2.4 GHz + 5 GHz radios for clients
- Default admin IP: `192.168.8.1`
- MAC (this unit): radio `94:83:c4:85:38:8e`. Printed label says `94:b3:c4`
  but the actual OUI is `94:83:c4` — printed `b3` reads as `83` (font confusion)
- S/N: `fc6ca8510b03526f`   Device ID: `us538d`
- Stock firmware: GL.iNet 4.3.28 (built on **OpenWrt 18.06 / LEDE base**,
  kernel 4.14.90, busybox 1.29.3). Ships with Dropbear v2017.75 —
  **old enough that ed25519 keys don't work**, see Gotchas.
- Admin password: stored at `~/repos/.scratch/glinet-creds.txt`
- **What we layered on top of stock** (key state for next session):
  - `openssh-server` 8.0p1 installed via opkg, **listening on port 2222**.
    Dropbear stays on port 22 as fallback. SSH key auth works on 2222.
  - SSID renamed from default → **Mockingbird** on 2.4 GHz, WPA2-PSK
  - Repeater mode WiFi-as-WAN to `entropy-5G`
  - SSH alias in `~/.ssh/config`: `ssh glinet-new` → port 2222, ed25519 key
- Flash budget: after openssh-server install, **~40 MB free on overlayfs**.
  Watch out — `/tmp` is only 58 MB tmpfs; a Tailscale-sized download fills
  it and wedges sshd. Always stream-extract to `/root` for large operations.
- Noah's **other GL-SFT1200** is already in service broadcasting SSID
  `HomeNet` (radio MAC `94:83:c4:75:47:88-9`). When configuring the new
  unit from a Mac that's plugged into the other one via Ethernet, both
  routers default to `192.168.8.0/24` and the Mac sees `192.168.8.1` on
  both `en0` (WiFi → new) and `en7` (Ethernet → old). Always pass
  `--interface en0` to curl / `-o BindAddress=192.168.8.232` to ssh to
  target the new one.

### Raspberry Pi Zero W (Tailscale subnet router + processing/storage backend)
Role: **runs Tailscale and advertises `192.168.8.0/24`** so tailnet peers
reach everything on Mockingbird by 192.168.8.x. Also doubles as the
processing/storage backend for the ESP32 leaves (data aggregation, per-
capability post-processing, database).

We originally planned to put Tailscale on the Opal. It doesn't fit:
Tailscale ships ~67 MB of binaries; the Opal has ~40 MB free flash.

- Model: **Raspberry Pi Zero W Rev 1.1** (BCM2835, single-core ARMv6 @ 1 GHz,
  512 MB RAM, 2.4 GHz WiFi only — which is why `mockingbird` is on 2.4 GHz)
- **NOT** a Zero 2 W — confirmed via `cat /proc/cpuinfo`
- OS: Raspberry Pi OS Lite **armhf** Bookworm `6.12.75-1+rpt1` (2026-03-11
  build, freshly flashed)
- SD card: 64 GB (originally read-only because of a stuck write-protect on
  the SD adapter — **the lock switch is now super-glued to the unlocked
  position**, so reflashing always works now)
- Hostname: `raspberrypi` on the LAN, `mockingbird-pi` on the tailnet
- LAN: `wlan0` on Mockingbird, `192.168.8.202/24` (DHCP — may renumber) <!-- VERIFY: Macca to either reserve DHCP lease on the Opal for the Pi's MAC, or move leaves to mDNS hostname `mockingbird-pi.local`; then change this to "static reservation" -->
- **Tailscale**: `mockingbird-pi` at `100.83.26.55` (IPv4) /
  `fd7a:115c:a1e0::5838:1a37` (IPv6) <!-- VERIFY: Macca to confirm tailnet IPs still match and stamp a "last verified YYYY-MM-DD" -->. Advertises `192.168.8.0/24`, route
  approved at the control plane. IP forwarding live
  (`net.ipv4.ip_forward=1`, persisted in `/etc/sysctl.d/99-tailscale.conf`).
- Reach paths (preferred → fallback):
  1. Tailscale (works from anywhere): `ssh pi@mockingbird-pi`
  2. LAN: `ssh pi@192.168.8.202` (or whatever DHCP gave it)
  3. USB-gadget serial console (physical access required):
     `screen /dev/cu.usbmodem* 115200` — gadget is `dwc2,g_cdc`; macOS
     reliably exposes CDC ACM serial, less reliably CDC ECM ethernet
- 16 GB SD card from the original Bullseye-era install is set aside as
  backup (has `g_ether` gadget — Mac-incompatible — and no WiFi). 64 GB
  card is the primary. <!-- VERIFY: Noah to confirm the 16 GB card still exists on the shelf and is worth keeping now that the 64 GB has months of state -->

### Noah's Mac (the dev box)
- macOS Sequoia (Darwin 24.6.0), Apple Silicon
- LAN: `192.168.0.241/24` on `en0` (WiFi)
- Tailscale: installed and authenticated. Now sees `mockingbird-pi` as a peer
  with `192.168.8.0/24` subnet route. Mac is currently on Mockingbird so it
  reaches `192.168.8.x` directly via LAN, not through the tailnet route —
  the route is for OTHER tailnet peers (phone, Windows server, etc.).
- `gh` authenticated as `noahjohnson0` (active) and `hopeharbor0`
- ESP-IDF: **not installed** — would need it for the ESP32-S3 firmware
  path. PlatformIO (`pio`) is installed and used by `~/repos/esp32_demo`.

## Credentials & secrets

All in `~/repos/.scratch/` (outside the repo, gitignored anyway):

| file | contents |
|---|---|
| `pi-creds.txt` (0600) | Pi user `pi` + SHA-512 password hash + plaintext |
| `wifi.txt` (0600) | `entropy` / WiFi PSK (used as upstream-WAN for Opal) |
| `mockingbird-wifi.txt` (0600) | `Mockingbird` SSID + auto-generated PSK (the mockingbird LAN) |
| `glinet-creds.txt` (0600) | Opal admin password |
<!-- VERIFY: Noah to delete `~/repos/.scratch/tailscale-authkey` (empty/unused) and remove this row entirely. Leaving here until physical-file deletion is confirmed. -->
| `flash-pi.sh` | one-shot flasher for the Pi's SD card |
| `install-tailscale-on-router.sh` | abandoned — won't fit; kept as a reference of the wrong approach |
| `sd-setup.sh` | SD card config-only edit script (legacy, kept for reference) |

SSH key auth installed on both:
- **Pi:** `ssh pi@mockingbird-pi` (via tailnet) or `ssh pi@192.168.8.202` (LAN)
- **Opal:** `ssh glinet-new` (alias in `~/.ssh/config` → port 2222,
  ed25519 key). The `pi` user's sudo still needs a password — pull it from
  `~/repos/.scratch/pi-creds.txt` and `echo "$pw" | sudo -S ...` from scripts.

**Never commit the values from these files.** Only paths and the fact that
they exist.

## Network topology (current — live)

```
       ┌── Tailscale tailnet ─────────────────────────────────────┐
       │                                                          │
       │   Noah's Mac (100.109.193.26), phone, Windows server,    │
       │   etc. — all reach 192.168.8.0/24 via the subnet route   │
       │   advertised by mockingbird-pi.                              │
       │                          │                               │
       └──────────────────────────┼───────────────────────────────┘
                                  │ wireguard
                                  ▼
                  ┌───────────────────────────────┐
                  │ Pi Zero W "mockingbird-pi"        │
                  │   tailnet: 100.83.26.55       │
                  │   LAN:     192.168.8.202      │
                  │   role:    subnet router      │
                  │           + processing/storage│
                  └───────────────┬───────────────┘
                                  │ Mockingbird WiFi 2.4 GHz
        ┌─────────────────────────┴───────────────────────────┐
        │                          │                          │
   ┌──────────┐         ┌────────────────────┐    ┌──────────────────┐
   │ Mac      │         │ GL.iNet Opal       │    │ ESP32 leaves     │
   │ (dev)    │         │ (WiFi AP + NAT)    │    │ ×10              │
   │ 192.168. │         │ 192.168.8.1        │    │ 192.168.8.x      │
   │ 8.134    │         │ WAN → entropy-5G   │    │ sensors / mesh   │
   └──────────┘         └────────────────────┘    └──────────────────┘
```

The Pi runs `tailscale up --advertise-routes=192.168.8.0/24 --accept-routes`.
The Opal is a dumb WiFi AP + NAT box; no Tailscale on it (won't fit, see
Gotchas). The Pi reaches the internet through the Opal's WAN to entropy.

## Why not Tailscale on each ESP32?

Tried first, rejected. The only viable port (CamM2325/microlink) needs:
- ESP32-S3 (ts2021 protocol implementation assumes Xtensa LX7 + extra RAM)
- 8 MB PSRAM (H2 receive buffer + JSON MapResponse parser live in PSRAM)

Noah's 10 boards are ESP32-D0WD-V3 (no -S3, no PSRAM). Subnet router on
the Opal is the right architecture for this hardware mix anyway — one
piece of code to manage instead of 10, ACL changes apply at one node,
and the Opal can run the full Tailscale daemon natively via GL.iNet's
firmware integration.

The `main/` ESP32 firmware in this repo is still written against the S3 +
MicroLink design. Keep it for the day Noah buys an S3 board, or refactor
into LAN-only firmware (no on-device Tailscale) if these 10 WROOM-32s
need bespoke firmware beyond the existing `esp32_demo` Arduino code.

## Gotchas learned (the hard way)

- **macOS doesn't speak RNDIS.** USB gadget mode `g_ether` defaults to
  RNDIS on modern Raspberry Pi OS kernels and macOS won't bind a network
  interface. Use `g_cdc` (CDC ECM + ACM composite) instead. Even then,
  macOS may bind only the CDC ACM (serial), not the CDC ECM (ethernet),
  on a composite device — the serial fallback is reliable.
- **Bookworm doesn't process `wpa_supplicant.conf` on the boot partition
  reliably.** It uses NetworkManager. Drop a `.nmconnection` file via a
  `firstrun.sh` instead (Imager pattern: `systemd.run=/boot/firmware/firstrun.sh
  systemd.run_success_action=reboot systemd.unit=kernel-command-line.target`).
- **macOS FAT mounts go read-only when the SD adapter's hardware lock
  switch is engaged.** `diskutil info` will show `Media Read-Only: Yes`.
  `touch` of an empty file may appear to succeed (cached) but real writes
  fail. The lock switch is the *single* most common SD-card-on-Mac problem.
  Glue the slider in place to make the problem disappear forever.
- **macOS shells corrupt `$6$...$...$...` strings in inline `sudo sh -c`
  commands** — the `$6` / `$8` look like positional-parameter expansions
  inside a `-c` argument. Always read a SHA-512 crypt hash from a file
  rather than embedding it in a command line.
- **Pi Zero W is ARMv6, can't run 64-bit Pi OS.** Use the `armhf` image
  variant: `https://downloads.raspberrypi.com/raspios_lite_armhf_latest`.
- **Two Opals on the same `192.168.8.0/24` confuses everything.** See
  the Opal section above — always pin the source interface when configuring.
- **GL.iNet stock firmware ships Dropbear 2017.75 which doesn't support
  ed25519.** SSH key auth silently fails with "Pubkey auth attempt with
  unknown algo". Fix: `opkg install openssh-server`, run it on port 2222.
  Don't bother trying RSA-SHA2-256 either — Dropbear 2017 only knows
  ssh-rsa with SHA-1, which modern OpenSSH disables by default.
- **GL.iNet's `/tmp` is a 58 MB tmpfs.** Anything big (Tailscale's 31 MB
  tarball + ~60 MB extraction) fills it, which wedges sshd. Stream-extract
  to `/root` (overlayfs, ~87 MB free pre-install) instead.
- **GL-SFT1200 SoC is SiFlower, not MediaTek.** Tailscale's `mips` build
  is big-endian; this SoC is **little-endian** — use `mipsle` tarballs.
  Verify with `hexdump -C /bin/busybox | head -1`: ELF byte 5 = `01` for LE,
  `02` for BE.
- **Tailscale doesn't fit on the Opal.** 67 MB of binaries on 16 MB of flash
  is geometrically impossible. Put Tailscale on the Pi or any beefier device
  and treat the Opal as a dumb WiFi/NAT AP.
- **Pi `tailscale up --advertise-routes=...` invocation gets killed when
  SSH disconnects** if the foreground process dies during the auth wait.
  Use `tailscale up` to interactively obtain the URL, then once authed,
  apply route advertisements with `tailscale set --advertise-routes=...`
  in a follow-up call — `set` is non-interactive and survives SSH churn.
- **`busybox` lacks `install`, `base64`, `od`.** Use `cp + chmod`, pipe to
  `python3` on the Mac, and `hexdump` respectively.

## Current state (as of last commit)

- Repo: <https://github.com/noahjohnson0/mockingbird> (private)
- **Opal:** powered up, admin pw set, SSH key auth working on port 2222
  (OpenSSH 8.0 installed via opkg, Dropbear left on 22 as fallback).
  Mockingbird SSID broadcasting, Repeater to `entropy-5G` up.
  **Not** running Tailscale — won't fit.
- **Pi (mockingbird-pi):** on Mockingbird at `192.168.8.202`, **Tailscale up
  and advertising `192.168.8.0/24`** (route approved at the control plane).
  Tailnet IP `100.83.26.55`. IP forwarding live and persisted.
- **Mac:** on Mockingbird at `192.168.8.134`, sees Pi as a direct
  WireGuard peer (14ms RTT, no DERP relay). Tailscale ed25519 SSH key
  installed on both Pi and Opal.
- **ESP32 leaves (firmware pinned at `v0.3.1-stream` in `platformio.ini`; 8 total deployed):** <!-- VERIFY: Vlad/Ethan — confirm what's actually flashed on the fleet. Tier-1 calibration firmware (commit 7992962) and parameterized obs-queue firmware (8e8a451) have landed on branches since the doc was written; the on-device build may be newer than the platformio.ini pin suggests. -->
  - `mockingbird-4ce184` (MAC `8c:94:df:4c:e1:84`) — last seen `192.168.8.244`
  - `mockingbird-4c0bdc` (MAC `8c:94:df:4c:0b:dc`) — last seen `192.168.8.196`
  - `mockingbird-4c36ec` (MAC `8c:94:df:4c:36:ec`) — last seen `192.168.8.219`
  - `mockingbird-4db204` (MAC `8c:94:df:4d:b2:04`) — last seen `192.168.8.168`
  - `mockingbird-4ce1d8` (MAC `8c:94:df:4c:e1:d8`) — last seen `192.168.8.178` *(flashed 2026-05-11)*
  - `mockingbird-4ceb7c` (MAC `8c:94:df:4c:eb:7c`) — last seen `192.168.8.126` *(flashed 2026-05-11)*
  - `mockingbird-92838c` (MAC `30:76:f5:92:83:8c`) — last seen `192.168.8.204` *(flashed 2026-05-11; Espressif OUI, not AITRIP — different batch)*
  - `mockingbird-4d4384` (MAC `8c:94:df:4d:43:84`) — last seen `192.168.8.107` *(flashed 2026-05-11)*
  - All eight **stream BLE observations as newline-delimited JSON over TCP
    to `mockingbird-pi:9001`** (the collector). Per-leaf state on-device
    is now just a 64-entry queue + counters, no accumulation, no OOM.
    Heap stays at ~110–125 KB free under continuous heavy scanning.
    Each `obs` line carries `mac, rssi, addr_type, ch, name, manuf, t_ms,
    location`. `ch` is the BLE primary advertising channel (37/38/39) and
    is `0` on leaves that can't determine it — NimBLE-Arduino 1.4.2's
    legacy callback path doesn't expose the channel index; see firmware
    `main.cpp` (PURU-2) for the follow-up plan. Collector stores it as
    `obs.chan INTEGER`, nullable.
  - HTTP API on `:80` is minimal: `GET /`, `GET /version`, `POST /restart`.
    `/scan/*` is gone (the Pi has every observation continuously).
  - ArduinoOTA on UDP 3232 still works; the BLE scan pauses during OTA.
  - ~3 unflashed AITRIP boards remain in the pack (started with 10 AITRIP; 7 AITRIP deployed; 1 of the deployed 8 is a non-AITRIP Espressif-OUI unit from a different batch — so 10 − 7 = 3 AITRIP left). <!-- VERIFY: Noah to physically count the unflashed boards on the desk. -->
- One unit on entropy at `192.168.0.172` (MAC `58:e6:c5:6f:4a:dc`) with old
  pre-mockingbird firmware — needs reflash to join the mesh. <!-- VERIFY: Noah/Vlad — ping 192.168.0.172. If responds, reflash. If not, delete this line. -->
- ESP32 firmware in `main/`: never been built (ESP-IDF not installed,
  hardware target is S3 which Noah doesn't own yet).
- **Pi services on the Pi (systemd):**
  - `mockingbird-collector.service` → `services/mockingbird-collector.py`:
    listens on `0.0.0.0:9001`, writes every observation into
    `~/mockingbird/observations.sqlite` with indices on `(ts)`, `(mac, ts)`,
    `(leaf, ts)`. Stores `leaf_events` for hello/heartbeat/disconnect.
    Hardened by MAC-2: crash-loop guard, OOM bias `-500`, MemoryHigh=128M /
    MemoryMax=192M, graceful SIGINT shutdown, journald cap.
  - `mockingbird-dashboard.service` → `services/mockingbird-dashboard.py`
    + `services/dashboard.html`: live Three.js web UI on `:8080`
    (live heatmap, trails, click-to-select, debounced auto-save, bird
    codenames, north compass).
  - `services/mockingbird_calibration.py`: per-leaf TX/RX bias decomposition
    + path-loss solver (Puru's RF review covers this).
  - `services/mockingbird_tracks.py`: Kalman fusion + entity clustering
    (multi-MAC tracks survive Apple Continuity MAC rotation).
- **Live analysis** via `scripts/analyze_ble_db.py` — query any time
  window from the SQLite DB on the Pi. Output: per-leaf rates, uplink
  health from latest heartbeat, coverage histogram, RSSI matrix, biggest-
  spread (spatial-info) devices, per-leaf singletons, manufacturer +
  named-advertiser tables, **churn detection** (midpoint appeared /
  disappeared lists). First full 4-way 60s run: 78 devices, 46 universal
  (59%), ~110 obs/sec aggregate, zero drops, zero crashes.
- Old `scripts/analyze_ble_capture.py` kept for the legacy JSON-file
  captures in `~/repos/.scratch/ble-captures/`; new captures don't
  produce JSON files at all — everything goes straight to the DB.
- `scripts/bootstrap-pi-subnet-router.sh` is the canonical bootstrap
  for the Pi side of the mesh (Opal is a dumb AP and isn't scripted).
- `tools/ota_serve.py` is the helper for pushing OTA images to leaves
  by mDNS hostname (`mockingbird-<chipid>.local`) — used during fleet
  reflashes from the Mac.

## How we test

Test suite lives in `tests/` (pytest), with `conftest.py` and starter
coverage from Andy's ANDY-1/ANDY-2 audits:
- `test_wire_contract.py` — leaf-to-collector JSON wire format + canary
- `test_calibration_pathloss.py`, `test_calibration_solvers.py` — RF math
- `test_numerical_stability.py` — Joseph-form Kalman, Tikhonov-regularized solves
- `test_tracks_smoothing.py` — entity clustering / track fusion
Coverage audit at `docs/test/coverage-audit.md`. <!-- VERIFY: Andy — once `docs/test-pyramid` branch (`docs/testing.md`) merges to main, add a pointer here. -->

## Shipped capabilities

(Full status list in `docs/roadmap.md`.)

- Distributed BLE sensing: 8 leaves stream observations → Pi collector → SQLite.
- MLE multilateration with per-leaf TX/RX bias decomposition.
- Robust Kalman fusion (Joseph form, Tikhonov-regularized solves), heavy
  EWMA for centroid devices (jitter 300 cm → 18 cm).
- Entity clustering: multi-MAC tracks survive Apple Continuity rotation;
  phone auto-detection via 4-leaf walk.
- Live Three.js web dashboard on `:8080`: heatmap, trails, click-to-select,
  bird codenames, north compass, debounced auto-save.
- Adaptive ZUPT + velocity clamp (kills phantom motion).
- Wire-format contract tests + canary.

## Future capabilities (full list in `docs/roadmap.md`; Q3 PRDs in `docs/prds/`)

- **Person fingerprinting Phases 1 & 2** — co-occurrence clustering +
  Apple Continuity sub-protocol fingerprints for stable identity across
  MAC rotation. PRD: `docs/prds/person-fingerprinting-phase-1-2.md`. Owner: Wanjiru.
- **IMU on every leaf** — MPU6050 over I²C; orientation-aware RSSI
  normalization, tamper detection, activity context. PRD:
  `docs/prds/imu-on-leaves.md`. Owners: Puru (RF) + Ethan (FW).
- **Presence & anomaly alerts** — household-level "who is where, is this
  normal?" PRD: `docs/prds/presence-anomaly-alerts.md`. Owner: Ethan.
- **ESP-NOW peer mesh** — for leaves out of WiFi range.
- **ESP32-S3 firmware path** — designed in `main/`, unflashed; awaits S3 hardware.
- **Battery-powered leaves**, **audio leaf** (I²S MEMS mic + classifier),
  **PIR-augmented leaf**, **HomeKit/HA bridge**.

Spec-lock for the three Q3 PRDs: **2026-05-20** (Sophie chairs weekly
PRD review every Monday 10:00).

## Next moves on deck

1. Land the three Q3 PRDs (`docs/prds/`) by the 2026-05-20 spec-lock:
   Person fingerprinting (Wanjiru), IMU on leaves (Puru + Ethan),
   Presence & anomaly alerts (Ethan).
2. Order MPU6050 IMU parts so the IMU PRD isn't blocked by shipping lead time.
3. Close out the post-integration cleanup items from the Q3 reviews:
   - Macca: DHCP reservation for Pi (or move leaves to `mockingbird-pi.local` mDNS) — see VERIFY comment on the Pi LAN line above.
   - Vlad/Ethan: confirm what firmware version is actually flashed on the fleet (audit item #1).
   - Sophie: drive a `docs/roadmap.md` status pass (move shipped items out of "Planned").
4. Decide rogue ESP32 at `192.168.0.172`: reflash and bring to Mockingbird, or remove from inventory (audit item #9).
5. Optional: write `scripts/bootstrap-pi-tailscale.sh` to reproduce the
   Pi-as-subnet-router setup from scratch (the Opal bootstrap is
   intentionally not scripted — it's a one-time stock-firmware setup).
