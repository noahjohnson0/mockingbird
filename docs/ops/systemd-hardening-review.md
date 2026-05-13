# systemd hardening review — mockingbird Pi services

Author: Macca · Scope: services running on `mockingbird-pi` (Pi Zero W, 512 MB RAM, 64 GB SD)

We ship two systemd units today:

- `services/mockingbird-collector.service` — TCP ingest on `:9001`, writes SQLite
- `services/mockingbird-dashboard.service` — HTTP dashboard server

Both run as `pi:pi`, `Type=simple`, `Restart=always`, `RestartSec=3`, journal logging.

## Operational impact if these go down

- **Collector down** → silent data loss. Leaves are streaming over TCP; on `ECONNREFUSED` they buffer briefly (64-entry queue) and drop. No persisted backlog anywhere. Every second of collector downtime = ~110 lost observations at current rates.
- **Dashboard down** → nobody dies, but the only "is the network alive" window goes dark. Lower priority.

## Audit — `mockingbird-collector.service`

| Concern | Current | Verdict | Recommendation |
|---|---|---|---|
| `Restart=` | `always` | OK | Keep. Crash → quick respawn is right for ingest. |
| `RestartSec=` | `3` | OK | Keep. Fast enough that leaves' TCP reconnect backoff catches up. |
| `StartLimitBurst` / `StartLimitIntervalSec` | **unset** | **Smell** | Add `StartLimitBurst=10` / `StartLimitIntervalSec=120` at `[Unit]`. Without this, a crash-loop hammers systemd forever and we never get paged — restart-as-feature, not bug. |
| `MemoryHigh` / `MemoryMax` | `128M` / `192M` | Mostly OK | Fine for steady-state (~30 MB). But `MemoryMax=192M` on a 512 MB Pi alongside Tailscale + dashboard + journald leaves slim margins. See risk #1 below. |
| `CPUQuota` | unset | OK for now | Pi Zero W is single-core; quota'ing the collector makes ingest worse. Leave open. |
| `TasksMax` | unset | Minor | Default is high. Add `TasksMax=64` — asyncio shouldn't spawn that many; if it does, something's wrong. |
| Graceful shutdown | **not handled** | **Smell** | `Type=simple` + asyncio + no `KillSignal` override means SIGTERM yanks the process mid-INSERT. WAL survives but the in-flight line is lost. Add `KillSignal=SIGINT` so `asyncio.run` cleans up, and add `TimeoutStopSec=10`. Also: the code's `KeyboardInterrupt` handler catches SIGINT — pair them. |
| `ExecReload` | unset | OK | Not stateful enough to need it. |
| Log destination | `journal` | OK | journald compresses + rotates. Confirm `SystemMaxUse=` is set globally (see disk-pressure runbook). |
| Dependencies | `After/Wants=network-online.target` | **Incomplete** | The service writes to `~/mockingbird/observations.sqlite` on the SD card. Add `After=local-fs.target` and `RequiresMountsFor=/home/pi/mockingbird` (belt + braces). Also: if WiFi flaps, `network-online.target` doesn't re-fire — the asyncio server keeps its socket so this is fine, but document it. |
| Hardening (sandbox) | none | **Smell** | This is exposed on `0.0.0.0:9001` to the LAN. Add: `NoNewPrivileges=true`, `ProtectSystem=full`, `ProtectHome=read-only` won't work (we write under `/home/pi/mockingbird`) so use `ReadWritePaths=/home/pi/mockingbird`, `PrivateTmp=true`, `ProtectKernelTunables=true`, `ProtectControlGroups=true`, `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`. Low cost, real defence-in-depth. |
| `WatchdogSec` | unset | Nice-to-have | If we add `sd_notify` heartbeat from the asyncio loop, set `WatchdogSec=60`. Today, a wedged-but-alive process (e.g. SQLite blocked on fsync) would not be detected by systemd. Defer until we see it bite. |
| `OOMScoreAdjust` | unset | **Add** | This service matters more than the dashboard. Set `OOMScoreAdjust=-500` on collector and `OOMScoreAdjust=500` on dashboard so the kernel kills the right thing under pressure. |

