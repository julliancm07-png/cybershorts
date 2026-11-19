"""Optional Uptime Kuma push-monitor ping."""
from __future__ import annotations

import logging
from urllib.parse import quote

import requests

log = logging.getLogger("cybershorts")


def ping(url: str, status: str = "up", msg: str = "ok") -> bool:
    """Ping a push monitor URL. Never raises; returns True on HTTP success."""
    if not url:
        return False
    url = url.split("?", 1)[0].split("#", 1)[0]
    try:
        r = requests.get(f"{url}?status={quote(status)}&msg={quote(msg[:200])}", timeout=10)
        return r.ok
    except requests.RequestException as e:
        log.warning("Health ping failed: %s", type(e).__name__)
        return False
