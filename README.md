# mockingbird

A small home mesh network platform. A travel router as the anchor, a
Raspberry Pi as the processing/storage backend, and a fleet of ESP32 leaves
— all reachable from a Tailscale tailnet via a single subnet route. The
network is general-purpose; capabilities get layered on as they're built.

```
        ┌── Tailscale tailnet ─────────────────────────────────┐
        │                                                      │
        │   Noah's Mac, phone, laptop, etc.                    │
        │                  │                                   │
        │                  │ via subnet route 192.168.8.0/24   │
        └──────────────────┼───────────────────────────────────┘
                           │
                           ▼
            ┌──────────────────────────────────┐
            │ GL.iNet Opal "mockingbird-router"    │
            │   WAN: upstream WiFi entropy-5G  │
            │   LAN: 192.168.8.0/24            │
            │   SSID: mockingbird (2.4 GHz)        │
            │   Tailscale: subnet router       │
            └─────────────┬────────────────────┘
                          │ 2.4 GHz
        ┌─────────────────┴────────────────────┐
        │                                      │
 ┌──────────────┐                  ┌────────────────────────┐
 │ Pi Zero W    │ ◄── HTTP/MQTT ── │ ESP32 leaves ×10       │
 │ processing + │                  │ sensors / actuators +  │
 │ storage      │                  │ peer-to-peer mesh      │
 └──────────────┘                  └────────────────────────┘
```

## Components

- **GL.iNet GL-SFT1200 "Opal"** (the network anchor). Travel router running
  GL.iNet's OpenWrt-based firmware. Connects to the household's upstream
  WiFi (`entropy-5G`) via WiFi-as-WAN and rebroadcasts its own `mockingbird`
  SSID on 2.4 GHz. Hosts the Tailscale subnet router so the whole
  `192.168.8.0/24` LAN is reachable from any tailnet peer.

- **Raspberry Pi Zero W** (processing + storage). Joins `mockingbird` over
  WiFi. Aggregates data from the ESP32 leaves, runs whatever post-
  processing each capability needs, and persists results. No on-device
  Tailscale — reaches the tailnet via the Opal's subnet route.

- **ESP32-WROOM-32 nodes ×10** (the leaves). Composed into logical
  leaves — a "leaf" is a sensing/actuating role, not necessarily one
  board. Two patterns:
  - **Single-chip generalist** — one ESP32 doing WiFi STA + BLE scan
    on the same radio. Time-sliced; ~30–70% BLE scan duty cycle when
    WiFi is busy. Cheap to deploy, good for spatial coverage.
  - **Paired specialist** — two ESP32s wired together via UART. One
    BLE-only (~100% BLE duty cycle, WiFi disabled), one WiFi-only
    (associated to `mockingbird`, forwards observations to the Pi).
    Use for remote out-of-range BLE locations or critical-coverage
    spots where you need clean RSSI + complete advert capture.

  See `CLAUDE.md` → "Node patterns" for the full rationale and the
  recommended mix across 10 boards.

## Capabilities

The network is the substrate. Each capability is a deployable workload that
runs across some subset of nodes.

- [ ] **Distributed BLE sensing** — every ESP32 scans BLE advertisements
  and publishes observations; the Pi de-duplicates by device address and
  fuses RSSI across nodes for rough indoor positioning. Real protocol
  sniffing of established BLE connections is delegated to a separate
  nRF52840 dongle.
- [ ] _(more — capabilities added as needed)_

## Layout

```
.
├── CLAUDE.md                   project memory; read this for full context
├── README.md                   this file
├── scripts/
│   └── bootstrap-glinet-router.sh   Opal one-shot setup (WiFi-WAN, SSID,
│                                    Tailscale, subnet route)
├── main/                       aspirational ESP32-S3 firmware (MicroLink
│                               + on-device Tailscale). Not flashed to
│                               the current WROOM-32 fleet — see below.
├── partitions.csv              two OTA slots, 8 MB flash (for the S3 path)
├── sdkconfig.defaults          ESP-IDF + MicroLink tuning
├── sdkconfig.credentials.example   copy → sdkconfig.credentials, fill in
├── external/microlink/         git submodule, not initialized by default
└── tools/ota_serve.py          HTTP server for pushing OTA builds
```

## Setting up the Opal

Run the bootstrap script against the Opal once it's powered up with SSH
access and a Tailscale auth key staged:

```bash
# One-time prereqs (manual)
ssh-copy-id glinet-new                       # install SSH key on the router
echo "SSID=entropy-5G"   > ~/repos/.scratch/wifi.txt
echo "PSK=..."          >> ~/repos/.scratch/wifi.txt
# generate at https://login.tailscale.com/admin/settings/keys
echo "tskey-auth-..." > ~/repos/.scratch/tailscale-authkey

# Bootstrap
./scripts/bootstrap-glinet-router.sh
```

The script:
1. Renames the device to `mockingbird-router`
2. Joins `entropy-5G` as upstream (WiFi-as-WAN repeater mode)
3. Rebroadcasts the new `mockingbird` SSID on 2.4 GHz (auto-generated PSK,
   saved to `~/repos/.scratch/mockingbird-wifi.txt`)
4. Installs the Tailscale package and runs `tailscale up` with the staged
   authkey, `--advertise-routes=192.168.8.0/24`

After it runs, approve the subnet route in the Tailscale admin console
(`https://login.tailscale.com/admin/machines`).

## Adding a leaf to mockingbird

Any device that joins the `mockingbird` SSID with the PSK from
`~/repos/.scratch/mockingbird-wifi.txt` becomes a member. From any tailnet
peer it'll be reachable by its `192.168.8.x` LAN IP.

For the Pi:

```bash
# from the Mac
NM_KEY=$(awk -F= '/^PSK=/{print $2}' ~/repos/.scratch/mockingbird-wifi.txt)
ssh pi@raspberrypi.local "
  sudo nmcli connection add type wifi con-name mockingbird ifname wlan0 \
       ssid mockingbird 802-11-wireless-security.key-mgmt wpa-psk \
       wifi-sec.psk '$NM_KEY'
  sudo nmcli connection up mockingbird
"
```

For an ESP32, set the WiFi credentials in whatever firmware you're flashing
and reboot. (The `main/` firmware reads `CONFIG_WIFI_SSID` /
`CONFIG_WIFI_PSK` from `sdkconfig.credentials`.)

## The `main/` firmware

`main/` holds an ESP-IDF firmware for **ESP32-S3** boards that joins the
Tailscale tailnet directly via [MicroLink][microlink] — bypassing the
subnet-router model entirely. It is **not** flashed to the current
WROOM-32 fleet, which lack the PSRAM that MicroLink requires.

Keep this code for when ESP32-S3 hardware arrives, or fork it for the
WROOM-32 boards by dropping the MicroLink bits and pointing it at the
Opal-subnet-route reachability model instead.

See the section "Why not Tailscale on each ESP32?" in `CLAUDE.md` for
the full rationale.

[microlink]: https://github.com/CamM2325/microlink