## Audit — `mockingbird-dashboard.service`

| Concern | Current | Verdict | Recommendation |
|---|---|---|---|
| `Restart=`, `RestartSec=` | always / 3 | OK | Keep. |
| `StartLimitBurst` | unset | Smell | Same fix — `StartLimitBurst=5` / `StartLimitIntervalSec=300`. Dashboard crash loop should escalate, not hide. |
| `MemoryMax` | `96M` | Generous | Static HTTP server doesn't need 96 MB. Tighten to `MemoryHigh=32M` / `MemoryMax=64M` — frees headroom for the collector. |
| Dependency on collector | `After=mockingbird-collector.service` | **Wrong knob** | `After=` only orders; it does not make the dashboard fail if the collector isn't up. That's actually what we want here — the dashboard should still serve a "collector down" banner. Keep `After=`, do NOT add `Requires=`. But: add `BindsTo=` is wrong too. Document the choice. |
| Graceful shutdown | not handled | Same as collector | Same fix. |
| Hardening | none | Same | Apply same sandbox flags. Dashboard probably doesn't need any write paths — set `ProtectSystem=strict` and only `ReadOnlyPaths=/home/pi`. |
| Port binding | unknown | TBD | If it binds privileged port (<1024), use `AmbientCapabilities=CAP_NET_BIND_SERVICE` rather than running as root. Verify in the script. |
| `OOMScoreAdjust` | unset | **Add** | `+500` — sacrifice the dashboard before the collector. |

## Proposed final unit — collector

```ini
[Unit]
Description=Mockingbird BLE observation collector
After=network-online.target local-fs.target
Wants=network-online.target
RequiresMountsFor=/home/pi/mockingbird
StartLimitBurst=10
StartLimitIntervalSec=120

[Service]
Type=simple
ExecStart=/usr/bin/python3 /home/pi/mockingbird-collector.py
Restart=always
RestartSec=3
User=pi
Group=pi
KillSignal=SIGINT
TimeoutStopSec=10
TasksMax=64
MemoryHigh=128M
MemoryMax=192M
OOMScoreAdjust=-500
NoNewPrivileges=true
ProtectSystem=full
ReadWritePaths=/home/pi/mockingbird
PrivateTmp=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

## Proposed final unit — dashboard

```ini
[Unit]
Description=Mockingbird 3D room dashboard (Three.js HTTP server)
After=network-online.target mockingbird-collector.service
Wants=network-online.target
StartLimitBurst=5
StartLimitIntervalSec=300

[Service]
Type=simple
ExecStart=/usr/bin/python3 /home/pi/mockingbird-dashboard.py
Restart=always
RestartSec=3
User=pi
Group=pi
KillSignal=SIGINT
TimeoutStopSec=10
TasksMax=32
MemoryHigh=32M
MemoryMax=64M
OOMScoreAdjust=500
NoNewPrivileges=true
ProtectSystem=strict
ReadOnlyPaths=/home/pi
PrivateTmp=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

## Application-side follow-ups (not systemd, but related)

1. **Collector: handle SIGTERM/SIGINT cleanly.** Wrap `asyncio.run(main())` with a signal handler that closes the server, drains in-flight readers (small budget — 2s), commits the WAL, then exits. Right now we rely on Python's interpreter shutdown order. Cheap fix, real safety.
2. **Collector: `db.commit()` is implicit (`isolation_level=None`) but the cursor still holds open transactions briefly under load.** Confirm `PRAGMA synchronous=NORMAL` is what we want; on a Pi with no UPS, `FULL` would be safer but ~3× slower. NORMAL + WAL is the right trade for this stage — document it.
3. **No `sd_notify` integration.** Defer; revisit when we add `WatchdogSec`.

## Rollback plan

These are systemd unit edits. Rollback = `git revert` + `sudo systemctl daemon-reload && sudo systemctl restart mockingbird-collector mockingbird-dashboard`. No data migration. The `deploy-collector.sh` script copies these files into place — make sure it does a `daemon-reload` after.
