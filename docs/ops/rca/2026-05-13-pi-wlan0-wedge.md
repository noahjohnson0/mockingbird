# RCA — Pi Zero W wlan0 silent wedge

- **Date:** 2026-05-13
- **Author:** Macca (SRE)
- **Ticket:** MAC-3
- **Severity:** S2 — single point of failure (the Pi is the collector and the
  tailnet subnet router; when it's off-net, the whole mesh is dark)
- **Status:** Mitigation shipping in branch `mac/wlan0-wedge-fix`. Residual
  risk remains — see "Residual risk" below.

## Symptom

The Pi Zero W (`mockingbird-pi`, 192.168.8.202) silently loses its WiFi
association to the `Mockingbird` SSID after some hours of uptime. From the
network's perspective:

- The Pi stops answering pings on its LAN IP and on its Tailscale IP.
- The collector (TCP :9001) stops accepting new leaf connections; existing
  leaf TCP sessions go silent and the leaves' heartbeats stop landing in
  `leaf_events`.
- No graceful failover — the leaves keep streaming into a dead socket
  until their own keepalive trips.

`wlan0` itself doesn't come back without intervention. A power cycle
(physically pulling and re-applying USB power) restores it. A graceful
`sudo systemctl reboot` would presumably also work but isn't reachable
once the link is gone — see "Why this is bad" below.

## Why this is bad

The Pi has no out-of-band management path on the LAN. We can reach it
over Tailscale, but the Tailscale daemon also runs on the Pi — when
`wlan0` wedges, Tailscale goes with it. The only ways back are:

1. USB-gadget serial console (requires physical access)
2. Power cycle (also requires physical access)

So an unattended wedge today = a hard outage until someone walks to the
Pi. Unacceptable for what is structurally a single point of failure for
the entire mesh.

## Evidence

Pulled today (2026-05-13) from a live `journalctl`/`dmesg`/`nmcli`
session on the Pi:

```
$ dmesg | grep -i brcmfmac | tail
brcmfmac: brcmf_cfg80211_set_power_mgmt: power save enabled

$ nmcli connection show Mockingbird | grep -i powersave
802-11-wireless.powersave:              0 (default)

$ cat /sys/module/brcmfmac/parameters/roamoff
(empty — driver default, roamoff=0)

$ dmesg | grep -i 'wl0\|brcmfmac' | head
brcmfmac: F/W version: wl0: Jul 19 2021 7.45.98 (TOB)

$ journalctl --list-boots
 0 <id> ...current boot...
(only one boot listed — the journal is volatile, prior boots' evidence is gone)
```

## What this is

Classic `brcmfmac` power-save wedge on the **BCM43430A1** radio in the Pi
Zero W. It's documented across the Raspberry Pi forums, the linux-wireless
list, and the Pi OS bug tracker going back years. The failure mode:

1. NetworkManager leaves `wifi.powersave` at "default" (value `0` in the
   nmcli output means "use driver/global default", *not* "off").
2. The driver default for brcmfmac is **power save ON**.
3. With PS on, the chip puts the radio into a low-power state during
   idle. The BCM43430 firmware (`7.45.98 (TOB)`, vintage 2021) has a
   bug where, on certain beacon-loss / scan-while-PS-active corner cases,
   the firmware's internal state machine wedges. The driver doesn't
   notice — the netdev stays "UP, associated" from the kernel's
   perspective — but no packets move in either direction.
4. Because the kernel thinks the link is fine, NetworkManager doesn't
   trigger a reconnect. `wpa_supplicant` doesn't see a deauth. Nothing
   self-heals.

The two well-known knobs that reduce the trigger rate are:

- Turn power-save OFF (eliminates the PS-active state where the bug lives).
- Set `roamoff=1` on `brcmfmac` (firmware-initiated roaming is part of
  several reported wedge sequences; we don't roam — there's one AP — so
  there's no cost to disabling it).

We also can't *trust* either fully — the underlying firmware is closed
and the bug isn't fully characterized. That's why this fix is layered
defense, not a single switch.

## Fix layers

1. **Persistently disable WiFi power save** via NetworkManager drop-in
   (`/etc/NetworkManager/conf.d/wifi-powersave.conf`, `wifi.powersave = 2`).
   This is the primary fix and addresses the root cause as best as a
   userspace knob can.
2. **`brcmfmac` module options** (`/etc/modprobe.d/brcmfmac.conf`):
   `roamoff=1 feature_disable=0x82000`. `roamoff=1` disables firmware
   roaming (we have one AP, so this only removes a wedge trigger).
   `feature_disable=0x82000` masks out P2P and TDLS features that show
   up in several wedge traces on this firmware.
3. **Userspace watchdog** — a 60s systemd timer pings the Opal
   (`192.168.8.1`); after 3 consecutive failures it `nmcli connection up`s
   the Mockingbird profile; after 5 it cycles the radio; after 8 it
   reboots. This is the safety net for "the layered fixes didn't catch
   every case." See `services/pi/wlan-watchdog.sh`.
4. **Persistent journal** — `/run/log/journal/` is volatile, so the
   evidence of the last wedge is gone the moment the Pi reboots. Moving
   the journal to `/var/log/journal/` so post-mortem evidence survives
   reboots. Idempotent installer at
   `services/pi/install-persistent-journal.sh`.

## Why this set of layers

- Layer 1 attacks the root cause (PS on → bug reachable).
- Layer 2 reduces remaining trigger surface from the bits of firmware
  behaviour we can't disable with a userspace setting.
- Layer 3 buys us auto-recovery in the worst case where 1 and 2 still
  miss something. The reboot at 8 minutes is intentionally conservative
  — we'd rather drop ~8 minutes of collector data than be locked out
  for hours.
- Layer 4 makes the *next* wedge investigatable instead of a guessing
  game.

## Residual risk

- **The Pi is still a single point of failure.** This fix makes the
  wedge less likely and self-healing when it does happen; it doesn't
  remove the SPOF. Long-term: collector HA, or at least a
  "warm-spare-on-an-SD-card" runbook.
- **The watchdog can mask a real outage.** If the Opal goes down,
  the watchdog will spin: reconnect → cycle radio → reboot. That's
  arguably fine (cheap, idempotent, will recover when the Opal comes
  back) but worth knowing. The journal entries will make this obvious
  after the fact.
- **8-minute reboot threshold drops collector data.** Acceptable. Leaves
  buffer locally (64-entry queue) and reconnect on their own; we lose
  the buffer overflow worth of observations, not the leaves themselves.
- **`feature_disable` is a bitmask documented by reading kernel source.**
  If we upgrade the kernel and the bit meanings shift, this could become
  a no-op or worse. Watch for kernel bumps and re-verify with
  `modinfo brcmfmac`.
- **Firmware-level fix is upstream's problem.** A newer BCM43430
  firmware blob may eventually ship in `firmware-brcm80211` that fixes
  the wedge directly. Worth checking after each `apt upgrade`.

## Verification

After deploying the fix:

```sh
ssh pi@mockingbird-pi
iw dev wlan0 get power_save    # expect: "Power save: off"
cat /sys/module/brcmfmac/parameters/roamoff   # expect: 1
systemctl list-timers | grep mockingbird-wlan # expect: timer scheduled
journalctl --list-boots                       # expect: more than 1 boot
```

## Deploy

From the Mac, with the Pi reachable:

```sh
bash scripts/deploy-pi-wlan-fix.sh
```

This will briefly drop SSH when it bounces the Mockingbird connection
to apply the new powersave setting. Reconnect after ~10s.

## Related

- See [`docs/ops/runbooks/leaf-silent.md`](../runbooks/leaf-silent.md)
  for the "is the Pi or the leaf the silent one" triage.
- See `CLAUDE.md` gotcha list for the broader brcmfmac context.
