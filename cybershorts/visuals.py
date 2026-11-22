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
FALLBACK_QUERY = "cyber security"


# ---------- Pixabay ----------
def tags_ok(tags: str, require: list | None, exclude: list | None) -> bool:
    """Keep a clip only if its tags look cyber/tech and contain nothing off-theme.
    `require` matches as a substring (so "cyber" matches "cybersecurity");
    `exclude` matches whole tag words."""
    t = (tags or "").lower()
    words = {w for w in t.replace(",", " ").split() if w}
    if exclude and any(str(x).lower() in words for x in exclude):
        return False
    if require and not any(str(x).lower() in t for x in require):
        return False
    return True


def _pixabay_pick(hits: list, require: list | None = None, exclude: list | None = None,
                  orientation: str = "portrait") -> list[str]:
    """Pick one download URL per hit. Pixabay clips are mostly landscape; the renderer
    crops to 9:16, so prefer the tallest rendition 720-1920 px high (width <= 1920, so no 4K when smaller exists).
    orientation="landscape" instead prefers a 1280-1920 px wide rendition, favouring true landscape frames."""
    if orientation == "landscape":
        return _pixabay_pick_landscape(hits, require, exclude)
    cands = []
    for hit in hits:
        if not tags_ok(hit.get("tags", ""), require, exclude):
            continue
        best = None
        fallback = None
        for size in ("large", "medium", "small", "tiny"):
            v = (hit.get("videos") or {}).get(size) or {}
            url, h, w = v.get("url"), v.get("height") or 0, v.get("width") or 0
            if not url:
                continue
            if 720 <= h <= 1920 and w <= 1920:
                score = h + (400 if h > w else 0)
                if best is None or score > best[0]:
                    best = (score, url)
            elif h >= 720 and (fallback is None or h < fallback[0]):
                fallback = (h, url)  # e.g. 4K only: smallest oversized rendition
        if best is None and fallback:
            best = (0, fallback[1])
        if best:
            cands.append(best[1])
    return cands


def _pixabay_pick_landscape(hits: list, require: list | None, exclude: list | None) -> list[str]:
    """Landscape: renditions 1280-1920 px wide; landscape frames score +400 so a 1920x1080 beats a
    portrait 1080x1920 of the same width. Fallback: the smallest rendition at least 1280 px wide."""
    cands = []
    for hit in hits:
        if not tags_ok(hit.get("tags", ""), require, exclude):
            continue
        best = None
        fallback = None
        for size in ("large", "medium", "small", "tiny"):
            v = (hit.get("videos") or {}).get(size) or {}
            url, h, w = v.get("url"), v.get("height") or 0, v.get("width") or 0
            if not url:
                continue
            if 1280 <= w <= 1920:
                score = w + (400 if w > h else 0)
                if best is None or score > best[0]:
                    best = (score, url)
            elif w >= 1280 and (fallback is None or w < fallback[0]):
                fallback = (w, url)  # e.g. 4K only: smallest oversized rendition
        if best is None and fallback:
            best = (0, fallback[1])
        if best:
            cands.append(best[1])
    return cands


def _pixabay_search(key: str, query: str, require=None, exclude=None, skip=None,
                    orientation: str = "portrait") -> str | None:
    r = requests.get(
        PIXABAY_URL,
        params={"key": key, "q": query[:100], "safesearch": "true", "per_page": 20, "order": "popular"},
        timeout=20,
    )
    r.raise_for_status()
    cands = [c for c in _pixabay_pick(r.json().get("hits", []), require, exclude, orientation)
             if c not in (skip or ())]
    return random.choice(cands[:8]) if cands else None


# ---------- Pexels (kept for anyone who already has a key) ----------
def _pexels_pick(videos: list, orientation: str = "portrait") -> list[str]:
    cands = []
    for v in videos:
        best = None
        for f in v.get("video_files", []) or []:
            h, w = f.get("height") or 0, f.get("width") or 0
            if orientation == "landscape":
                if w >= 1280 and w > h and f.get("link"):
                    score = abs(w - 1920)  # closest to 1080p landscape
                    if best is None or score < best[0]:
                        best = (score, f["link"])
            elif h >= 1280 and h > w and f.get("link"):
                score = abs(h - 1920) + (1000 if w > 1080 and h > 2000 else 0)
                if best is None or score < best[0]:
                    best = (score, f["link"])
        if best:
            cands.append(best)
    cands.sort()
    return [link for _, link in cands]


