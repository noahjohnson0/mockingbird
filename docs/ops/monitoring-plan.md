# mockingbird monitoring plan

Author: Macca · Stage: 8 leaves, one Pi, one Opal, one human operator (Noah). No SLO contracts. No customers. Real production data, but recoverable.

## TL;DR — what we'll do

A handful of cron'd shell scripts on the Pi that check four things, post to **ntfy.sh** when something's wrong, and that's it. No Prometheus. No Grafana. No node_exporter. Total disk footprint: <1 MB. Total memory at runtime: ~0 (cron spawns, runs, exits).

When the project gets to ~50 leaves, multiple sites, or someone other than Noah depending on it, we revisit and probably move to Prometheus + a self-hosted Grafana on a beefier box (NOT the Pi Zero W — it would not be a good citizen of 512 MB RAM).

## Why not Prometheus + Grafana now

I like Prometheus. I've run it at scale. It is the wrong tool for this stage:

- **The Pi Zero W has 512 MB RAM** and is already running tailscaled (~40 MB), the collector (~30 MB), and the dashboard. Prometheus's TSDB plus node_exporter plus a scrape target eats 150–300 MB on a quiet day and goes higher with retention. We'd be monitoring the Pi by using up its RAM.
- **We have one operator.** A dashboard is only useful if someone looks at it; alerts are useful if they wake the right person. Noah has a phone. ntfy.sh → phone is the simplest viable alerting path.
- **The cardinality is laughable.** 8 leaves + 1 Pi + 1 Opal = ~10 hosts, maybe 20 series we'd actually care about. Prometheus's value proposition starts at ~100s of series.
- **Setup cost is real.** A weekend to deploy + tune Prometheus + Grafana + AlertManager + ntfy bridges. Versus an afternoon for the cron approach. We're not at a stage where the difference pays back.

When we DO switch: signal will be us actually wanting historical graphs we can't get from `sqlite3` ad-hoc, or wanting multi-host correlation that crontab notifications can't give. Until then — over-engineering.

## What to monitor

Order = priority. Each item: what, why, how, threshold, where it goes.

### 1. Collector ingest rate (CRITICAL)
- **What**: rows inserted into `obs` in the last 60 seconds.
- **Why**: this is the heart. If it's zero, the whole network is silently failing.
- **How**: `sqlite3 observations.sqlite "SELECT COUNT(*) FROM obs WHERE ts > strftime('%s','now','-60 seconds');"`
- **Threshold**: warn if <10 over 5 minutes (degraded), page if 0 over 5 minutes (down).
- **Routes to**: ntfy.sh `mockingbird-critical` topic → Noah's phone with sound.
- **Runbook**: [collector-down.md](runbooks/collector-down.md)

### 2. Per-leaf heartbeat freshness (HIGH)
- **What**: each known leaf's `MAX(ts)` in `leaf_events`.
- **Why**: catches one-leaf-down before it shows up as a coverage hole.
- **How**: SQL groups by leaf; alert if any leaf has been silent > 10 min.
- **Threshold**: 10 min silence.
- **Routes to**: ntfy.sh `mockingbird-warn` topic, no sound.
- **Runbook**: [leaf-silent.md](runbooks/leaf-silent.md)

