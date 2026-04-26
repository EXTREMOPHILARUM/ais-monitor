"""AIS station + API monitor.

Watches the consolidated stack on the miniserver:
- ais-catcher (RTL-SDR decode)
- ais-ingest (local-radio buffer + forwarder)
- ais-api (FastAPI serve, Postgres-backed) via the public Cloudflare tunnel
- AISHub / AISfriends / AIS-catcher community feeds (downstream visibility)

The Pi4 is no longer in the loop — RTL-SDR was moved to the miniserver after
the Pi died. All checks now target miniserver (Tailscale 100.86.157.26).
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import URLError

MINISERVER_IP = "100.86.157.26"
MINISERVER_USER = "extremo"
INGEST_HEALTH = f"http://{MINISERVER_IP}:9123/health"
API_HEALTH_PUBLIC = "https://api.saurabhn.com/health"
AISCATCHER_MONITOR = "https://www.aiscatcher.org/api/station/monitor?id=3122"
AISHUB_DAILY = "https://www.aishub.net/station/2387/daily-statistics.json"
AISFRIENDS_STATS = "https://www.aisfriends.com/station-stats/869?station_only=1"

HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json",
    "User-Agent": "AIS-Monitor/1.0",
}

# Containers we check Docker logs for — all on the miniserver now.
LOG_CONTAINERS = ("ais-catcher", "ais-ingest", "ais-api")

TS_KEY_EXPIRY = os.environ.get("TS_KEY_EXPIRY", "2026-06-21")
GOOGLE_CHAT_WEBHOOK = os.environ.get("GOOGLE_CHAT_WEBHOOK", "")


def fetch_json(url, timeout=15):
    """Fetch a URL and return parsed JSON, or {'_error': msg} on failure."""
    try:
        req = Request(url, headers=HEADERS)
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
            if not body:
                return None
            return json.loads(body)
    except (URLError, json.JSONDecodeError, TimeoutError) as e:
        return {"_error": str(e)}


def fetch_page_text(url, timeout=15):
    """Fetch HTML page and return its text content."""
    try:
        req = Request(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html",
        })
        with urlopen(req, timeout=timeout) as resp:
            return resp.read().decode()
    except (URLError, TimeoutError):
        return None


def check_ingest():
    """Check the miniserver ais-ingest /health (radio decode → forwarder buffer)."""
    data = fetch_json(INGEST_HEALTH)
    if data is None or "_error" in (data or {}):
        msg = data.get("_error", "no response") if data else "no response"
        return "unreachable", f"ais-ingest unreachable: {msg}"

    status = data.get("status", "unknown")
    local_age = data.get("local_age_s")
    buffered = data.get("buffered")
    issues = data.get("issues")

    if status == "ok":
        return "ok", f"Healthy (local: {local_age}s, buffered: {buffered})"
    return "degraded", f"Degraded: {', '.join(issues or [])} (local: {local_age}s, buffered: {buffered})"


def check_api_public():
    """Check ais-api /health via the public Cloudflare tunnel.

    Exercises Cloudflare → cloudflared → ais-api → Postgres in one call —
    the most important external-facing path.
    """
    data = fetch_json(API_HEALTH_PUBLIC)
    if data is None or "_error" in (data or {}):
        msg = data.get("_error", "no response") if data else "no response"
        return "unreachable", f"api.saurabhn.com unreachable: {msg}"

    status = data.get("status", "unknown")
    db_ok = data.get("db_ok")
    age = data.get("last_ingest_age_s")
    latest = data.get("latest_rows")
    issues = data.get("issues")

    if status == "ok" and db_ok:
        return "ok", f"Healthy (last ingest: {age}s, {latest:,} latest rows)"
    return "degraded", f"Degraded: db_ok={db_ok}, issues={issues}, age={age}s"


def check_aiscatcher(ingest_reachable):
    """Check AIS-catcher community station monitor API."""
    data = fetch_json(AISCATCHER_MONITOR)
    if data is not None and "_error" not in data:
        online = data.get("online", False)
        ago = data.get("ago_seconds")
        stats = data.get("stats", {})
        ships = stats.get("ships", 0)
        messages = stats.get("messages", 0)

        if online:
            return "ok", f"Online, {ships} ships, {messages} msgs, last {ago:.0f}s ago"
        return "offline", f"Station offline (last seen {ago}s ago)"

    # Fallback: scrape station page
    print("  ↳ API blocked, scraping page...")
    html = fetch_page_text("https://www.aiscatcher.org/station/3122")
    if html is not None and "Just a moment" not in html and "Attention Required" not in html:
        import re
        if re.search(r'"active"|Active', html):
            return "ok", "Station page shows Active"
        if re.search(r'"not_active"|Not Connected', html):
            return "offline", "Station page shows Not Connected"

    # Last fallback: infer from ais-ingest reachability (catcher pushes to ingest in-process)
    if ingest_reachable:
        return "ok", "Inferred healthy (ais-ingest reachable, catcher feeds it)"
    return "unknown", "Cannot verify — Cloudflare blocked and ais-ingest unreachable"


def check_aishub():
    """Check AISHub daily statistics — empty or trailing nulls means offline."""
    data = fetch_json(AISHUB_DAILY)
    if data is None:
        return "no_data", "No data from AISHub (station may be offline)"
    if "_error" in (data or {}):
        return "error", f"AISHub error: {data['_error']}"

    counts = data.get("count", [])
    if not counts:
        return "no_data", "Empty count array from AISHub"

    recent = [c for c in counts[-6:] if c is not None]
    if recent:
        return "ok", f"Active, latest: {recent[-1]} ships, {len(recent)}/6 recent slots"
    return "inactive", "Last 30min all nulls — station not feeding AISHub"


def check_aisfriends(aishub_ok):
    """Check AISfriends station stats API."""
    data = fetch_json(AISFRIENDS_STATS)
    if data is not None and "_error" not in data:
        vessels = data.get("vessels_count", 0)
        uptime = data.get("uptime", 0)
        if vessels > 0:
            return "ok", f"{vessels} vessels, {uptime}% uptime"
        return "inactive", f"0 vessels on AISfriends (uptime: {uptime}%)"

    # Cloudflare blocks direct access — infer from AISHub (both use UDP from same source)
    if aishub_ok:
        return "ok", "Inferred healthy (AISHub UDP feed active, same source)"
    return "unknown", "Cannot verify — Cloudflare blocked and AISHub feed is down"


def fetch_docker_logs(container, lines=20):
    """SSH into miniserver via Tailscale and fetch Docker container logs."""
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no",
             f"{MINISERVER_USER}@{MINISERVER_IP}",
             f"docker logs --tail {lines} {container} 2>&1"],
            capture_output=True, text=True, timeout=20,
        )
        return result.stdout.strip() or result.stderr.strip() or "(empty)"
    except (subprocess.TimeoutExpired, Exception) as e:
        return f"(failed to fetch: {e})"


def check_docker_errors():
    """Scan Docker logs across all stack containers for errors in the last hour."""
    # `recv() error 0 (Success)` is a benign AIS-catcher log line, not a real error.
    # `forwarder transient error` was demoted to warning but may still appear.
    grep_cmd = (
        "for c in " + " ".join(LOG_CONTAINERS) + "; do "
        "  echo \"=== $c ===\"; "
        "  docker logs --since 1h $c 2>&1 "
        "    | grep -iE 'Error|Exception|Traceback|Failed' "
        "    | grep -vE 'recv\\(\\)|forwarder transient' "
        "    | tail -10; "
        "done"
    )
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no",
             f"{MINISERVER_USER}@{MINISERVER_IP}", grep_cmd],
            capture_output=True, text=True, timeout=30,
        )
        out = result.stdout.strip()
        # Strip out empty container blocks (just the === header with nothing after)
        blocks = []
        for block in out.split("=== ")[1:]:
            name, _, body = block.partition("\n")
            body = body.strip()
            if body:
                blocks.append(f"=== {name}\n{body}")
        if not blocks:
            return "ok", "No errors in last hour across stack"
        joined = "\n".join(blocks)
        n = sum(len(b.splitlines()) - 1 for b in blocks)
        return "errors", f"{n} error line(s) in last hour:\n{joined}"
    except (subprocess.TimeoutExpired, Exception) as e:
        return "unknown", f"Could not check logs: {e}"


def check_ts_key_expiry():
    """Check if Tailscale auth key is expiring within 7 days."""
    try:
        expiry = datetime.strptime(TS_KEY_EXPIRY, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        days_left = (expiry - datetime.now(timezone.utc)).days
        if days_left <= 7:
            return "expiring", f"Tailscale key expires in {days_left} days ({TS_KEY_EXPIRY})"
    except ValueError:
        pass
    return "ok", None


def send_google_chat(text):
    """Send a message to Google Chat webhook."""
    if not GOOGLE_CHAT_WEBHOOK:
        print("GOOGLE_CHAT_WEBHOOK not set, skipping notification")
        return

    payload = json.dumps({"text": text}).encode()
    req = Request(GOOGLE_CHAT_WEBHOOK, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=10) as resp:
            print(f"Google Chat notification sent ({resp.status})")
    except URLError as e:
        print(f"Failed to send Google Chat notification: {e}")


def main():
    results = {}
    any_failed = False

    # 1. Local radio decode → forwarder buffer
    results["ais-ingest"] = check_ingest()
    ingest_reachable = results["ais-ingest"][0] in ("ok", "degraded")

    # 2. ais-api via public Cloudflare tunnel — exercises full external path
    results["ais-api (public)"] = check_api_public()

    # 3. Downstream feeds
    results["AISHub"] = check_aishub()
    results["AIS-catcher"] = check_aiscatcher(ingest_reachable)
    aishub_ok = results["AISHub"][0] == "ok"
    results["AISfriends"] = check_aisfriends(aishub_ok)

    # 4. Docker error scan across the whole stack
    results["App Errors"] = check_docker_errors()

    for name, (status, message) in results.items():
        is_ok = status == "ok"
        icon = "✅" if is_ok else "⚠️" if "Cloudflare" in message else "❌"
        print(f"{icon} {name}: {status} — {message}")
        if not is_ok:
            any_failed = True

    # Tailscale key
    ts_status, ts_msg = check_ts_key_expiry()
    if ts_status != "ok":
        print(f"🔑 Tailscale: {ts_msg}")
        any_failed = True

    # Always pull recent Docker logs for visibility
    print("\nFetching Docker logs from miniserver...")
    docker_logs = {}
    for container in LOG_CONTAINERS:
        logs = fetch_docker_logs(container)
        docker_logs[container] = logs
        print(f"--- {container} ---\n{logs}\n")

    # Alert on failure
    if any_failed:
        alert = ["🚨 *AIS Stack Alert*\n"]
        for name, (status, message) in results.items():
            alert.append(f"*{name}:* {status} — {message}")
        if ts_status != "ok":
            alert.append(f"*Tailscale:* {ts_msg}")

        for container, logs in docker_logs.items():
            truncated = logs[-500:] if len(logs) > 500 else logs
            alert.append(f"\n*{container} logs (last 20 lines):*\n```\n{truncated}\n```")

        alert.append(f"\n_{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_")
        send_google_chat("\n".join(alert))
        sys.exit(1)
    else:
        print("All checks passed.")


if __name__ == "__main__":
    main()
