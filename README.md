# AIS Stack Monitor

GitHub Actions-based monitoring for the AIS stack on the **miniserver**
(Tailscale `100.86.157.26`). Runs hourly, connects via Tailscale, and sends
Google Chat alerts on failure.

> **Note:** The Pi4 was retired in April 2026 after the RTL-SDR was moved
> to the miniserver. All checks now target the consolidated miniserver
> stack: `ais-catcher`, `ais-ingest`, `ais-api`, `ais-postgres`,
> `ais-cloudflared`.

## What it checks

| Check | Method | Fallback |
|-------|--------|----------|
| **ais-ingest** | Direct HTTP via Tailscale (`http://100.86.157.26:9123/health`) | — |
| **ais-api (public)** | HTTPS to `https://api.saurabhn.com/health` — exercises Cloudflare → cloudflared → ais-api → Postgres | — |
| **AISHub** | JSON API (`/station/2387/daily-statistics.json`) | — |
| **AIS-catcher** | JSON API → page scrape | Inferred from `ais-ingest` reachability |
| **AISfriends** | JSON API | Inferred from AISHub (same UDP source) |
| **App Errors** | SSH into miniserver, scan Docker logs across `ais-catcher`, `ais-ingest`, `ais-api` for errors in last hour | — |
| **Tailscale key** | Days until expiry | Alerts 7 days before |

AIS-catcher and AISfriends are behind Cloudflare, which blocks API/scrape
requests from CI. When blocked, status is inferred: if `ais-ingest` is
reachable, the catcher is running. If AISHub is receiving data (UDP),
AISfriends is too (same UDP from the same process).

## On failure

When any check fails:
- Recent Docker logs from `ais-catcher`, `ais-ingest`, and `ais-api` are
  fetched via Tailscale SSH
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

The monitor SSHes into the miniserver and scans `docker logs --since 1h`
across the stack containers for: `Error`, `Exception`, `Traceback`,
`Failed`. Excluded as known-benign:

- `recv() error 0 (Success)` — AIS-catcher's harmless TCP reset chatter
- `forwarder transient error` — already demoted to warning; appears on
  brief connection blips during ais-api redeploys

This catches Postgres failures, schema migration errors, R2 upload
failures, type mismatches, unhandled Python exceptions, and similar.

## Setup

### Required secrets

| Secret | Description |
|--------|-------------|
| `TS_AUTH_KEY` | Tailscale auth key (ephemeral + reusable) |
| `GOOGLE_CHAT_WEBHOOK` | Google Chat incoming webhook URL |

### Tailscale ACL

The ACL needs `tag:ci` plus an SSH accept rule so the GitHub runner can
SSH into the miniserver for Docker log scraping:

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

### Tailscale SSH on miniserver

Enable Tailscale SSH on the miniserver:

```bash
sudo tailscale set --ssh
```

### Local testing

```bash
# Without alerts
python3 check.py

# With alerts
GOOGLE_CHAT_WEBHOOK="https://chat.googleapis.com/..." python3 check.py
```

## Related

- [EXTREMOPHILARUM/ais-station](https://github.com/EXTREMOPHILARUM/ais-station) — local radio decode + forwarder
- [EXTREMOPHILARUM/ais-api](https://github.com/EXTREMOPHILARUM/ais-api) — FastAPI + Postgres serve layer
