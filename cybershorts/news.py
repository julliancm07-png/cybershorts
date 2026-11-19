"""Fetch RSS feeds into candidate stories."""
from __future__ import annotations

import calendar
import html
import logging
import re
import sqlite3
import time
from typing import Any

import feedparser
import requests

from . import db
from .config import Config

log = logging.getLogger("cybershorts")

UA = "Mozilla/5.0 (compatible; cybershorts/0.1; +https://github.com/)"
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(text: str, limit: int = 600) -> str:
    text = _TAG_RE.sub(" ", text or "")
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _entry_epoch(entry: Any) -> float | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                return float(calendar.timegm(t))
            except (TypeError, ValueError, OverflowError):
                continue
    return None


def parse_feed(content: bytes | str, source: str) -> list[dict]:
    """Parse feed content into story dicts (no filtering)."""
    parsed = feedparser.parse(content)
    out: list[dict] = []
    for e in parsed.entries:
        url = (e.get("link") or "").strip()
        title = strip_html(e.get("title", ""), 300)
        if not url or not title:
            continue
        summary = strip_html(e.get("summary") or e.get("description") or "", 600)
        ts = _entry_epoch(e)
        published = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else None
        out.append({"url": url, "title": title, "source": source, "published": published,
                    "summary": summary, "_ts": ts})
    return out


def fetch_candidates(cfg: Config, conn: sqlite3.Connection) -> list[dict]:
    max_age = float(cfg.get("news.max_age_hours", 36))
    max_n = int(cfg.get("news.max_candidates", 25))
    cutoff = time.time() - max_age * 3600
    items: dict[str, dict] = {}
    attempted = failed = 0
    for feed in cfg.get("feeds", []) or []:
        name, url = feed.get("name", "feed"), feed.get("url")
        if not url:
            continue
        attempted += 1
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
            r.raise_for_status()
            stories = parse_feed(r.content, name)
        except Exception as e:  # skip failing feed
            log.warning("Feed %s failed: %s", name, e)
            failed += 1
            continue
        for s in stories:
            if s["_ts"] is not None and s["_ts"] < cutoff:
                continue
            items.setdefault(s["url"], s)
    if attempted and failed == attempted:
        raise RuntimeError("All feeds failed")
    result: list[dict] = []
    for s in items.values():
        if db.story_used(conn, s["url"]):
            continue
        db.upsert_story(conn, s["url"], s["title"], s["source"], s["published"], s["summary"])
        result.append(s)
    result.sort(key=lambda s: s["_ts"] if s["_ts"] is not None else time.time(), reverse=True)
    for s in result:
        s.pop("_ts", None)
    return result[:max_n]
