# Runbook — pin the Pi's IP on the Opal (DHCP reservation)

**Audience:** the version of you that's on the Mockingbird LAN with five
minutes and wants this done.

**Why:** the Pi is the collector + tailnet subnet router — a single point
of failure for the whole mesh. Leaves resolve `mockingbird-pi.local` via
mDNS with cache invalidation, so a renumber self-heals; the static
reservation is the belt-and-braces safety net (avahi crashes, IGMP
snooping breaks, etc.). See `CLAUDE.md` — Pi LAN line.

**Pre-reqs:**
- On the Mockingbird WiFi (`ssh glinet-new` must work).
- Pi reachable (so we can read the live MAC, not trust this doc).

## Steps

```bash
# 1. Read the Pi's wlan0 MAC from the Pi itself. Don't trust this runbook.
ssh pi@mockingbird-pi 'cat /sys/class/net/wlan0/address'
# expected: b8:27:eb:xx:xx:xx  (Pi Foundation OUI)
#
# Set this in your shell for the next steps:
PI_MAC="b8:27:eb:xx:xx:xx"     # <-- fill in from the line above
PI_IP="192.168.8.202"           # the address we want to pin

# 2. Confirm the lease is currently held by that MAC on the Opal.
ssh glinet-new "cat /tmp/dhcp.leases | awk '\$3==\"$PI_IP\"'"
# expected: a line ending with mockingbird-pi and starting with the MAC
# above. If empty, the Pi isn't currently leased that IP — pick the IP
# it IS leased (column 3 of /tmp/dhcp.leases) or change PI_IP above.

# 3. Add a static host entry to dnsmasq via UCI on the Opal.
ssh glinet-new "
  uci add dhcp host
  uci set dhcp.@host[-1].name='mockingbird-pi'
  uci set dhcp.@host[-1].mac='$PI_MAC'
  uci set dhcp.@host[-1].ip='$PI_IP'
  uci commit dhcp
  /etc/init.d/dnsmasq restart
"

# 4. Verify the reservation took.
ssh glinet-new "uci show dhcp | grep -A1 host"
# expected: the new host stanza with the right MAC and IP

# 5. Force the Pi to renew its lease so we know the reservation is honored
#    (otherwise we won't find out until the next natural renewal).
ssh pi@mockingbird-pi "
  sudo nmcli connection down Mockingbird && sleep 2 && \
  sudo nmcli connection up Mockingbird
"
# This will drop the SSH session briefly. Reconnect after ~10s.

# 6. Confirm the Pi came back on the same IP.
ssh pi@mockingbird-pi 'ip -4 addr show wlan0 | grep inet'
# expected: 192.168.8.202/24 (or whatever PI_IP you chose)
```

## After the change

- Update `CLAUDE.md` Pi LAN line to read
  `static DHCP reservation on the Opal, currently 192.168.8.202`
  and drop the VERIFY comment.
- mDNS remains the canonical discovery path on the leaves — do **not**
  hard-code the IP into leaf firmware. The reservation is a safety net,
  not the primary contract.

## Rollback

```bash
ssh glinet-new "
  # find the index of the host we added
  uci show dhcp | grep 'mockingbird-pi'
  # delete it (replace N with the index from above)
  uci delete dhcp.@host[N]
  uci commit dhcp
  /etc/init.d/dnsmasq restart
"
```

The Pi will pick up a fresh DHCP lease on next renewal. mDNS keeps the
leaves working through the transition.

## Failure modes worth thinking about

- **Two Opals on `192.168.8.0/24` in the lab.** The `HomeNet` Opal is
  on the same subnet shape. If you ever cable through it by mistake,
  `ssh glinet-new` may land on the wrong box. The alias pins port 2222
  (only the Mockingbird Opal runs openssh-server on that port) and
  binds to `en0`, which is the right protection — but if you change
  hosts/cables, re-check `ssh glinet-new 'cat /etc/openwrt_release | head -3'`
  before mutating DHCP.
- **GL.iNet web UI may overwrite UCI on save.** If anyone touches the
  DHCP page in the LuCI / GL.iNet admin UI after this, it can rewrite
  `/etc/config/dhcp` and lose the host entry. The reservation should
  survive a reboot (UCI is persistent), but treat web-UI edits as
  potentially destructive of CLI-side state.

## Why not just static-IP on the Pi?

Considered. Rejected: a static IP on the Pi means an outage if the
Opal's subnet ever changes, and it's invisible to anyone reading the
Opal's lease table. The DHCP reservation keeps the source of truth in
one place (the DHCP server) and self-heals if we ever renumber.
