"""Optional Uptime Kuma push-monitor heartbeat (HEALTHCHECK_URL).

The bot pings every health.interval_seconds while it runs, so the Kuma monitor means
"the cybershorts bot is alive". Job failures are reported separately by the Telegram alerts.
"""
from __future__ import annotations

import logging
import threading
from urllib.parse import quote

import requests

log = logging.getLogger("cybershorts")


def _push_url(url: str, status: str, msg: str) -> str:
    base = url.split("?", 1)[0].split("#", 1)[0]
    return f"{base}?status={quote(status)}&msg={quote(msg[:200])}&ping="


def check(url: str, status: str = "up", msg: str = "ok", *, verify: bool = True) -> tuple[bool, str]:
    """Ping once. Never raises. Returns (ok, short reason)."""
    if not url:
        return False, "HEALTHCHECK_URL is not set"
    try:
        r = requests.get(_push_url(url, status, msg), timeout=10, verify=verify)
    except requests.exceptions.SSLError:
        return False, ("TLS certificate rejected (self-signed?). Use the plain http://<kuma-ip>:3001 push URL "
                       "or set health.verify_tls: false")
    except requests.exceptions.ConnectionError as e:
        why = "DNS lookup failed" if "resolve" in str(e).lower() or "name" in str(e).lower() else "connection refused/unreachable"
        return False, f"{why} - check the host/port in HEALTHCHECK_URL from this container"
    except requests.RequestException as e:
        return False, type(e).__name__
    if not r.ok:
        return False, f"HTTP {r.status_code}"
    try:
        body = r.json()
        if isinstance(body, dict) and body.get("ok") is False:
            return False, f"Kuma said: {str(body.get('msg') or 'not ok')[:100]} (wrong push token?)"
    except ValueError:
        pass
    return True, "ok"


def ping(url: str, status: str = "up", msg: str = "ok", *, verify: bool = True) -> bool:
    """Ping a push monitor URL. Never raises; returns True on success."""
    if not url:
        return False
    ok, why = check(url, status, msg, verify=verify)
    if not ok:
        log.warning("Health ping failed: %s", why)
    return ok


def start_heartbeat(cfg) -> threading.Thread | None:
    """Daemon thread pinging HEALTHCHECK_URL every health.interval_seconds. None if not configured."""
    url = getattr(cfg, "healthcheck_url", "") or ""
    if not url:
        return None
    interval = max(30, int(cfg.get("health.interval_seconds", 300)))
    verify = bool(cfg.get("health.verify_tls", True))
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            ping(url, "up", "bot running", verify=verify)
            stop.wait(interval)

    t = threading.Thread(target=loop, name="heartbeat", daemon=True)
    t.stop = stop  # type: ignore[attr-defined]
    t.start()
    log.info("Uptime Kuma heartbeat every %ss", interval)
    return t
