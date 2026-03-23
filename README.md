# AIS Station Monitor

GitHub Actions-based monitoring for the AIS station on Pi4. Runs hourly, connects via Tailscale, and sends Google Chat alerts on failure.

## What it checks

| Check | Method | Fallback |
|-------|--------|----------|
| **Pi4 Health** | Direct HTTP via Tailscale (`/health` endpoint) | — |
| **AISHub** | JSON API (`/station/2387/daily-statistics.json`) | — |
| **AIS-catcher** | JSON API → page scrape | Inferred from Pi4 health |
| **AISfriends** | JSON API | Inferred from AISHub (same UDP source) |

AIS-catcher and AISfriends are behind Cloudflare, which blocks API/scrape requests from CI. When blocked, status is inferred: if AIS-catcher is sending data to our ingest (Pi4 healthy), the TCP push to aiscatcher.org is working. If AISHub is receiving data (UDP), AISfriends is too (same UDP from same process).

## Alerts

Sends Google Chat webhook notifications when:
- Pi4 is unreachable or ingest service is degraded
- AISHub shows station inactive (trailing nulls in daily stats)
- AIS-catcher or AISfriends detected as offline
- Tailscale auth key expiring within 7 days

On failure, Docker logs from both `ais-ingest` and `ais-catcher` containers are fetched via Tailscale SSH and included in the alert.

## Setup

### Required secrets

| Secret | Description |
|--------|-------------|
| `TS_AUTH_KEY` | Tailscale auth key (ephemeral + reusable) |
| `GOOGLE_CHAT_WEBHOOK` | Google Chat incoming webhook URL |

### Tailscale ACL

The ACL policy needs `tag:ci` defined and an SSH accept rule:

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

### Local testing

```bash
GOOGLE_CHAT_WEBHOOK="" python3 check.py
```
