# AIS Stack Monitor

GitHub Actions-based monitoring for the AIS stack, which spans **two hosts**.
Runs hourly, connects via Tailscale, and sends Google Chat alerts on failure.

| Host | Tailscale | Runs |
|------|-----------|------|
| **pi5** | `100.69.37.64` | `ais-catcher` (RTL-SDR decode), `ais-ingest`, `ais-autoheal` |
| **invoicebuddy** | `100.81.76.63` | `ais-api`, `ais-postgres`, `ais-redis`, celery worker/beat, `ais-cloudflared` |

> **Note:** as of **2026-08-16** the miniserver is out of the data path and is
> no longer monitored. `ais-api` moved to invoicebuddy (restored from the
> Backblaze B2 backups) and the RTL-SDR moved from the miniserver to a new
> Pi 5. Before that the whole stack was consolidated on the miniserver; before
> April 2026 the receiver was a Pi 4.

## What it checks

| Check | Method | Fallback |
|-------|--------|----------|
| **ais-ingest** | Direct HTTP via Tailscale (`http://100.69.37.64:9123/health`) — pi5 | — |
| **ais-api (public)** | HTTPS to `https://api.saurabhn.com/health` — exercises Cloudflare → cloudflared → ais-api → Postgres | Direct tailnet hit at `http://100.81.76.63:9200/health` to separate a tunnel fault from an app fault |
| **AISHub** | JSON API (`/station/2387/daily-statistics.json`) | — |
| **AIS-catcher** | JSON API → page scrape | Inferred from `ais-ingest` reachability |
| **AISfriends** | JSON API | Inferred from AISHub (same UDP source) |
| **App Errors** | SSH into **both** hosts, scan Docker logs (`ais-catcher`, `ais-ingest` on pi5; `ais-api`, `ais-celery-worker` on invoicebuddy) for errors in last hour | Unreachable host reported as `unknown` |
| **Tailscale key** | Days until expiry | Alerts 7 days before |

AIS-catcher and AISfriends are behind Cloudflare, which blocks API/scrape
requests from CI. When blocked, status is inferred: if `ais-ingest` is
reachable, the catcher is running. If AISHub is receiving data (UDP),
AISfriends is too (same UDP from the same process).

## On failure

When any check fails:
- Recent Docker logs from all four monitored containers, across both hosts,
  are fetched via Tailscale SSH (labelled `host/container`)
- All results + logs are sent as a Google Chat notification

Docker logs are always fetched (even on success) and printed to the
GitHub Actions log for debugging.

## Health endpoint thresholds

- `ais-ingest` `/health` reports `degraded` if local radio data is stale
  (> 30 s since last AIS-catcher POST). 120 s startup grace period.
- `ais-api` `/health` reports `degraded` if the Postgres pool is
  unreachable or `last_ingest_age_s` is too high (> ~5 min — the writer
  hasn't received a POST recently).

## App error scanning

The monitor SSHes into **both** hosts and scans `docker logs --since 1h` for
*structured* log-level events — the space-delimited ` ERROR `/` CRITICAL `/
` FATAL ` field — rather than any line containing the word "error". That way a
single `logger.exception` yields one matched line instead of leaking traceback
fragments. Excluded as known-benign:

- `forwarder send failed` — the ingest outbox retrying with backoff while
  ais-api restarts; expected on every redeploy, and ais-api reachability has
  its own check.

`ais-celery-worker` is scanned because the nightly Postgres backup to
Backblaze B2 runs there — a failing backup would otherwise be silent.

This catches Postgres failures, backup failures, unhandled exceptions and
similar. If a host cannot be reached the check reports `unknown` and alerts;
it never reports healthy for a host it could not scan.

## Setup

### Required secrets

| Secret | Description |
|--------|-------------|
| `TS_AUTH_KEY` | Tailscale auth key (ephemeral + reusable) |
| `GOOGLE_CHAT_WEBHOOK` | Google Chat incoming webhook URL |

### Tailscale ACL

The ACL needs `tag:ci` plus an SSH accept rule so the GitHub runner can SSH
into both hosts for Docker log scraping. `dst: autogroup:self` covers every
device this account owns, so adding hosts needs no ACL change:

```jsonc
"tagOwners": {
    "tag:ci": ["autogroup:admin"],
},

"ssh": [
    // ... existing rules ...
    {
        "action": "accept",
        "src":    ["tag:ci"],
        "dst":    ["autogroup:self"],
        "users":  ["extremo"],
    },
],
```

### Tailscale SSH on each host

Every monitored host needs Tailscale SSH enabled, or its log scan fails:

```bash
sudo tailscale set --ssh
```

Check with `tailscale debug prefs | grep RunSSH`. Note that running this while
connected over Tailscale drops the current session (it reroutes port 22 to
Tailscale SSH) — it will refuse unless you pass `--accept-risk=lose-ssh`. Make
sure you have another way in before doing that on a remote host.

### Local testing

```bash
# Without alerts
python3 check.py

# With alerts
GOOGLE_CHAT_WEBHOOK="https://chat.googleapis.com/..." python3 check.py
```

## Related

- [EXTREMOPHILARUM/ais-station](https://github.com/EXTREMOPHILARUM/ais-station) — local radio decode + forwarder (runs on pi5)
- [EXTREMOPHILARUM/ais-api](https://github.com/EXTREMOPHILARUM/ais-api) — API + Postgres + AISHub poller (runs on invoicebuddy)
- [EXTREMOPHILARUM/ais-api](https://github.com/EXTREMOPHILARUM/ais-api) — FastAPI + Postgres serve layer
