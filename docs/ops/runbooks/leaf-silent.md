# Runbook — a leaf hasn't sent a heartbeat in 10+ minutes

A leaf is silent. The other leaves are fine. Goal: get the silent one back, or confirm it needs hands.

Triage in order — soft → hard. Most outages stop at step 2 or 3.

## 0. Identify the leaf

From the alert (or from `leaf_events`):

```sh
ssh pi@mockingbird-pi
sqlite3 /home/pi/mockingbird/observations.sqlite <<'SQL'
SELECT leaf, MAX(ts) AS last, datetime(MAX(ts),'unixepoch','localtime') AS last_local
FROM leaf_events GROUP BY leaf ORDER BY last DESC;
SQL
```

The leaf-name format is `mockingbird-XXXXXX` (hex tail of MAC). Cross-reference CLAUDE.md "Current state" for last-seen IP.

## 1. Confirm it's actually silent (not the alert misfiring)

```sh
sqlite3 /home/pi/mockingbird/observations.sqlite \
  "SELECT MAX(ts), datetime(MAX(ts),'unixepoch','localtime') FROM obs WHERE leaf='mockingbird-XXXXXX';"
```

If `MAX(ts)` is recent → alert is wrong; don't chase ghosts.

Also: is the collector itself fine? Check that other leaves are still reporting:

```sh
sqlite3 /home/pi/mockingbird/observations.sqlite \
  "SELECT leaf, COUNT(*) FROM obs WHERE ts > strftime('%s','now','-5 minutes') GROUP BY leaf;"
```

If NOBODY is reporting → this is not a leaf-silent problem, it's [collector-down.md](collector-down.md).

## 2. Network — is the leaf reachable?

From the Pi:

```sh
ping -c 3 mockingbird-XXXXXX.local        # mDNS
# or by last-known IP from CLAUDE.md
ping -c 3 192.168.8.244
```

Also scan the LAN to find its current IP (DHCP may have moved it):

```sh
# install once: sudo apt-get install -y arp-scan
sudo arp-scan --interface=wlan0 192.168.8.0/24 | grep -iE '8c:94:df|30:76:f5'
```

If you see it on the LAN but it's not connecting to `:9001`:

```sh
# from the Pi
curl -s --max-time 3 http://<leaf-ip>/version
```

If `/version` responds, WiFi and HTTP are fine — the TCP collector connection is the issue. Restart the leaf to force a fresh connect:

```sh
curl -s --max-time 5 -X POST http://<leaf-ip>/restart
```

Wait 15 seconds, then re-check the DB. If observations resume, you're done. Note in the postmortem: the leaf's TCP reconnect logic let it hang — file a firmware bug.

## 3. WiFi association — has it dropped off the AP?

If ping fails, check the AP's view of associated stations:

```sh
ssh glinet-new "iwinfo wlan0 assoclist; iwinfo wlan1 assoclist" 2>/dev/null
# the 2.4 GHz radio is the one with Mockingbird SSID; whichever shows it.
```

If the leaf's MAC is NOT in the assoc list → it's lost WiFi. Causes, in order of probability:
- AP rebooted / had a hiccup → leaf will rejoin within ~30 s, just wait.
- Leaf's WiFi credentials are stale (rare; we don't rotate PSKs).
- Leaf crashed and is rebooting (see step 4).
- WiFi interference / range — has anything moved physically near it?

If the leaf's MAC IS in the assoc list but doesn't ping → it's associated but the IP stack is dead. This is a leaf-side crash; either the firmware wedged or BLE-scan code is starving the WiFi task. Goto step 4.

## 4. Soft reboot via OTA / network

ArduinoOTA runs on UDP 3232. If the firmware is still partly alive but unresponsive to HTTP, OTA may still work — but on a wedged leaf, often nothing works.

Try HTTP restart one more time (it's idempotent):

```sh
curl -s --max-time 5 -X POST http://<leaf-ip>/restart || echo "no response"
```

If nothing → physical access.

## 5. Physical — power-cycle the leaf

Find the leaf. They're labelled by hostname (or should be — see "Hardening" below).

- Unplug USB power for 5 seconds.
- Plug back in.
- Watch the onboard LED for the expected boot pattern (firmware-defined).
- From the Pi, watch for the `hello` event:
  ```sh
  journalctl -u mockingbird-collector -f | grep mockingbird-XXXXXX
  ```

If it comes back: done. Note the cause (probably firmware wedge under load).

If it doesn't come back after a power-cycle:

- Hold the BOOT button while plugging in USB (puts it in download mode). If the host Mac sees `/dev/cu.SLAB_USBtoUART`, the chip is alive — the flash is just borked. Reflash via PlatformIO.
- If the Mac doesn't see the serial device at all → CP2102 USB bridge is dead, or USB cable is power-only. Try a different cable first (this catches it ~30% of the time).
- If you've eliminated cable + flash and still nothing → the board is dead. Pull it from the inventory, deploy one of the spare AITRIP boards. Update CLAUDE.md.

## 6. After

- If this leaf has done this more than once → it's flaky. Investigate root cause (heat? power supply? specific BLE advertiser triggering the wedge?). Don't just keep restarting it.
- If the firmware wedge was the cause → file an Ethan ticket with the journal lines and the time window.
- Update CLAUDE.md if the leaf's IP changed or you swapped boards.

## Hardening recommendations (file as MAC-2)

- **Physical labels on every leaf.** Hostname + power-cycle expectations on a label maker tag. The person doing the 3am power-cycle should not need a laptop to know which one to unplug.
- **Firmware watchdog.** ESP32 has hardware task watchdog — confirm it's enabled and that a wedged BLE callback resets the chip within 30 s. Right now we rely on TCP timeout (120s in the collector) which is too slow.
- **Leaf-side TCP reconnect with backoff** — verify firmware doesn't just sit on a half-open socket forever after the collector restarts. The fact that we sometimes need to power-cycle to bring leaves back suggests this is broken.
- **Per-leaf "last seen" panel on the dashboard with staleness colouring** (green <60s, yellow <5m, red >10m). Bia should own this.
