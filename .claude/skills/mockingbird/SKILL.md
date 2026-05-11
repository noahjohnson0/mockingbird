---
name: mockingbird
description: Operate Noah's mockingbird home mesh network — show node inventory (Pi, Opal, ESP32 leaves), flash a USB-connected ESP32, push OTA updates by mDNS hostname, SSH into the Pi or Opal. Use when the user mentions mockingbird, the noahnet/mockingbird repo, the Mockingbird WiFi, or any of its known nodes (`mockingbird-pi`, `mockingbird-*` ESP32s, `glinet-new`).
---

# mockingbird

Small ops surface for the **mockingbird** project (repo `~/repos/mockingbird`,
GitHub `noahjohnson0/mockingbird`). Read `~/repos/mockingbird/CLAUDE.md`
before doing anything substantive — it has the live state, credentials map,
and the gotchas you must respect.

## Architecture in one paragraph

The **Mockingbird** SSID (2.4 GHz, broadcast by the GL.iNet Opal at
`192.168.8.1`) is the noahnet LAN at `192.168.8.0/24`. The **Pi** at
`mockingbird-pi` runs Tailscale and advertises that subnet so the rest of
Noah's tailnet can reach Mockingbird devices by their 192.168.8.x IP. The
ESP32 leaves (`mockingbird-<chipid>.local`) join Mockingbird, run a small
HTTP control surface, and accept ArduinoOTA push-flashes on UDP 3232. The
Opal itself is a dumb WiFi-AP + NAT box; Tailscale does **not** run on it
(see `CLAUDE.md` gotchas — won't fit in 16 MB flash).

## Default behavior — `/mockingbird` with no args

Show a node-inventory snapshot. Run the equivalent of `show nodes`:

1. `tailscale status` — tailnet peers
2. Burst-ping `192.168.8.0/24` to populate ARP, then dump `arp -an | grep 192\.168\.8\.` with vendor labels
3. Resolve `mockingbird-pi` and any known `mockingbird-*.local` hosts via mDNS

Format like the layout in `CLAUDE.md` § "Network topology". Highlight any
node that's known-expected but missing (e.g. `mockingbird-4ce184` was
last seen at 192.168.8.244).

## Verbs (when args supplied)

- **`flash`** — A USB-connected ESP32 is at `/dev/cu.SLAB_USBtoUART`. Confirm
  it's an ESP32 via `python3 -m esptool ... chip-id`, capture its MAC, then
  `cd ~/repos/mockingbird/firmware/esp32-wroom-mockingbird && pio run -e usb -t upload`.
  After ~15 s, verify via mDNS that `mockingbird-<last3macbytes>.local`
  resolves and the device's `GET /` returns JSON with the matching hostname.

- **`ota <node>`** — Push the current build to a running ESP32 by mDNS
  hostname (e.g. `mockingbird-4ce184.local`) or IP. Uses
  `pio run -e ota -t upload --upload-port=<node>`. If pio's espota wrapper
  returns generic "Error Uploading", retry directly:
  `python3 ~/.platformio/.../espota.py --ip=<ip> --port=3232 --file=.pio/build/ota/firmware.bin`.
  Verify post-flash by re-reading `GET /` and checking `uptime_s` reset.

- **`pi`** — SSH to the Pi: `ssh pi@mockingbird-pi` (tailnet) or
  `ssh pi@192.168.8.202` (LAN). `sudo` needs a password — pull from
  `~/repos/.scratch/pi-creds.txt`, pipe via `echo "$pw" | sudo -S ...`.

- **`opal`** — SSH to the GL.iNet: `ssh glinet-new` (alias resolves to
  `root@192.168.8.1:2222` via the ed25519 key). Note: the stock Dropbear on
  port 22 doesn't accept modern keys; only port 2222 (OpenSSH we installed)
  works for key auth.

