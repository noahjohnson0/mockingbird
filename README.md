# esp32-fw

General-purpose ESP32-S3 firmware that joins a Tailscale tailnet at boot and
exposes an HTTP control + OTA endpoint. Once flashed, every device shows up
as a peer on your tailnet — you can hit it by MagicDNS name from anywhere
you're on the tailnet, and you can push new firmware to it the same way.

```
   WiFi STA  →  MicroLink (Tailscale ts2021 + WireGuard)  →  esp_http_server
                                                              ├─ GET  /         status JSON
                                                              ├─ GET  /version  app version
                                                              ├─ POST /ota      pull-OTA from URL
                                                              └─ POST /restart  reboot
```

## Hardware

- **ESP32-S3 with PSRAM** (the OCT 8 MB variant is the safe default — what
  [MicroLink][microlink] is tuned for). Plain ESP32-WROOM **will not work** —
  MicroLink's H2 / JSON / peer buffers live in PSRAM.
- 8 MB flash. The partition table allocates two ~4 MB OTA slots.

## Layout

```
.
├── CMakeLists.txt              top-level project
├── partitions.csv              two OTA slots, 8 MB flash
├── sdkconfig.defaults          ESP32-S3 + PSRAM + OTA + MicroLink tuning
├── sdkconfig.credentials.example  copy → sdkconfig.credentials, fill in
├── external/microlink/         git submodule — github.com/CamM2325/microlink
├── main/
│   ├── main.c                  boot → wifi → microlink → httpd
│   ├── wifi.{c,h}              minimal STA bring-up
│   ├── http_server.{c,h}       control + OTA endpoint
│   ├── ota.{c,h}               esp_https_ota wrapper + rollback confirm
│   └── Kconfig.projbuild       app-level Kconfig (FW_OTA_TOKEN, …)
└── tools/ota_serve.py          tiny http server for pushing builds
```

## One-time setup

### 1. Install ESP-IDF v5.x or v6.x

```bash
mkdir -p ~/esp && cd ~/esp
git clone --recursive -b release/v5.4 https://github.com/espressif/esp-idf.git
./esp-idf/install.sh esp32s3
source ./esp-idf/export.sh   # add this to your shell profile or run per session
```

### 2. Clone this repo and pull MicroLink

```bash
cd ~/repos/esp32-fw
git submodule update --init --recursive
```

> The submodule isn't pre-populated in this repo — initialize it explicitly so
> you can decide whether to trust [CamM2325/microlink][microlink] (the
> Tailscale-compatible client). It's a third-party C implementation of the
> ts2021 protocol; review the source before flashing it onto devices that sit
> on your tailnet.

### 3. Provide credentials

```bash
cp sdkconfig.credentials.example sdkconfig.credentials
$EDITOR sdkconfig.credentials   # fill in WiFi, Tailscale auth key, OTA token
```

Generate a Tailscale auth key at
<https://login.tailscale.com/admin/settings/keys>. Recommended settings:
*reusable*, *pre-approved*, and *tagged* (`tag:esp32`) so an attacker who
extracts the key from flash can't pivot into your tailnet as an arbitrary
user. Generate the OTA token with `openssl rand -hex 32`.

### 4. Build and flash

```bash
idf.py set-target esp32s3
idf.py build
idf.py -p /dev/cu.usbmodem* flash monitor
```

You should see the device transition `IDLE → WIFI_WAIT → CONNECTING →
REGISTERING → CONNECTED` and log its assigned Tailscale IP.

## OTA over Tailscale

From any machine on the tailnet:

```bash
# 1. Build the new firmware locally
idf.py build

# 2. Serve build/esp32-fw.bin over HTTP from your machine
python3 tools/ota_serve.py
# → serving … as http://0.0.0.0:8000/esp32-fw.bin

# 3. In another terminal, tell the device to pull it
DEVICE=esp32-fw            # MagicDNS name, or use the 100.x.y.z IP
TOKEN=$(grep CONFIG_FW_OTA_TOKEN sdkconfig.credentials | cut -d'"' -f2)
MY_TS_IP=$(tailscale ip -4)

curl -X POST "http://$DEVICE/ota" \
     -H "X-OTA-Token: $TOKEN" \
     -d "{\"url\":\"http://$MY_TS_IP:8000/esp32-fw.bin\"}"
```

The flow:

1. The device receives the request on its Tailscale IP, validates the token,
   and queues `fw_ota_pull_and_apply()` on a worker task.
2. `esp_https_ota` streams the binary from your machine — also over the
   tailnet — into the inactive OTA slot, verifying the embedded image hash.
3. The bootloader marks the new slot as *pending verify* and reboots.
4. On the new boot, after WiFi and Tailscale come back up,
   `fw_ota_mark_self_ok()` confirms the image. If the new app crashes
   instead, the bootloader rolls back to the previous slot automatically
   (`CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y`).

WireGuard already encrypts the link, so plain `http://` over the tailnet is
fine. For pulls from a public URL, use `https://` — the bundled root CA set
(`CONFIG_MBEDTLS_CERTIFICATE_BUNDLE=y`) covers most public CAs.

## Status endpoint

```bash
$ curl http://esp32-fw/
{"version":"1","build":"May 10 2026 19:53:11","uptime_s":124,"free_heap":156384,
 "tailscale":{"connected":true,"vpn_ip":"100.81.222.42","peers":3}}
```

## Adding your own code

`main/main.c` is the orchestrator. Drop application logic into a new task
launched from `app_main` after the HTTP server is up. The control server and
OTA flow are independent — they keep working even if your app code panics
inside its own task, which is what makes recovery-by-OTA possible.

To expose application-specific endpoints, register more `httpd_uri_t`
handlers from `http_server.c`. To gate them behind the same shared secret,
call `check_token(req)` at the top of the handler.

## Security notes

- **`CONFIG_FW_OTA_TOKEN` is the entire access control story** on the OTA
  endpoint. Treat it like an SSH key.
- The HTTP server binds to `0.0.0.0`, so it answers on the LAN IP too. If
  you want to be paranoid, drop the firewall on the WiFi router or modify
  `http_server.c` to reject requests whose destination IP isn't in
  100.64.0.0/10.
- Tailscale auth keys baked into firmware are recoverable from flash by
  anyone with physical access. Use *ephemeral* + *tagged* keys with ACLs
  that limit what an `esp32` node can reach.

[microlink]: https://github.com/CamM2325/microlink
