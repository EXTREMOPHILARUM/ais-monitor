"""AIS station + API monitor.

Watches a stack split across two hosts:

- **pi5** (Tailscale 100.69.37.64) — the receiver.
  ais-catcher (RTL-SDR decode), ais-ingest (dedup + forwarder outbox), autoheal.
- **invoicebuddy** (Tailscale 100.81.76.63) — the serving layer.
  ais-api (FastAPI + Postgres + in-process AISHub poller), celery worker/beat,
  cloudflared. Public at https://api.saurabhn.com.
- AISHub / AISfriends / AIS-catcher community feeds (downstream visibility).

History: everything ran on the miniserver until 2026-08-16, when ais-api moved
to invoicebuddy and the RTL-SDR moved to a new Pi 5. The miniserver is no longer
in the data path and is not checked.

Both hosts are reached over Tailscale SSH. The existing ACL rule grants tag:ci
`dst: autogroup:self`, which covers any device this account owns, so no ACL
change was needed for the new hosts — but each host must have Tailscale SSH
enabled (`tailscale set --ssh`). A host we cannot reach is reported as
`unknown`, never as a false "healthy".
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import URLError

SSH_USER = "extremo"
PI5_IP = "100.69.37.64"           # receiver: ais-catcher + ais-ingest
INVOICEBUDDY_IP = "100.81.76.63"  # serving: ais-api + Postgres + celery

INGEST_HEALTH = f"http://{PI5_IP}:9123/health"
API_HEALTH_PUBLIC = "https://api.saurabhn.com/health"
# Direct tailnet hit at ais-api, bypassing Cloudflare. Lets us tell "the API is
# down" apart from "the tunnel is down" when the public check fails.
API_HEALTH_DIRECT = f"http://{INVOICEBUDDY_IP}:9200/health"
AISCATCHER_MONITOR = "https://www.aiscatcher.org/api/station/monitor?id=3122"
AISHUB_DAILY = "https://www.aishub.net/station/2387/daily-statistics.json"
AISFRIENDS_STATS = "https://www.aisfriends.com/station-stats/869?station_only=1"

HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json",
    "User-Agent": "AIS-Monitor/1.0",
}

# Containers to scan Docker logs for, per host. Keys are (label, tailscale ip).
# celery-worker is included because the nightly pg_backup to Backblaze B2 runs
# there — a failing backup is otherwise silent.
LOG_TARGETS = {
    ("pi5", PI5_IP): ("ais-catcher", "ais-ingest"),
    ("invoicebuddy", INVOICEBUDDY_IP): ("ais-api", "ais-celery-worker"),
}

TS_KEY_EXPIRY = os.environ.get("TS_KEY_EXPIRY", "2026-09-20")  # workflow overrides; keep in sync
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
    """Check pi5 ais-ingest /health (radio decode → dedup → forwarder outbox)."""
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
        # Retry straight at ais-api over the tailnet. If that answers, the app
        # is fine and the fault is Cloudflare/cloudflared — a materially
        # different page to wake up for.
        direct = fetch_json(API_HEALTH_DIRECT, timeout=10)
        if direct is not None and "_error" not in direct:
            return "degraded", (
                f"Tunnel down but ais-api healthy on the tailnet "
                f"(public: {msg}; direct status={direct.get('status')}) — "
                "check cloudflared on invoicebuddy"
            )
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


# AISHub's daily-statistics backfills in bursts — even a healthy feed leaves
# strings of recent null slots that fill in retroactively (observed gaps up to
# ~50min). So "N of last 6 slots" flaps; we instead alert only when the most
# recent populated *past* slot is older than this many minutes.
AISHUB_STALE_MIN = 90


def check_aishub():
    """Check AISHub daily statistics — stale latest *past* slot means offline.

    `count` is a fixed 24h window of 5-min slots aligned to `labels` (unix ts).
    Trailing entries are *future* slots (always null), and AISHub backfills
    recent past slots in bursts, so the literal tail says nothing. We measure
    minutes since the most recent non-null slot whose label is already in the
    past, and alert only past AISHUB_STALE_MIN.
    """
    data = fetch_json(AISHUB_DAILY)
    if data is None:
        return "no_data", "No data from AISHub (station may be offline)"
    if "_error" in (data or {}):
        return "error", f"AISHub error: {data['_error']}"

    counts = data.get("count", [])
    if not counts:
        return "no_data", "Empty count array from AISHub"

    labels = data.get("labels", [])
    now = datetime.now(timezone.utc).timestamp()
    if labels and len(labels) == len(counts):
        past = [(t, c) for t, c in zip(labels, counts) if t <= now and c is not None]
    else:
        # No usable labels — fall back to the last non-null entry, age unknown.
        nz = [c for c in counts if c is not None]
        if nz:
            return "ok", f"Active, latest: {nz[-1]} ships (no slot timestamps)"
        return "inactive", "All slots null — station not feeding AISHub"

    if not past:
        return "inactive", "No populated slots today — station not feeding AISHub"

    last_t, last_v = past[-1]
    age_min = (now - last_t) / 60
    if age_min <= AISHUB_STALE_MIN:
        return "ok", f"Active, latest: {last_v} ships, {age_min:.0f}min ago"
    return "inactive", f"Last feed {age_min:.0f}min ago (>{AISHUB_STALE_MIN}min) — station not feeding AISHub"


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


def fetch_docker_logs(host_ip, container, lines=20):
    """SSH to a host over Tailscale and fetch one container's recent logs."""
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no",
             f"{SSH_USER}@{host_ip}",
             f"docker logs --tail {lines} {container} 2>&1"],
            capture_output=True, text=True, timeout=20,
        )
        return result.stdout.strip() or result.stderr.strip() or "(empty)"
    except (subprocess.TimeoutExpired, Exception) as e:
        return f"(failed to fetch: {e})"


