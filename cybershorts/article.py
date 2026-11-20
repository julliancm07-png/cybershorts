"""Fetch the full text of a news article so the script has real specifics.

Uses trafilatura to pull the main article body out of the page (no menus, ads or
comments). Any failure returns "" and the script falls back to the RSS summary.
"""
from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0 Safari/537.36")


def trim(text: str, max_chars: int) -> str:
    """Cut at a paragraph or sentence boundary at or before max_chars."""
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    for sep in ("\n\n", "\n", ". "):
        i = cut.rfind(sep)
        if i > max_chars * 0.6:
            return cut[: i + (1 if sep == ". " else 0)].strip()
    return cut.strip()


def fetch_text(url: str, max_chars: int = 6000, timeout: int = 20) -> str:
    try:
        import trafilatura  # lazy: optional dependency at import time
    except ImportError:
        log.warning("trafilatura not installed; using the RSS summary only")
        return ""
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}, timeout=timeout)
        r.raise_for_status()
        text = trafilatura.extract(
            r.text, url=url, include_comments=False, include_tables=False, favor_precision=True
        ) or ""
    except Exception as e:  # noqa: BLE001
        log.info("Full-article fetch failed for %s: %s", url, type(e).__name__)
        return ""
    text = trim(text, max_chars)
    if len(text) < 300:  # paywall, bot wall, or extraction miss
        log.info("Article text too short for %s; using the RSS summary", url)
        return ""
    log.info("Fetched %d characters of article text", len(text))
    return text
