# noahnet — project memory for Claude Code

This file is what Claude reads when starting a new session in this repo. It
captures decisions, hardware, and gotchas that aren't obvious from the code
alone. Keep it terse and current — if a fact here stops being true, fix it.

## Project intent

Build a small home network of Noah's microcontrollers and helper devices,
all reachable from his Tailscale tailnet:

- 10 × ESP32-WROOM-32 boards as the leaves (sensors, actuators, controllers)
- 1 × Raspberry Pi Zero W as the bridge — runs Tailscale as a **subnet router**
  so the ESP32s are reachable from the tailnet without each ESP32 needing
  to run a VPN client itself

The repo started as `esp32-fw` (firmware-only) and was renamed to `noahnet`
when the scope expanded to include the Pi bridge and the surrounding
infrastructure. The original ESP32 firmware code is still in `main/` and
remains the firmware story for *future* ESP32-S3 hardware — see "Why not
Tailscale on each ESP32" below.

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
  project (Arduino framework, custom servo code) before this project began

### Raspberry Pi Zero W
- Model: **Raspberry Pi Zero W Rev 1.1** (BCM2835, single-core ARMv6 @ 1 GHz,
  512 MB RAM, 2.4 GHz WiFi only)
- **NOT** a Zero 2 W — confirmed via `cat /proc/cpuinfo`
- OS: Raspberry Pi OS Lite **armhf** Bookworm `6.12.75-1+rpt1` (2026-03-11
  build, freshly flashed)
- SD card: 64 GB (originally read-only because of a stuck write-protect on
  the SD adapter — **the lock switch is now super-glued to the unlocked
  position**, so reflashing always works now)
- Hostname: `raspberrypi` (default — change later if useful)
- LAN: `192.168.0.128/24`, WiFi `wlan0` joined SSID `entropy`
- Reach paths:
  - SSH over WiFi: `ssh pi@raspberrypi.local` (or `pi@192.168.0.128`)
  - USB-gadget serial console: `screen /dev/cu.usbmodem* 115200`
    (gadget mode is `dwc2,g_cdc` — CDC ECM + ACM composite; macOS doesn't
    bind the CDC ECM ethernet sub-function but happily creates the CDC ACM
    serial endpoint, so the serial console is the reliable USB fallback)
- 16 GB SD card from the original Bullseye-era Pi OS install is set aside —
  has gadget mode pre-configured with `g_ether` (Mac-incompatible) but no
  WiFi. Keep as a backup but the 64 GB card is the primary.

### Noah's Mac (the dev box)
- macOS Sequoia (Darwin 24.6.0), Apple Silicon
- LAN: `192.168.0.241/24` on `en0` (WiFi)
- Tailscale: installed and authenticated (used elsewhere on his network;
  not yet wired into this project)
- `gh` authenticated as `noahjohnson0` (active) and `hopeharbor0`
- ESP-IDF: **not installed** — would need it for the ESP32-S3 firmware
  path. PlatformIO (`pio`) is installed and used by `~/repos/esp32_demo`.

## Credentials & secrets

All in `~/repos/.scratch/` (outside the repo, gitignored anyway):

| file | contents |
|---|---|
| `pi-creds.txt` (0600) | Pi user `pi` + SHA-512 password hash + plaintext |
| `wifi.txt` (0600) | `entropy` / WiFi PSK |
| `flash-pi.sh` | one-shot flasher for the Pi's SD card |
| `sd-setup.sh` | SD card config-only edit script (legacy, kept for reference) |

The Pi's `pi` user has the Mac's `~/.ssh/id_ed25519.pub` installed in
`~/.ssh/authorized_keys` — Claude can run commands on the Pi
non-interactively via `ssh pi@raspberrypi.local <cmd>`.

**Never commit the values from these files.** Only paths and the fact that
they exist.

## Network topology (intended)

```
            ┌─────────────────────────────────────────────────────┐
            │  Tailscale tailnet (100.x.y.z)                      │
            │                                                     │
            │     ┌── Noah's Mac, phone, laptop, etc. ────┐       │
            │     │                                       │       │
            │     │   advertised route: 192.168.0.0/24 ───┘       │
            │     │                                               │
            │     └─→ Pi Zero W (subnet router, --advertise-routes)
            │           192.168.0.128                             │
            └─────────────│───────────────────────────────────────┘
                          │
              ╔═══════════│═══════════ WiFi entropy 2.4 GHz ╗
              ║           │                                  ║
              ║   ┌───────┴──────┐                           ║
              ║   │   LAN router │                           ║
              ║   │  192.168.0.1 │                           ║
              ║   └──────┬───────┘                           ║
              ║          │                                   ║
              ║   ESP32 #1 ────── 192.168.0.172 (already on) ║
              ║   ESP32 #2..#10                              ║
              ╚══════════════════════════════════════════════╝
```

The Pi runs `tailscale up --advertise-routes=192.168.0.0/24`. From any
tailnet peer, the ESP32s are reachable directly by their LAN IPs.

## Why not Tailscale on each ESP32?

Tried first, rejected. The only viable port (CamM2325/microlink) needs:
- ESP32-S3 (ts2021 protocol implementation assumes Xtensa LX7 + extra RAM)
- 8 MB PSRAM (H2 receive buffer + JSON MapResponse parser live in PSRAM)

Noah's 10 boards are ESP32-D0WD-V3 (no -S3, no PSRAM). Subnet router on
the Pi is the correct architecture for this hardware mix anyway — one
piece of code to manage instead of 10, and ACL changes apply at one node.

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

## Current state (as of last commit)

- Repo: <https://github.com/noahjohnson0/noahnet> (private)
- Pi: alive, on WiFi, SSH key-authenticated. **No Tailscale installed yet.**
- ESP32: one unit on WiFi at `192.168.0.172` with unknown old firmware;
  the other 9 are unflashed AITRIP boards.
- ESP32 firmware in `main/`: never been built (ESP-IDF not installed,
  hardware target is S3 which Noah doesn't own yet).

## Next moves on deck

1. Install Tailscale on the Pi and bring it onto Noah's tailnet
2. Configure it as a subnet router advertising `192.168.0.0/24`
3. Accept the route on tailnet ACLs at the admin console
4. Confirm reachability of `192.168.0.172` (the live ESP32) from another
   tailnet peer
5. Decide what to do with the 10 ESP32 leaves — flash with custom firmware
   targeting plain ESP32 (drop Tailscale code, keep OTA + HTTP control) or
   leave alone for now
