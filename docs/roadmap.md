# Mockingbird roadmap

Living list of planned capabilities, in no particular priority. Move items
to "in flight" when started, to "shipped" once landed in `main`, and to
"deferred" if explicitly chosen not to pursue.

## Planned

### IMU (accelerometer + gyroscope) on each leaf
Add a 6-axis IMU (MPU6050 / LSM6DSO / similar — I²C, ~$2 each) to every
leaf. Three concrete payoffs:

1. **Orientation-aware RSSI.** PCB-antenna ESP32 radios have a non-uniform
   gain pattern — RSSI varies up to ~10 dB depending on which way the
   board is pointing. Knowing roll/pitch/yaw lets the analyzer normalize
   readings, which makes the spread-ratio distance estimate meaningfully
   tighter (could close the gap from ±50 % to maybe ±20 %).
2. **Tamper / displacement detection.** If a leaf is moved by more than
   a threshold (say 0.5 m or 15° of rotation), the analyzer flags it
   as "spatial fingerprint invalid" until re-calibrated. Stops one
   re-arranged leaf from poisoning the entire mesh's localization.
3. **Activity context.** Cross-fuse BLE proximity with leaf accelerometer
   bursts: "this AirPod's RSSI rose AND the desk leaf got jostled" =
   someone sat down. Multimodal events.

Implementation sketch:
- Wire MPU6050 to ESP32's I²C pins (SDA=21, SCL=22 on WROOM-32) — share
  the bus across all sensors on the board.
- Firmware adds a `gyro` block to `/status` and a periodic IMU sample
  stream alongside the BLE obs stream (same TCP uplink, new
  `{"event":"imu", ...}` line type).
- Collector grows an `imu` table: `(ts, leaf, ax, ay, az, gx, gy, gz, t_ms)`.
- Calibration step on first install: leaf does a 5 s still period, records
  zero-rate offsets to NVS. Then a known orientation push (e.g. "tap top")
  to anchor the gravity vector.
- Power: MPU6050 draws ~4 mA active, much less in sleep — negligible vs
  ESP32's ~80 mA at WiFi+BLE-active.

### Leaf positions → real trilateration
Today the analyzer reports relative distance ratios (4× closer to A than B)
because the leaf positions are unknown. Once leaves have stickered
locations, capture an explicit `(x, y, z)` for each one in the DB
(`leaf_position(leaf TEXT PRIMARY KEY, x REAL, y REAL, z REAL, notes TEXT)`)
and the analyzer can solve the trilateration system per device. Pairs
nicely with the IMU normalization above.

### Live TUI / web dashboard
A tail-the-DB view that updates every 1–2 s:
- Per-leaf live packet rate, heap, uplink state
- "Currently nearby" rolling 10-s coverage matrix
- Movement events (RSSI delta > N dB over T seconds)

Rich-text TUI on the Pi (running as a second systemd service) or a tiny
read-only HTTP UI on the Pi at `:8080`. The data is already in SQLite —
the dashboard is just queries.

### ESP-NOW peer mesh for out-of-WiFi-range leaves
The Paired Specialist pattern in CLAUDE.md uses UART to bridge a BLE-only
leaf to a WiFi-uplink leaf. Alternative: pair them over ESP-NOW (no WiFi
config needed, no infrastructure). Useful for outdoor or
unusually-shielded spots that can't reach the Opal directly.

### ESP32-S3 firmware path
The `main/` directory already has an ESP-IDF + MicroLink design for ESP32-S3
boards with PSRAM. If Noah ever buys an S3, each leaf could run its own
Tailscale identity instead of relying on the Pi as a subnet router —
useful if a leaf gets deployed somewhere that can't reach Mockingbird WiFi
(e.g. at another house with its own LAN).

### Battery-powered leaves
For deployments that can't tape a USB power cable to a wall socket. Need:
- Larger flash for deeper sleep states (deepsleep + BLE-only periodic wake)
- LiPo + charging circuit (TP4056 module + boost)
- Lower duty cycle in firmware: scan 5 s every minute, sleep otherwise
- Status reporting becomes "battery_pct" alongside heap/RSSI

### Other capabilities (sketch)
- **Audio leaf**: I²S MEMS mic, FFT on ESP32, classifier — picks up
  doorbells, alarms, broken glass.
- **PIR-augmented leaf**: cheap PIR sensor as a high-confidence "human
  here" signal, calibrate BLE RSSI against it.
- **HomeKit / HomeAssistant bridge** on the Pi exposing observations as
  presence/proximity entities.

## In flight
- (none right now — paused at 2/4 leaves labeled, waiting on 3D-printed
  cases for the other two)

## Shipped
- v0.1: WiFi + ArduinoOTA + HTTP control (first OTA-able firmware)
- v0.2.x: On-device BLE scan accumulator with `/scan/reset` + `/scan/result`
- v0.3.0: Streaming architecture — leaves push obs to Pi collector → SQLite
- v0.3.1: NVS-persisted location label per leaf, non-blocking TCP write
- Pi-side: Tailscale subnet router for `192.168.8.0/24`,
  mockingbird-collector systemd service ingesting from `:9001`
- Analyzer: SQLite-backed query for any time window with coverage
  matrix, spread leaders, churn, manufacturer/named breakdowns

## Deferred
- (none right now)