def _pexels_search(key: str, query: str, require=None, exclude=None, skip=None,
                   orientation: str = "portrait") -> str | None:
    r = requests.get(
        PEXELS_URL,
        params={"query": query, "orientation": orientation, "per_page": 15},
        headers={"Authorization": key},
        timeout=20,
    )
    r.raise_for_status()
    cands = [c for c in _pexels_pick(r.json().get("videos", []), orientation) if c not in (skip or ())]
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


def _local_clips(cfg) -> list[Path]:
    d = local_dir(cfg)
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTS)


def _fetch_remote(cfg, provider: str, queries: list[str], out_path: Path, used: set,
                  *, orientation: str = "portrait") -> Path | None:
    """Download one clip for the first query that yields a URL not in `used`. None if nothing found."""
    searchers = {
        "pixabay": (getattr(cfg, "pixabay_api_key", "") or "", _pixabay_search, "PIXABAY_API_KEY"),
        "pexels": (getattr(cfg, "pexels_api_key", "") or "", _pexels_search, "PEXELS_API_KEY"),
    }
    if provider not in searchers:
        return None
    key, search, env_name = searchers[provider]
    if not key:
        return None
    for q in queries:
        if not q:
            continue
        try:
            link = search(key, q, cfg.get("broll.require_tags") or None,
                          cfg.get("broll.exclude_tags") or None, skip=used, orientation=orientation)
            if not link:
                log.info("No suitable %s video for %r", provider, q)
                continue
            _download(link, out_path)
            used.add(link)
            return out_path
        except Exception as e:  # noqa: BLE001  (never log the URL: Pixabay keys ride in it)
            log.warning("%s fetch failed for %r: %s", provider, q, type(e).__name__)
    return None


def fetch_broll_set(cfg, n: int, query: str, out_prefix, *, orientation: str = "portrait",
                    max_clips: int | None = None) -> list[Path]:
    """Up to `n` distinct clips (never the same download URL twice), files `{out_prefix}_{i}.mp4`.

    Shortfalls are filled with distinct files from the local folder (returned in place, never
    copied or deleted). Returns [] for provider none or when nothing is available."""
    provider = str(cfg.get("broll.provider", "pixabay")).lower()
    if provider == "none":
        return []
    if max_clips is None:
        raw_max = cfg.get("broll.max_clips", 5)
        max_clips = 5 if raw_max is None else int(raw_max)
    else:
        max_clips = int(max_clips)  # explicit override of broll.max_clips
    n = min(int(n), max_clips if max_clips > 0 else 1)
    if n <= 0:
        return []
    prefix = str(out_prefix)
    pool = [str(q).strip() for q in (cfg.get("broll.queries") or []) if str(q).strip()]
    if cfg.get("broll.match_story", False) or not pool:
        queries = [query] * n
    else:
        picks = random.sample(pool, min(n, len(pool)))
        queries = [picks[i % len(picks)] for i in range(n)]
    if provider not in ("pixabay", "pexels", "local"):
        log.warning("Unknown broll.provider %r; using local folder", provider)
    clips: list[Path] = []
    used: set = set()
    if provider in ("pixabay", "pexels"):
        key_attr = f"{provider}_api_key"
        if not (getattr(cfg, key_attr, "") or ""):
            log.warning("%s not set; using local b-roll folder", key_attr.upper())
            n_remote = 0
        else:
            n_remote = n
        for i in range(n_remote):
            got = _fetch_remote(cfg, provider, [queries[i], FALLBACK_QUERY], Path(f"{prefix}_{i}.mp4"), used,
                                orientation=orientation)
            if got:
                clips.append(got)
    if len(clips) < n:
        free = [p for p in _local_clips(cfg) if p not in clips]
        random.shuffle(free)
        clips.extend(free[: n - len(clips)])
    if not clips:
        log.info("No b-roll clip available; using a plain background")
    return clips


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
    if provider in ("pixabay", "pexels"):
        if not (getattr(cfg, f"{provider}_api_key", "") or ""):
            log.warning("%s_API_KEY not set; using local b-roll folder", provider.upper())
        else:
            got = _fetch_remote(cfg, provider, [query, FALLBACK_QUERY], out_path, set())
            if got:
                return got
    elif provider != "local":
        log.warning("Unknown broll.provider %r; using local folder", provider)
    clip = pick_local(cfg)
    if clip is None:
        log.info("No b-roll clip available; using a plain background")
    return clip