def check_docker_errors():
    """Scan Docker logs across all stack containers for errors in the last hour.

    Matches *structured* ERROR/CRITICAL/FATAL log-level events, not any line
    mentioning "error". Both Python services log `%(asctime)s %(levelname)s
    %(name)s: %(message)s`, so the level field is space-delimited (` ERROR `).
    Grepping the level field (rather than the old keyword grep) means a single
    `logger.exception` produces ONE matched line — its message — instead of the
    whole traceback body leaking orphaned `Traceback`/`...Error:` fragments that
    can never be cleanly filtered. ais-catcher's plain-text blips have no level
    field and are covered by the dedicated AIS-catcher / ais-ingest checks.
    """
    # Benign — structured ERROR events that are transient *by design*; the
    # condition each reflects is already covered by a dedicated check above, so
    # this scan stays focused on genuine app-level faults:
    #   forwarder send failed   — ingest outbox retry+backoff while ais-api is
    #                             restarting (expected on every ais-api redeploy;
    #                             ais-api reachability is the `ais-api` check)
    benign = "forwarder send failed"
    blocks = []
    unreachable = []

    for (label, host_ip), containers in LOG_TARGETS.items():
        # `--tail` before `--since` so docker reads from the end of the json-file
        # logs instead of scanning the whole (up-to-300MB) file from the start —
        # ais-catcher's per-message firehose otherwise blows past the SSH timeout.
        # 20k lines comfortably covers >1h for every container; --since still caps
        # the window to the last hour.
        grep_cmd = (
            "for c in " + " ".join(containers) + "; do "
            "  echo \"=== $c ===\"; "
            "  docker logs --tail 20000 --since 1h $c 2>&1 "
            f"    | grep -E ' (ERROR|CRITICAL|FATAL) ' "
            f"    | grep -vE '{benign}' "
            "    | tail -10; "
            "done"
        )
        try:
            result = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no",
                 f"{SSH_USER}@{host_ip}", grep_cmd],
                capture_output=True, text=True, timeout=90,
            )
            if result.returncode != 0 and not result.stdout.strip():
                unreachable.append(f"{label}: {result.stderr.strip()[:120] or 'ssh failed'}")
                continue
            # Drop empty container blocks (a === header with nothing under it).
            for block in result.stdout.strip().split("=== ")[1:]:
                name, _, body = block.partition("\n")
                body = body.strip()
                if body:
                    blocks.append(f"=== {label}/{name}\n{body}")
        except (subprocess.TimeoutExpired, Exception) as e:
            unreachable.append(f"{label}: {e}")

    # An unreachable host is reported, never silently treated as healthy — a
    # host we cannot scan is exactly when we most want to know.
    if unreachable:
        detail = "; ".join(unreachable)
        if blocks:
            return "errors", f"Could not scan {detail}\n" + "\n".join(blocks)
        return "unknown", f"Could not check logs — {detail}"

    if not blocks:
        return "ok", "No errors in last hour across both hosts"
    n = sum(len(b.splitlines()) - 1 for b in blocks)
    return "errors", f"{n} error line(s) in last hour:\n" + "\n".join(blocks)


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
    print("\nFetching Docker logs from pi5 + invoicebuddy...")
    docker_logs = {}
    for (label, host_ip), containers in LOG_TARGETS.items():
        for container in containers:
            logs = fetch_docker_logs(host_ip, container)
            docker_logs[f"{label}/{container}"] = logs
            print(f"--- {label}/{container} ---\n{logs}\n")

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
