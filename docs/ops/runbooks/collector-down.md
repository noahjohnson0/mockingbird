# Runbook — collector isn't writing observations

You got paged (or noticed the dashboard's gone stale). Goal: confirm whether the collector is down, and get observations flowing again. Target: green in under 5 minutes.

## 0. Get on the box

```sh
ssh pi@mockingbird-pi
# fallback if tailnet is sad:
ssh pi@192.168.8.202
```

If both fail → this isn't a collector problem, it's a Pi problem. Skip to "Pi unreachable" at the bottom.

## 1. Is the service running?

```sh
systemctl status mockingbird-collector
```

Expected: `Active: active (running)` and recent log lines like `obs=NNNN drop=0 hello=...`.

| What you see | What it means | Jump to |
|---|---|---|
| `active (running)`, but no recent `obs=` lines | Service alive, no leaves talking | Step 3 |
| `active (running)`, `obs=` is incrementing | It's fine. Re-check the alarm. | Done. |
| `failed` / `inactive` | Service crashed or stopped | Step 2 |
| `activating (auto-restart)` with rapid restarts | Crash loop | Step 2 |
| Command hangs | systemd or D-Bus stuck | Step 6 |

## 2. Service down / crash-looping

Get the last 100 lines of why it died:

```sh
journalctl -u mockingbird-collector -n 100 --no-pager
```

Common causes (read the traceback, match below):

- **`sqlite3.OperationalError: database is locked`** → another process has the DB open (rare, but a stray `sqlite3` shell will do it). Find and kill:
  ```sh
  sudo fuser /home/pi/mockingbird/observations.sqlite
  ```
- **`sqlite3.OperationalError: disk I/O error` / `database or disk is full`** → SD card full or write-protected. Run `df -h /home/pi`. If >95% full, go to [disk-pressure.md](disk-pressure.md). If the SD card has flipped to read-only (Linux dmesg will show `EXT4-fs (mmcblk0p2): Remounting filesystem read-only`), the card is dying — do not write more to it, plan an SD swap.
- **`MemoryError` / killed by OOM** → check `dmesg | tail -50` for `oom-kill`. Tighten dashboard's MemoryMax, restart.
- **`PermissionError` on `/home/pi/mockingbird/`** → directory perms changed. Fix: `sudo chown -R pi:pi /home/pi/mockingbird`.
- **Python import error** → someone edited the script. `git -C ~/mockingbird status`, revert.

Try a manual restart:

```sh
sudo systemctl restart mockingbird-collector
sleep 5
systemctl status mockingbird-collector
```

If it stays up for 30 seconds and logs `obs=` ticking, you're done. Document the cause in the postmortem.

## 3. Service alive, no observations

Confirm leaves can reach the port:

```sh
sudo ss -tlnp | grep 9001
```

Expected: `LISTEN 0 ... 0.0.0.0:9001 ... python3`.

If not listening: it failed to bind (port in use). `sudo ss -tlnp | grep python3` and kill the stray process, then restart.

Check active connections from leaves:

```sh
sudo ss -tnp | grep :9001
```

Expected: several `ESTAB` lines from `192.168.8.x` peers. If zero, the leaves can't reach the Pi. Try from the Pi outward:

```sh
ping -c 3 192.168.8.244   # mockingbird-4ce184, adjust per CLAUDE.md inventory
```

If ping fails to all leaves → WiFi side problem (the Opal AP, not the collector). Investigate `mockingbird` SSID:

```sh
iwgetid                                 # confirm Pi is associated to "Mockingbird"
sudo wpa_cli -i wlan0 status            # signal level, BSSID
```

If only some leaves are missing → that's [leaf-silent.md](leaf-silent.md), not this runbook.

## 4. Service alive, connections present, but DB isn't growing

```sh
sqlite3 /home/pi/mockingbird/observations.sqlite \
  "SELECT MAX(ts) AS last, datetime(MAX(ts),'unixepoch','localtime') FROM obs;"
```

If `last` is fresh (within the last 10 s) → it IS writing; the dashboard or alert is lying. Investigate the alert.

If `last` is stale → check `drop=` counter in the journal:

```sh
journalctl -u mockingbird-collector --since "5 min ago" | tail -30
```

High `drop` rate means the leaves are sending malformed JSON (firmware bug) or oversized lines (`MAX_LINE=1024` exceeded). Read a few lines raw to diagnose:

```sh
sudo tcpdump -i wlan0 -A -s0 'tcp port 9001 and tcp[tcpflags] & tcp-push != 0' -c 20
```

## 5. Nuclear option (safe)

If you can't pinpoint it and the network is dark:

```sh
sudo systemctl restart mockingbird-collector
sudo systemctl restart mockingbird-dashboard
```

Restarting is cheap. Leaves will reconnect within ~5 s. No data is corrupted — SQLite WAL handles it.

If that doesn't help, reboot the Pi:

```sh
sudo reboot
# wait 60 s, then:
ssh pi@mockingbird-pi 'systemctl status mockingbird-collector'
```

## 6. Pi unreachable

- Try LAN IP `ssh pi@192.168.8.202` (or scan: from Mac, `arp -a | grep 192.168.8`).
- Try tailnet: `ssh pi@mockingbird-pi`.
- If both fail, get physical: USB-gadget serial console — `screen /dev/cu.usbmodem* 115200` after plugging the Pi into the Mac via the data USB port. See CLAUDE.md "Reach paths".
- If still nothing: power-cycle. Pull and reseat USB power. Wait 90 s. Try again.

## After you fix it

1. Note start/end time of the outage.
2. Open a postmortem stub in `docs/ops/postmortems/YYYY-MM-DD-collector.md`.
3. Did the alert fire? Was it useful? File the gap.
4. Was the fix in this runbook? If not, **add the new failure mode** before you log off. The next 3am-you will thank you.

## Known gotchas

- The Pi Zero W's single ARMv6 core is the bottleneck under load. If `obs/sec` is climbing and CPU is pinned (`top` shows python3 at >90%), this is capacity, not bugs — see monitoring-plan.md.
- WAL mode means `observations.sqlite-wal` can grow to tens of MB during heavy ingest. It checkpoints automatically. Do NOT delete `*-wal` or `*-shm` files manually unless the collector is stopped.
