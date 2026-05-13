# Runbook — Pi SD card filling up

Symptom: alert fires (`df` >70% on `/`), or you see `database or disk is full` in collector logs, or the FS just remounted read-only and everything is broken at once.

Goal: free space, fix the cause, prevent recurrence. Don't reformat anything.

## 1. Triage

```sh
ssh pi@mockingbird-pi
df -h
df -i        # don't forget inodes — a million 1-byte files can fill inode table while df shows 30%
```

Find the biggest offenders fast:

```sh
sudo du -hx --max-depth=1 / 2>/dev/null | sort -hr | head -20
sudo du -hx --max-depth=1 /var 2>/dev/null | sort -hr | head -20
sudo du -hx --max-depth=1 /home/pi 2>/dev/null | sort -hr | head -20
```

The usual suspects on this Pi, ranked:

1. `/var/log/journal/` — systemd journal. Can balloon to gigabytes.
2. `/home/pi/mockingbird/observations.sqlite` (+ `-wal`, `-shm`) — the BLE DB.
3. `/var/cache/apt/archives/` — leftover .deb downloads.
4. `/tmp/` — should be tmpfs, but check.
5. `/home/pi/.cache/`, `~/.local/share/` — python pip cache, etc.

## 2. Quick wins (safe, reversible, do these first)

### Vacuum journald

```sh
sudo journalctl --disk-usage
sudo journalctl --vacuum-size=200M     # keep at most 200MB
# or:
sudo journalctl --vacuum-time=7d       # keep last 7 days
```

### Clear apt cache

```sh
sudo apt-get clean
```

### Drop pip/python caches

```sh
rm -rf /home/pi/.cache/pip
```

These three usually buy back 1–5 GB on this Pi. Re-check `df -h`.

## 3. The collector DB

This is the one that grows monotonically. Inspect:

```sh
ls -lh /home/pi/mockingbird/observations.sqlite*
sqlite3 /home/pi/mockingbird/observations.sqlite \
  "SELECT COUNT(*), MIN(datetime(ts,'unixepoch','localtime')), MAX(datetime(ts,'unixepoch','localtime')) FROM obs;"
```

At ~110 obs/sec the DB grows roughly **4 GB/month**. The 64 GB card has ~50 GB usable after OS — call it ~12 months of raw retention before this becomes the binding constraint. Plan, don't panic.

### If you need to free space NOW

Delete a window. SQLite won't shrink the file on its own — you have to `VACUUM` after delete.

```sh
sudo systemctl stop mockingbird-collector    # CRITICAL: stop writes first
sqlite3 /home/pi/mockingbird/observations.sqlite <<'SQL'
DELETE FROM obs WHERE ts < strftime('%s','now','-30 days');
DELETE FROM leaf_events WHERE ts < strftime('%s','now','-30 days');
SQL
sqlite3 /home/pi/mockingbird/observations.sqlite "VACUUM;"
sudo systemctl start mockingbird-collector
```

`VACUUM` rewrites the whole file → takes ~5 minutes per GB on a Pi Zero W and needs free space equal to the current DB size. If you don't have that free space, you can't VACUUM. In that emergency: `mv observations.sqlite observations.sqlite.bak`, restart collector (creates fresh DB), then VACUUM the backup on a beefier box later.

### Move the WAL/SHM if those are huge

If `*-wal` is many GB, the collector is writing faster than checkpoints. Stop, then:

```sh
sqlite3 /home/pi/mockingbird/observations.sqlite "PRAGMA wal_checkpoint(TRUNCATE);"
```

## 4. If the filesystem went read-only

`dmesg | tail -30` will say `EXT4-fs ... Remounting filesystem read-only`. This usually means **SD card failure**, not "out of space". The Pi will keep "running" but writes silently fail.

Triage:

```sh
sudo dmesg | grep -iE 'ext4|mmcblk|i/o error' | tail
sudo touch /tmp/test && sudo touch /home/pi/test    # second one will fail if FS is RO
```

If RO due to errors: **the card is dying**. Do not try to recover in place. Pull the card, image it on the Mac (`dd if=/dev/diskN of=mockingbird-pi-card.img bs=4M`), then flash a fresh card from the most recent backup. See "SD card swap procedure" in the project memory (TODO: write it).

## 5. Retention strategy recommendation

Right now we keep everything forever and rely on the 64 GB card buying us a year. That's lazy, and it means we don't notice growth until it hurts. Recommend:

- **Daily housekeeping cron (`/etc/cron.daily/mockingbird-housekeeping`)**: `DELETE FROM obs WHERE ts < strftime('%s','now','-90 days')` followed by an incremental vacuum (`PRAGMA incremental_vacuum(...)` — set `PRAGMA auto_vacuum=INCREMENTAL` on the DB first; this is a one-time migration: stop service, set pragma, run `VACUUM` once, restart).
- **Weekly rollup**: aggregate older-than-7-days `obs` into a downsampled `obs_5min` table (mac, leaf, 5-min bucket, count, rssi_min/max/avg). Wanjiru's analytics work fine on this; we'd save ~95% of the rows for long-term trends.
- **Hard cap on journald**: `/etc/systemd/journald.conf` → `SystemMaxUse=500M`. Do this now — it costs nothing.
- **Alert thresholds**: warn at 70% (`/`), page at 85%. With 4 GB/month growth that's ~3 months of "you have time to act" before any actual outage. Compare to the current "you find out when the DB write fails" experience.

## 6. After

- Did the alert fire before the disk became a problem? If you only learned at 95%, your monitoring isn't doing its job. Lower the threshold.
- Write the new failure mode into this runbook if it wasn't here.
- If this was the SD card dying, **document the swap procedure** before next time.

## Don't do

- Don't `rm -rf /var/log` while services are running. Use `journalctl --vacuum-*` or `truncate -s 0 file`.
- Don't delete `*-wal` or `*-shm` while the collector is up — you will corrupt the DB.
- Don't `fstrim` repeatedly on a Pi SD card. Once a week max; SD endurance is real.
