"""AIS Station Monitor — checks Pi4 health, AIS-catcher, AISHub, AISfriends."""

import json
import os
import sys
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import URLError

PI4_HEALTH = "http://100.99.85.83:9123/health"
AISCATCHER_MONITOR = "https://www.aiscatcher.org/api/station/monitor?id=3122"
AISHUB_DAILY = "https://www.aishub.net/station/2387/daily-statistics.json"
AISFRIENDS_STATS = "https://www.aisfriends.com/station-stats/869?station_only=1"

HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json",
    "User-Agent": "AIS-Monitor/1.0",
}

TS_KEY_EXPIRY = os.environ.get("TS_KEY_EXPIRY", "2026-06-21")
GOOGLE_CHAT_WEBHOOK = os.environ.get("GOOGLE_CHAT_WEBHOOK", "")


def fetch_json(url, timeout=15):
    """Fetch a URL and return parsed JSON, or None on failure."""
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
    except (URLError, TimeoutError) as e:
        return None


def check_pi4():
    """Check the Pi4 ingest health endpoint via Tailscale."""
    data = fetch_json(PI4_HEALTH)
    if data is None or "_error" in (data or {}):
        return "unreachable", f"Pi4 unreachable: {data.get('_error', 'no response') if data else 'no response'}"

    status = data.get("status", "unknown")
    local_age = data.get("local_age_s")
    aishub_age = data.get("aishub_age_s")
    issues = data.get("issues")

    if status == "ok":
        return "ok", f"Healthy (local: {local_age}s, aishub: {aishub_age}s)"
    else:
        return "degraded", f"Degraded: {', '.join(issues or [])} (local: {local_age}s, aishub: {aishub_age}s)"


def check_aiscatcher():
    """Check AIS-catcher community station monitor API."""
    # Try JSON API first
    data = fetch_json(AISCATCHER_MONITOR)
    if data is not None and "_error" not in data:
        online = data.get("online", False)
        ago = data.get("ago_seconds")
        stats = data.get("stats", {})
        ships = stats.get("ships", 0)
        messages = stats.get("messages", 0)

        if online:
            return "ok", f"Online, {ships} ships, {messages} msgs, last {ago:.0f}s ago"
        else:
            return "offline", f"Station offline (last seen {ago}s ago)"

    # Fallback: scrape the station page
    print("  ↳ API blocked, scraping page...")
    html = fetch_page_text("https://www.aiscatcher.org/station/3122")
    if html is None or "Just a moment" in html or "Attention Required" in html:
        return "error", "Cannot reach AIS-catcher (Cloudflare blocked)"

    import re
    # Look for status indicators in the page
    if re.search(r'"active"|Active', html):
        return "ok", "Station page shows Active"
    elif re.search(r'"not_active"|Not Connected', html):
        return "offline", "Station page shows Not Connected"
    else:
        return "unknown", "Could not parse station status from page"


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

    # Check last 6 entries (30 min at 5-min intervals)
    recent = [c for c in counts[-6:] if c is not None]
    if recent:
        return "ok", f"Active, latest: {recent[-1]} ships, {len(recent)}/6 recent slots"
    else:
        return "inactive", "Last 30min all nulls — station not feeding AISHub"


def check_aisfriends(local_ok):
    """Check AISfriends station stats API."""
    # Try JSON API first
    data = fetch_json(AISFRIENDS_STATS)
    if data is not None and "_error" not in data:
        vessels = data.get("vessels_count", 0)
        uptime = data.get("uptime", 0)

        if vessels > 0:
            return "ok", f"{vessels} vessels, {uptime}% uptime"
        else:
            return "inactive", f"0 vessels on AISfriends (uptime: {uptime}%)"

    # Cloudflare blocks direct access — infer from local AIS-catcher health
    if local_ok:
        return "ok", "Inferred healthy (AIS-catcher is running, UDP feed active)"
    else:
        return "unknown", "Cannot verify — Cloudflare blocked and AIS-catcher is down"


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

    # Run checks in order — AISfriends depends on Pi4 health result
    for name, fn in [
        ("Pi4 Health", lambda: check_pi4()),
        ("AIS-catcher", lambda: check_aiscatcher()),
        ("AISHub", lambda: check_aishub()),
    ]:
        status, message = fn()
        results[name] = (status, message)

    # AISfriends: infer from local health if Cloudflare blocks
    local_ok = results["Pi4 Health"][0] == "ok"
    results["AISfriends"] = check_aisfriends(local_ok)

    for name, (status, message) in results.items():
        is_ok = status == "ok"
        icon = "✅" if status == "ok" else "⚠️" if "Cloudflare" in message else "❌"
        print(f"{icon} {name}: {status} — {message}")
        if not is_ok:
            any_failed = True

    # Check TS key
    ts_status, ts_msg = check_ts_key_expiry()
    if ts_status != "ok":
        print(f"🔑 Tailscale: {ts_msg}")
        any_failed = True

    # Send alert if anything failed
    if any_failed:
        lines = ["🚨 *AIS Station Alert*\n"]
        for name, (status, message) in results.items():
            lines.append(f"*{name}:* {status} — {message}")
        if ts_status != "ok":
            lines.append(f"*Tailscale:* {ts_msg}")
        lines.append(f"\n_{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_")
        send_google_chat("\n".join(lines))
        sys.exit(1)
    else:
        print("\nAll checks passed.")


if __name__ == "__main__":
    main()
