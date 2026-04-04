# AIS Station Monitor

GitHub Actions-based monitoring for the AIS station on Pi4. Runs hourly, connects via Tailscale, and sends Google Chat alerts on failure.

## What it checks

| Check | Method | Fallback |
|-------|--------|----------|
| **Pi4 Health** | Direct HTTP via Tailscale (`/health` endpoint) | — |
| **AISHub** | JSON API (`/station/2387/daily-statistics.json` with `X-Requested-With` header) | — |
| **AIS-catcher** | JSON API → page scrape | Inferred from Pi4 reachability |
| **AISfriends** | JSON API | Inferred from AISHub (same UDP source) |
| **App Errors** | SSH into Pi4, scan Docker logs for errors/exceptions in last hour | — |
| **Tailscale key** | Checks days until expiry | Alerts 7 days before |

AIS-catcher and AISfriends are behind Cloudflare, which blocks API/scrape requests from CI. When blocked, status is inferred: if Pi4 is reachable, AIS-catcher is running. If AISHub is receiving data (UDP), AISfriends is too (same UDP from same process).

## On failure

When any check fails:
- Docker logs from both `ais-ingest` and `ais-catcher` are fetched via Tailscale SSH
- All results + logs are sent as a Google Chat notification

Docker logs are always fetched (even on success) and printed to the GitHub Actions log for debugging.

## Health endpoint thresholds

The Pi4 `/health` endpoint reports `degraded` when:
- Local AIS-catcher data is stale (>30s since last POST)
- AISHub poll is stale (>180s since last successful poll)

A 120s startup grace period prevents false alerts after redeploys.

## App error scanning

The monitor SSHes into the Pi4 and scans `ais-ingest` Docker logs from the last hour for:
- `Error`, `Exception`, `Traceback`, `Failed`
- Excludes the harmless AIS-catcher `recv()` error

This catches R2 upload failures, type errors, API issues, and any unhandled Python exceptions.

## Setup

### Required secrets

| Secret | Description |
|--------|-------------|
| `TS_AUTH_KEY` | Tailscale auth key (ephemeral + reusable) |
| `GOOGLE_CHAT_WEBHOOK` | Google Chat incoming webhook URL |

### Tailscale ACL

The ACL policy needs `tag:ci` defined and an SSH accept rule so the GitHub Action runner can SSH into the Pi4 for Docker logs:

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

### Tailscale SSH on Pi4

Enable Tailscale SSH on the Pi4:

```bash
sudo tailscale set --ssh --accept-risk=lose-ssh
```

### Local testing

```bash
# Without alerts
python3 check.py

# With alerts
GOOGLE_CHAT_WEBHOOK="https://chat.googleapis.com/..." python3 check.py
```

## Related

- [EXTREMOPHILARUM/ais-station](https://github.com/EXTREMOPHILARUM/ais-station) — the ingest pipeline this monitor watches
