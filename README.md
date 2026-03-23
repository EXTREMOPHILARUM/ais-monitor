# AIS Station Monitor

GitHub Actions-based monitoring for the AIS station on Pi4. Runs every 15 minutes, connects via Tailscale, and sends Google Chat alerts on failure.

## What it checks

1. **Pi4 health endpoint** (`http://100.99.85.83:9123/health`) — verifies ingest service is running, local AIS data and AISHub data are flowing
2. **AISHub station page** — verifies station is listed as active
3. **AIS-catcher community** — basic reachability check

## Alerts

Sends Google Chat webhook notifications when:
- Pi4 is unreachable (down, network issue, Tailscale disconnected)
- Ingest service is degraded (local data stale >30s, AISHub stale >120s)
- AISHub shows station as offline (UDP feed issue)

## Setup

### 1. Tailscale OAuth client

Create an OAuth client for GitHub Actions to join your Tailscale network:

1. Go to [Tailscale Admin Console](https://login.tailscale.com/admin/settings/oauth) → OAuth clients
2. Create new OAuth client with tag `tag:ci`
3. Add to your Tailscale ACL policy:
   ```json
   "tagOwners": {
     "tag:ci": ["autogroup:admin"]
   }
   ```
4. Add secrets to this repo:
   - `TS_OAUTH_CLIENT_ID` — OAuth client ID
   - `TS_OAUTH_SECRET` — OAuth client secret

### 2. Google Chat webhook

1. In Google Chat, go to the space where you want alerts
2. Apps & integrations → Webhooks → Create webhook
3. Copy the webhook URL
4. Add as repo secret: `GOOGLE_CHAT_WEBHOOK`

### 3. Required secrets

| Secret | Description |
|--------|-------------|
| `TS_OAUTH_CLIENT_ID` | Tailscale OAuth client ID |
| `TS_OAUTH_SECRET` | Tailscale OAuth client secret |
| `GOOGLE_CHAT_WEBHOOK` | Google Chat incoming webhook URL |
