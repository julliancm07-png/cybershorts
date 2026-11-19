"""B-roll background clips: Pixabay (default), Pexels, or a local folder of your own clips.

Every provider falls back to the local folder (data/broll/) if the API returns nothing,
and the renderer falls back to a plain background if there is no clip at all.
"""
from __future__ import annotations

import logging
import random
from pathlib import Path

import requests

log = logging.getLogger(__name__)

PIXABAY_URL = "https://pixabay.com/api/videos/"
PEXELS_URL = "https://api.pexels.com/videos/search"
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
FALLBACK_QUERY = "technology"


# ---------- Pixabay ----------
def _pixabay_pick(hits: list) -> list[str]:
    """Pick one download URL per hit. Pixabay clips are mostly landscape; the renderer
    crops to 9:16, so prefer the tallest rendition between 720 and 2160 px high."""
    cands = []
    for hit in hits:
        best = None
        for size in ("large", "medium", "small"):
            v = (hit.get("videos") or {}).get(size) or {}
            url, h = v.get("url"), v.get("height") or 0
            if url and 720 <= h <= 2160:
                portrait_bonus = 400 if h > (v.get("width") or 0) else 0
                score = h + portrait_bonus
                if best is None or score > best[0]:
                    best = (score, url)
        if best:
            cands.append(best[1])
    return cands


def _pixabay_search(key: str, query: str) -> str | None:
    r = requests.get(
        PIXABAY_URL,
        params={"key": key, "q": query[:100], "safesearch": "true", "per_page": 20, "order": "popular"},
        timeout=20,
    )
    r.raise_for_status()
    cands = _pixabay_pick(r.json().get("hits", []))
    return random.choice(cands[:8]) if cands else None


# ---------- Pexels (kept for anyone who already has a key) ----------
def _pexels_pick(videos: list) -> list[str]:
    cands = []
    for v in videos:
        best = None
        for f in v.get("video_files", []) or []:
            h, w = f.get("height") or 0, f.get("width") or 0
            if h >= 1280 and h > w and f.get("link"):
                score = abs(h - 1920) + (1000 if w > 1080 and h > 2000 else 0)
                if best is None or score < best[0]:
                    best = (score, f["link"])
        if best:
            cands.append(best)
    cands.sort()
    return [link for _, link in cands]


def _pexels_search(key: str, query: str) -> str | None:
    r = requests.get(
        PEXELS_URL,
        params={"query": query, "orientation": "portrait", "per_page": 15},
        headers={"Authorization": key},
        timeout=20,
    )
    r.raise_for_status()
    cands = _pexels_pick(r.json().get("videos", []))
    return random.choice(cands[:8]) if cands else None


# ---------- local folder ----------
def local_dir(cfg) -> Path:
    d = cfg.get("broll.local_dir", "broll")
    p = Path(d)
    return p if p.is_absolute() else cfg.path(d)


def pick_local(cfg) -> Path | None:
    d = local_dir(cfg)
    if not d.is_dir():
        return None
    clips = [p for p in d.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTS]
    return random.choice(clips) if clips else None


def _download(url: str, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".part")
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
    tmp.replace(out_path)


def fetch_broll(cfg, query: str, out_path) -> Path | None:
    """Return a clip path to use as background, or None for a plain background.

    A local clip is returned in place (never copied, never deleted by cleanup)."""
    provider = str(cfg.get("broll.provider", "pixabay")).lower()
    if provider == "none":
        return None
    out_path = Path(out_path)
    if not cfg.get("broll.match_story", False):
        pool = [str(q).strip() for q in (cfg.get("broll.queries") or []) if str(q).strip()]
        if pool:
            query = random.choice(pool)
    searchers = {
        "pixabay": (getattr(cfg, "pixabay_api_key", "") or "", _pixabay_search, "PIXABAY_API_KEY"),
        "pexels": (getattr(cfg, "pexels_api_key", "") or "", _pexels_search, "PEXELS_API_KEY"),
    }
    if provider in searchers:
        key, search, env_name = searchers[provider]
        if not key:
            log.warning("%s not set; using local b-roll folder", env_name)
        else:
            for q in (query, FALLBACK_QUERY):
                if not q:
                    continue
                try:
                    link = search(key, q)
                    if not link:
                        log.info("No suitable %s video for %r", provider, q)
                        continue
                    _download(link, out_path)
                    return out_path
                except Exception as e:  # noqa: BLE001  (never log the URL: Pixabay keys ride in it)
                    log.warning("%s fetch failed for %r: %s", provider, q, type(e).__name__)
    elif provider != "local":
        log.warning("Unknown broll.provider %r; using local folder", provider)
    clip = pick_local(cfg)
    if clip is None:
        log.info("No b-roll clip available; using a plain background")
    return clip