### 3. Disk usage on the Pi (HIGH)
- **What**: `df --output=pcent /` and SQLite DB file size growth.
- **Why**: SD card death and disk-full are the second-most-common Pi outage cause (after WiFi flakes).
- **How**: shell, daily.
- **Threshold**: warn at 70%, page at 85%. Also: alert if DB growth in last 24h is >2× the trailing 7-day average (something's making us write a lot more).
- **Routes to**: `mockingbird-warn` for 70%, `mockingbird-critical` for 85%.
- **Runbook**: [disk-pressure.md](runbooks/disk-pressure.md)

### 4. Pi reachability (CRITICAL)
- **What**: Pi is reachable on the LAN AND on the tailnet.
- **Why**: if you can't SSH in to fix anything else, you can't fix anything.
- **How**: this check runs *from somewhere that isn't the Pi* — the Mac, ideally, or any tailnet peer. A simple cron on the Mac (`launchd` plist for proper persistence) that does `ping -c 2 mockingbird-pi` every 5 min and posts to ntfy on 3 consecutive failures.
- **Threshold**: 3 consecutive failures (15 min) — page.
- **Routes to**: `mockingbird-critical`.
- **Runbook**: TODO — `pi-unreachable.md`. Until then: "go look at it physically."

### 5. Service health (MEDIUM)
- **What**: `systemctl is-active mockingbird-collector mockingbird-dashboard`.
- **Why**: belt + braces; the ingest-rate check should catch collector failures, but this catches "running but wedged" and dashboard outages.
- **How**: cron on the Pi.
- **Threshold**: not `active`.
- **Routes to**: `mockingbird-warn`.

### 6. Heap-free trend on leaves (LOW, future)
- **What**: include `free_heap` in the `hb` JSON, log to DB, alert on sustained downward trend.
- **Why**: heap fragmentation on the WROOM-32 is a known risk; we want lead time on memory pressure.
- **How**: requires firmware change. Defer until Ethan ships it.

### 7. WiFi association count on the Opal (LOW, future)
- **What**: `iwinfo wlan0 assoclist | wc -l` should equal known leaf count + 1 (Pi).
- **Why**: if a leaf drops the AP we want to know before its heartbeat times out.
- **How**: SSH from Pi to Opal once a minute.
- **Threshold**: drop > 1 from expected.
- **Defer** until step 2 is proven not noisy enough.

## Implementation sketch

`/usr/local/bin/mockingbird-healthcheck.sh` on the Pi:

```sh
#!/bin/sh
# Posted as /etc/cron.d/mockingbird-healthcheck: */1 * * * * pi /usr/local/bin/mockingbird-healthcheck.sh
set -eu

NTFY_TOPIC_CRIT="mockingbird-critical"
NTFY_TOPIC_WARN="mockingbird-warn"
DB=/home/pi/mockingbird/observations.sqlite
STATE_DIR=/var/lib/mockingbird-monitor
mkdir -p "$STATE_DIR"

notify() {
    # $1=topic $2=priority $3=title $4=message
    curl -sS --max-time 5 \
        -H "Title: $3" \
        -H "Priority: $2" \
        -H "Tags: warning" \
        -d "$4" \
        "https://ntfy.sh/$1" >/dev/null || true
}

# Dedup: only fire once per condition until it clears.
fire_once() {
    local key="$1"; shift
    local flag="$STATE_DIR/$key"
    if [ ! -f "$flag" ]; then
        touch "$flag"
        notify "$@"
    fi
}
clear_flag() { rm -f "$STATE_DIR/$1"; }

# 1. Ingest rate
COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM obs WHERE ts > strftime('%s','now','-300 seconds');")
if [ "$COUNT" -lt 1 ]; then
    fire_once ingest_zero "$NTFY_TOPIC_CRIT" urgent "mockingbird: collector silent" \
        "Zero observations in last 5 min. See docs/ops/runbooks/collector-down.md"
else
    clear_flag ingest_zero
fi

# 2. Disk
PCT=$(df --output=pcent / | tail -1 | tr -dc 0-9)
if [ "$PCT" -ge 85 ]; then
    fire_once disk_critical "$NTFY_TOPIC_CRIT" urgent "mockingbird: disk >=85%" \
        "df=$PCT%. See runbooks/disk-pressure.md"
elif [ "$PCT" -ge 70 ]; then
    fire_once disk_warn "$NTFY_TOPIC_WARN" default "mockingbird: disk >=70%" \
        "df=$PCT%."
else
    clear_flag disk_critical; clear_flag disk_warn
fi

# 3. Services
for svc in mockingbird-collector mockingbird-dashboard; do
    if ! systemctl is-active --quiet "$svc"; then
        fire_once "svc_$svc" "$NTFY_TOPIC_WARN" high "mockingbird: $svc not active" \
            "$(systemctl status $svc --no-pager | head -10)"
    else
        clear_flag "svc_$svc"
    fi
done

# 4. Per-leaf staleness (every 5 min is enough)
MINUTE=$(date +%M)
if [ "$((10#$MINUTE % 5))" -eq 0 ]; then
    STALE=$(sqlite3 "$DB" "
        SELECT leaf FROM leaf_events
        GROUP BY leaf
        HAVING MAX(ts) < strftime('%s','now','-600 seconds');
    ")
    if [ -n "$STALE" ]; then
        fire_once leaf_stale "$NTFY_TOPIC_WARN" default "mockingbird: silent leaves" \
            "Leaves with no events in 10+ min: $STALE"
    else
        clear_flag leaf_stale
    fi
fi
```

`mockingbird-pi-reachable.sh` on the Mac (LaunchAgent, every 5 min):

```sh
#!/bin/sh
COUNTFILE=/tmp/mockingbird-pi-down-count
if ! ping -c 2 -W 2 mockingbird-pi >/dev/null 2>&1; then
    COUNT=$(( $(cat $COUNTFILE 2>/dev/null || echo 0) + 1 ))
    echo $COUNT > $COUNTFILE
    if [ $COUNT -ge 3 ]; then
        curl -sS -H "Priority: urgent" -H "Title: mockingbird-pi unreachable" \
            -d "3 consecutive pings failed" https://ntfy.sh/mockingbird-critical
    fi
else
    rm -f $COUNTFILE
fi
```

## Alert routing

- ntfy.sh topic `mockingbird-critical` → phone push with sound.
- ntfy.sh topic `mockingbird-warn` → phone push, silent.
- Topic names should be guessable-but-not-trivial; ntfy.sh is unauthenticated by default. Use long random suffixes in the real config (`mockingbird-critical-<random>`). Treat the topic name as a weak secret.

## What we explicitly are NOT doing yet

- No metric scrape. No time-series store. No PromQL. No Grafana.
- No SLOs. No error budget. (We don't have customers; SLOs would be cargo-culting.)
- No PagerDuty. ntfy.sh costs $0 and Noah is the on-call. When there's a rotation, we revisit.
- No log shipping. journald on the Pi + `journalctl` is fine for the scale.

## When to graduate to "real" monitoring

Trigger any one:

- 25+ leaves (cardinality grows past what ad-hoc SQL is pleasant for).
- Two+ people on call (need a real router, not "Noah's phone").
- We want historical dashboards that don't come from `sqlite3` queries.
- We add a second site (multi-host correlation gets painful with crons).

Until then: cron + ntfy is the right amount of tool for the job.