- **`status <node>`** — `curl http://<node>/` for any ESP32, prints the JSON.
  Status now includes a `ble` block — `n_unique` (devices seen since last
  reset) and `scan_window_ms`.

- **`ble`** [duration_s] — Run a coordinated BLE-capture experiment across
  every reachable leaf. Just runs `bash ~/repos/mockingbird/scripts/ble-experiment.sh`,
  which: discovers leaves on `192.168.8.0/24` by Espressif OUI in ARP →
  POSTs `/scan/reset` to all of them simultaneously → waits the duration
  (default 60 s) → pulls `/scan/result` from each into
  `~/repos/.scratch/ble-captures/<chipid>-<timestamp>.json` → `scp`'s
  them + `scripts/analyze_ble_capture.py` to the Pi → runs the analyzer
  on **all** captured leaves (N-input). Output: per-leaf stats, coverage
  histogram (singletons → universal), RSSI matrix sorted by best signal,
  biggest-spread devices (most spatially-informative — each has a clear
  "closest" leaf with an estimated distance ratio that cancels out the
  unknown TxPower), per-leaf singleton contributions, what each leaf
  sees most clearly, manufacturer breakdown, named-advertiser RSSI vector.

- **`bleraw <node>`** — Just dump `GET /scan/result` JSON from one leaf
  without resetting. Useful for inspecting a long-running scan window.

## Files in the repo to know

| path | purpose |
|---|---|
| `CLAUDE.md` | live state, hardware inventory, gotchas, next-moves |
| `firmware/esp32-wroom-mockingbird/` | PlatformIO project for the ESP32 leaves (Arduino framework, 4 MB flash, OTA-enabled) |
| `firmware/esp32-wroom-mockingbird/src/main.cpp` | WiFi + ArduinoOTA + WebServer + NimBLE continuous scanner |
| `scripts/gen-esp32-secrets.sh` | regenerates `src/secrets.h` from `~/repos/.scratch/mockingbird-wifi.txt` — never hand-edit secrets.h |
| `scripts/bootstrap-pi-subnet-router.sh` | idempotent Pi setup (Tailscale install, Mockingbird WiFi connection, IP forwarding) |
| `scripts/ble-experiment.sh` | runs the multi-leaf BLE capture pipeline (discover → reset → wait → pull → scp → analyze) |
| `scripts/analyze_ble_capture.py` | N-input multi-leaf analyzer — coverage histogram, RSSI matrix, biggest-spread (spatial-info) devices, per-leaf singletons, named-advertiser table with full RSSI vector |
| `main/` | ESP-IDF firmware *for future ESP32-S3 hardware*. Doesn't run on the current WROOM-32 leaves — wrong chip family. |

## Credentials map (paths only, never values)

All in `~/repos/.scratch/`, mode 0600:

- `pi-creds.txt` — Pi `pi` user password (for sudo)
- `mockingbird-wifi.txt` — `SSID=Mockingbird` / `PSK=...`
- `wifi.txt` — upstream `entropy` / `entropy-5G` PSK for the Opal's WAN
- `glinet-creds.txt` — Opal admin password

## Gotchas (full list in `CLAUDE.md`, the ones that bite during ops)

- **First OTA attempt sometimes fails partway through.** Retry once; it's
  usually a WiFi hiccup. Don't pivot to USB until two consecutive failures.
- **mDNS hostnames cache in macOS** — `dscacheutil -q host -a name` may
  return the old name for a few minutes after rename. Use IP for certainty.
- **`busybox sh` on the Opal lacks `install`, `base64`, `od`.** Use
  `cp + chmod` and `hexdump`. The Pi has a full GNU coreutils — no issue.
- **The Opal has a sibling `Inspiren` router on the LAN at the same
  default `192.168.8.1`.** When you have Ethernet plugged into it,
  `route get 192.168.8.1` picks the wrong path. Always
  `-o BindAddress=192.168.8.232` for SSH or `--interface en0` for curl
  when you need to be sure you're hitting the new Opal.
