"""Weekly recap: pick the week's best Shorts, write one long-form script, render and send it for review.

`run` is the entry point; the caller (scripts/recap.py) holds the draft lock.
Heavy modules (tts, captions, render, visuals, recap_captions, recap_render) are imported lazily.
"""
from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db, pipeline, tags as tagmod, writer
from .config import Config

log = logging.getLogger("cybershorts")

SHORTS_URL = "https://youtube.com/shorts/"
DESC_LIMIT = 4900
CHAPTER_MAX_CHARS = 60
THUMB_FALLBACK = "WEEK IN CYBER"
_URL_STRIP = re.compile(r"(?:https?://|www\.)\S+", re.I)


# --- week + selection ---------------------------------------------------------

def _tz(cfg: Config):
    from zoneinfo import ZoneInfo

    name = str(cfg.get("recap.timezone", "America/New_York") or "UTC")
    try:
        return ZoneInfo(name)
    except Exception as e:  # noqa: BLE001  (missing tzdata, bad name)
        log.warning("Timezone %r unavailable (%s); using UTC", name, type(e).__name__)
        return timezone.utc


def _aware(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    return now if now.tzinfo else now.replace(tzinfo=timezone.utc)


def week_key(cfg: Config, now: datetime | None = None) -> str:
    """ISO week in recap.timezone, e.g. '2026-W47'."""
    local = _aware(now).astimezone(_tz(cfg))
    iso = local.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def episode_label(cfg: Config, n: int) -> str:
    """User-facing label, e.g. 'Week 1' (recap.label, {n} = this channel's recap number)."""
    tpl = str(cfg.get("recap.label", "Week {n}") or "Week {n}")
    try:
        return tpl.format(n=n)
    except (KeyError, IndexError, ValueError):
        return f"Week {n}"


def date_range(cfg: Config, now: datetime | None = None) -> str:
    """The lookback window in plain words, e.g. 'Nov 16 to Nov 22, 2026'."""
    end = _aware(now).astimezone(_tz(cfg))
    start = end - timedelta(days=max(int(cfg.get("recap.lookback_days", 7)) - 1, 0))
    fmt = lambda d: f"{d.strftime('%b')} {d.day}"  # noqa: E731
    return f"{fmt(start)} to {fmt(end)}, {end.year}"


def _row_data(row) -> dict:
    try:
        d = json.loads(row["data_json"] or "{}")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def _views(cfg: Config, conn, ids: list[str]) -> dict[str, int]:
    """Fresh views when the token may call videos.list, topped up with stored stats."""
    out: dict[str, int] = {}
    try:
        from . import youtube

        if youtube.has_scope(cfg, youtube.SCOPE_FORCE_SSL):
            for vid, st in (youtube.video_stats(cfg, ids) or {}).items():
                out[str(vid)] = int((st or {}).get("views") or 0)
    except Exception as e:  # noqa: BLE001
        log.warning("Fresh view counts unavailable (%s); using stored stats", type(e).__name__)
    try:
        for vid, n in db.views_for(conn, [i for i in ids if i not in out]).items():
            out[vid] = int(n or 0)
    except Exception as e:  # noqa: BLE001
        log.warning("Stored view counts unavailable: %s", type(e).__name__)
    return out


def select_stories(cfg: Config, conn, now: datetime | None = None) -> list[dict]:
    """Top `recap.stories` Shorts from the last `recap.lookback_days`, ranked by views, breaking, recency."""
    since = _aware(now).astimezone(timezone.utc) - timedelta(days=int(cfg.get("recap.lookback_days", 7)))
    rows = db.recap_candidates(conn, since.strftime("%Y-%m-%dT%H:%M:%S"))
    if not rows:
        return []
    views = _views(cfg, conn, [str(r["youtube_id"]) for r in rows])
    items = []
    for r in rows:
        data = _row_data(r)
        title = (data.get("source_title") or r["story_title"] or data.get("on_screen_title") or "").strip()
        items.append({
            "draft_id": int(r["id"]), "youtube_id": str(r["youtube_id"]), "url": r["story_url"],
            "title": " ".join(title.split()), "summary": (r["story_summary"] or "").strip(),
            "script": data.get("script", ""), "description": data.get("description", ""),
            "breaking": bool(data.get("breaking")) or r["kind"] == "breaking",
            "series": data.get("series") or "", "data": data,
            "views": views.get(str(r["youtube_id"]), 0), "_updated": r["updated_at"] or "",
        })
    items.sort(key=lambda s: (s["views"] or 0, s["breaking"], s["_updated"]), reverse=True)
    out, seen = [], set()
    for s in items:
        if s["url"] in seen:
            continue
        seen.add(s["url"])
        s.pop("_updated", None)
        out.append(s)
    return out[: max(1, int(cfg.get("recap.stories", 5)))]


def source_text(cfg: Config, s: dict) -> str:
    """Everything the LLM may use for one story. The fetched article is cached on the dict."""
    parts = [f"Title: {s.get('title', '')}"]
    if s.get("summary"):
        parts.append(f"Summary: {s['summary']}")
    if s.get("script"):
        parts.append(f"Our Short's narration: {s['script']}")
    if s.get("description"):
        parts.append(f"Our Short's description: {s['description']}")
    art = s.get("article")
    if art is None and cfg.get("recap.fetch_article", True) and s.get("url"):
        try:
            from . import article

            art = article.fetch_text(s["url"], int(cfg.get("recap.max_article_chars", 2500))) or ""
        except Exception as e:  # noqa: BLE001
            log.warning("Article fetch failed: %s", type(e).__name__)
            art = ""
        s["article"] = art
    if art:
        parts.append(f"Article text: {art}")
    return "\n".join(parts)


# --- script -------------------------------------------------------------------

def write_recap_script(cfg: Config, stories: list[dict], week: str, *, label: str = "",
                       dates: str = "") -> dict:
    lo, hi = cfg.get("recap.target_words", [600, 800])
    n = len(stories)
    per = max(1, round(((lo + hi) / 2 - 90) / max(n, 1)))
    use_tags = tagmod.tags_supported(cfg)
    budget = int(cfg.get("recap.max_audio_tags", 5))
    sub = str(cfg.get("recap.subscribe_line", "") or "")
    blocks = [f"[Story {i}]\n{source_text(cfg, s)}" for i, s in enumerate(stories, 1)]
    system = (
        "You write the narration for a 6-8 minute in-depth weekly news recap video about IT and security, "
        "read aloud by a text-to-speech voice. Write for the ear, the way a sharp friend who works in IT "
        "would walk you through the week: conversational, confident, never stiff.\n"
        "Facts: use ONLY what is in the provided story material. Never invent details, numbers, names, "
        "CVE ids, dates or attributions. If something is unknown, leave it out. Write in your own words; "
        "never copy sentences from an article."
    )
    user = (
        (f"This is {label} of the channel's weekly recap" if label else "This is the weekly recap")
        + (f", covering {dates}" if dates else "")
        + ". Never mention ISO or calendar week numbers. "
        "The stories below are ranked by viewer interest; keep this order.\n\n"
        + "\n\n".join(blocks)
        + "\n\nHow the narration must sound:\n"
        "- Short sentences, mostly under 15 words, contractions, no headline-speak, no filler.\n"
        "- Spoken by a robot voice: no parentheses, slashes, symbols or URLs. Write numbers the way people "
        "say them. Spell out uncommon acronyms. Don't read CVE ids or version strings aloud.\n"
        f"- Total narration {lo}-{hi} words. One intro paragraph (what's coming this week), then exactly "
        f"{n} story segments of 3 to 5 short paragraphs each (about {per} words). Go deeper than the "
        "Short did: what happened, how it works in plain terms, who is affected, why it matters, and what "
        "to do, all from the provided material. Then a one-paragraph outro.\n"
        "- Separate paragraphs with a blank line. The voice pauses between paragraphs.\n"
        + (tagmod_instructions(cfg, budget) if use_tags else "")
        + (f"- The outro ends by inviting viewers to subscribe, e.g. \"{sub}\"\n" if sub else "")
        + "\nReturn JSON only with these keys:\n"
        '- "title": plain video title, max 70 characters, no hashtags\n'
        '- "description": 2-3 sentence summary of the week (no URLs)\n'
        '- "intro": intro narration\n'
        f'- "segments": list of exactly {n} objects {{"story_index": <1-{n}, the story number above>, '
        '"chapter": "short chapter title, max 50 characters", "narration": "..."}\n'
        '- "outro": outro narration\n'
        '- "tags": list of 8-15 tags without the # symbol\n'
        '- "thumbnail_text": 2-4 words in capitals for the thumbnail\n'
        f'- "first_comment": a question for viewers, max {writer.MAX_COMMENT_CHARS} characters, ending with "?", '
        "no URLs\n"
    )
    model = str(cfg.get("recap.model", "") or "").strip() or None
    last: Exception | None = None
    for attempt in range(2):
        text = writer.chat(cfg, [{"role": "system", "content": system}, {"role": "user", "content": user}],
                           model=model)
        try:
            return validate_recap(writer.extract_json(text), stories, cfg)
        except ValueError as e:
            last = e
            log.warning("Recap script parse/validate failed (attempt %d): %s", attempt + 1, e)
    raise RuntimeError(f"LLM did not return a valid recap script: {last}")


def tagmod_instructions(cfg: Config, budget: int) -> str:
    """Voice-tag rules for the recap prompt. The per-script budget replaces the Shorts per-script limit."""
    allowed = cfg.get("voice.elevenlabs.allowed_tags", None) or tagmod.DEFAULT_ALLOWED
    names = ", ".join(f"[{a}]" for a in allowed)
    return (f"- You may add voice tags from this list, placed right before the words they colour: {names}. "
            "Never put a tag on its own line, and never use any other tag. Use them sparingly. "
            f"Across the WHOLE script use at most {budget} voice tags in total.\n")


def _clean_text(raw) -> str:
    """Narration text: URLs removed, per-line whitespace collapsed, newlines kept."""
    lines = []
    for ln in str(raw or "").split("\n"):
        ln = _URL_STRIP.sub("", ln)
        lines.append(" ".join(ln.split()))
    return "\n".join(lines).strip()


def _trunc(s: str, n: int) -> str:
    return writer._truncate(s, n)


def _clean_tags(raw) -> list[str]:
    if isinstance(raw, str):
        raw = re.split(r"[\s,]+", raw)
    if not isinstance(raw, (list, tuple)):
        raw = []
    out: list[str] = []
    total = 0
    for t in raw:
        t = re.sub(r"[^A-Za-z0-9_]", "", str(t).lstrip("#"))
        if not t or t.lower() in [x.lower() for x in out]:
            continue
        if len(out) >= 15 or total + len(t) + 1 > 450:
            break
        out.append(t)
        total += len(t) + 1
    for fb in ("cybersecurity", "infosec", "itnews"):
        if len(out) >= 3:
            break
        if fb not in [x.lower() for x in out]:
            out.append(fb)
    return out


def _thumb_text(raw) -> str:
    text = re.sub(r"[^A-Za-z0-9 '&\-]", " ", writer._plain(raw)).upper()
    words = text.split()[:4]
    while words and len(" ".join(words)) > 28:
        words.pop()
    return " ".join(words) or THUMB_FALLBACK


def validate_recap(raw: dict, stories: list[dict], cfg: Config) -> dict:
    """Validate + clean the LLM JSON. Raises ValueError for structural problems."""
    if not isinstance(raw, dict):
        raise ValueError("Recap JSON is not an object")
    n = len(stories)
    segs = raw.get("segments")
    if not isinstance(segs, list) or len(segs) != n:
        raise ValueError(f"Expected {n} segments, got {len(segs) if isinstance(segs, list) else 'none'}")
    by_index: dict[int, dict] = {}
    for seg in segs:
        if not isinstance(seg, dict):
            raise ValueError("Segment is not an object")
        try:
            idx = int(seg.get("story_index"))
        except (TypeError, ValueError):
            raise ValueError("Segment has a bad story_index") from None
        if idx in by_index:
            raise ValueError(f"Duplicate story_index {idx}")
        by_index[idx] = seg
    if set(by_index) != set(range(1, n + 1)):
        raise ValueError(f"story_index values must be 1..{n}, got {sorted(by_index)}")

    sub = str(cfg.get("recap.subscribe_line", "") or "").strip()
    intro = _clean_text(raw.get("intro"))
    outro = _clean_text(raw.get("outro"))
    narrations = []
    chapters_txt = []
    for i in range(1, n + 1):
        seg = by_index[i]
        nar = _clean_text(seg.get("narration"))
        if not tagmod.split_paragraphs(tagmod.strip_tags(nar)):
            raise ValueError(f"Segment {i} has no narration")
        narrations.append(nar)
        ch = writer._plain(seg.get("chapter")) or stories[i - 1].get("title", "")
        chapters_txt.append(_trunc(" ".join(ch.split()), CHAPTER_MAX_CHARS))
    if not tagmod.split_paragraphs(tagmod.strip_tags(intro)):
        raise ValueError("Intro is empty")
    if "subscribe" not in outro.lower() and sub:
        outro = f"{outro}\n{sub}".strip() if outro else sub
        if tagmod.split_paragraphs(outro) and len(tagmod.split_paragraphs(outro)) > 1:
            outro = " ".join(tagmod.split_paragraphs(outro))  # keep the outro a single paragraph
    if not tagmod.split_paragraphs(tagmod.strip_tags(outro)):
        raise ValueError("Outro is empty")

    # audio tags: shared budget across parts, only in voice_script
    parts_raw = [intro] + narrations + [outro]
    supported = tagmod.tags_supported(cfg)
    allowed = cfg.get("voice.elevenlabs.allowed_tags", None)
    remaining = int(cfg.get("recap.max_audio_tags", 5))
    voice_parts, clean_parts, counts = [], [], []
    for text in parts_raw:
        if supported and remaining > 0:
            v = tagmod.sanitize_tags(text, allowed, remaining)
            remaining -= len(re.findall(tagmod.TAG_RE, v))
        else:
            v = tagmod.strip_tags(text)
        v = tagmod.normalize_paragraphs(v)
        c = tagmod.normalize_paragraphs(tagmod.strip_tags(v))
        voice_parts.append(v)
        clean_parts.append(c)
        counts.append(len(tagmod.split_paragraphs(c)))
    script = "\n\n".join(clean_parts)
    voice = "\n\n".join(voice_parts)
    total_words = len(script.split())
    lo, hi = cfg.get("recap.target_words", [600, 800])
    if not (0.8 * lo <= total_words <= 1.25 * hi):
        raise ValueError(f"Recap has {total_words} words; expected {lo}-{hi}")

    prefix = str(cfg.get("recap.title_prefix", "This Week in Cyber: ") or "")
    plain = writer._plain(raw.get("title"))
    plain = re.sub(r"\s*#Shorts\b", "", plain, flags=re.I).strip()
    if prefix and plain.lower().startswith(prefix.strip().lower()):
        plain = plain[len(prefix.strip()):].lstrip(" :-")
    plain = plain or "Top stories of the week"
    yt_title = prefix + _trunc(plain, max(10, 100 - len(prefix)))
    desc = _clean_text(tagmod.strip_tags(str(raw.get("description") or "")))
    out = {
        "title": yt_title,
        "youtube_title": yt_title,
        "on_screen_title": _trunc(plain, 60),
        "description": desc,
        "hashtags": _clean_tags(raw.get("tags") or raw.get("hashtags")),
        "thumbnail_text": _thumb_text(raw.get("thumbnail_text")),
        "intro": clean_parts[0],
        "segments": [{"story_index": i + 1, "chapter": chapters_txt[i], "narration": clean_parts[i + 1]}
                     for i in range(n)],
        "outro": clean_parts[-1],
        "script": script,
        "para_counts": counts,
    }
    if voice != script:
        out["voice_script"] = voice
    comment = writer._clean_first_comment(raw.get("first_comment"))
    if comment:
        out["first_comment"] = comment
    return out


# --- chapters -----------------------------------------------------------------

def part_starts(spans, para_counts) -> list[float] | None:
    """Start time of each part's first paragraph from TTS spans, or None if they don't line up."""
    if not spans or not para_counts or sum(para_counts) != len(spans):
        return None
    starts, idx = [], 0
    for c in para_counts:
        starts.append(float(spans[idx][0]))
        idx += c
    return starts


def part_starts_from_words(words, script: str, para_counts) -> list[float] | None:
    """Fallback: part starts from word timings, using each paragraph's word count."""
    from . import captions

    counts = captions.beat_ranges(script)
    if not words or not para_counts or sum(para_counts) != len(counts):
        return None
    total = sum(counts)
    starts, idx = [], 0
    for c in para_counts:
        first = sum(counts[:idx])
        pos = first if len(words) == total else int(first / total * len(words))
        starts.append(float(words[min(pos, len(words) - 1)][0]))
        idx += c
    return starts


def chapters(parts: list[dict], duration: float, min_len: int = 10) -> list[tuple[int, str]]:
    """[(seconds, title)]: Intro @0, one per story, outro merged into the last story.
    A part shorter than min_len merges into the previous one (a short intro merges forward).
    Returns [] when fewer than 3 chapters remain."""
    items: list[tuple[int, str]] = []
    for p in parts:
        if p.get("kind") == "outro":
            continue
        title = "Intro" if p.get("kind") == "intro" else _trunc(" ".join(str(p.get("title", "")).split()),
                                                                 CHAPTER_MAX_CHARS)
        items.append((int(math.floor(float(p.get("start", 0)))), title or "Part"))
    if not items:
        return []
    items[0] = (0, items[0][1])
    dur = float(duration)
    while True:
        for i in range(len(items)):
            end = items[i + 1][0] if i + 1 < len(items) else dur
            if end - items[i][0] < min_len:
                if i == 0 and len(items) > 1:
                    items[1] = (0, items[1][1])
                del items[i]
                break
        else:
            break
        if not items:
            return []
    return items if len(items) >= 3 else []


def valid_chapters(ch, duration: float) -> bool:
    try:
        if not ch or len(ch) < 3 or int(ch[0][0]) != 0:
            return False
        secs = [int(c[0]) for c in ch]
        if any(b - a < 10 for a, b in zip(secs, secs[1:])):
            return False
        return secs[-1] <= float(duration) - 10
    except (TypeError, ValueError, IndexError):
        return False


def _stamp(sec: int) -> str:
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def format_chapters(ch) -> str:
    return "\n".join(f"{_stamp(c[0])} {c[1]}" for c in ch)


def _blen(text: str) -> int:
    return len(text.encode("utf-8"))


def _cap_bytes(text: str, limit: int) -> str:
    """Cut to at most `limit` UTF-8 bytes without splitting a character."""
    b = text.encode("utf-8")
    return text if len(b) <= limit else b[:limit].decode("utf-8", "ignore")


def build_description(cfg: Config, data: dict, ch, stories: list[dict]) -> str:
    """Body, Chapters (only when valid), per-story links, hashtags; <= 4900 UTF-8 bytes, sources trimmed first."""
    body = str(data.get("description") or "").strip()
    chap = ""
    if valid_chapters(ch, float(data.get("duration") or 0)):
        chap = "Chapters\n" + format_chapters(ch)
    hashtags = " ".join(f"#{t}" for t in (data.get("hashtags") or [])[:5])

    def sources(level: int) -> str:
        if level >= 3:
            return ""
        blocks = []
        for s in stories:
            lines = [f"• {s.get('title', '')}"]
            if level < 2:
                lines.append(f"Short: {SHORTS_URL}{s.get('youtube_id', '')}")
            if level < 1:
                lines.append(f"Source: {s.get('url', '')}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    def assemble(level: int, body_text: str) -> str:
        return "\n\n".join(x for x in (body_text, chap, sources(level), hashtags) if x)

    for level in range(4):
        text = assemble(level, body)
        if _blen(text) <= DESC_LIMIT:
            return text
    room = DESC_LIMIT - _blen(assemble(3, ""))
    return _cap_bytes(assemble(3, _cap_bytes(body, max(0, room - 2))), DESC_LIMIT)


# --- run ----------------------------------------------------------------------

def _word_count_parts(script: str, para_counts: list[int]) -> list[int]:
    from . import captions

    counts = captions.beat_ranges(script)
    out, idx = [], 0
    for c in para_counts:
        out.append(sum(counts[idx:idx + c]))
        idx += c
    return out


def _fail(cfg: Config, draft_id: int, week: str, e: BaseException) -> None:
    msg = pipeline._short_error(cfg, e, 500)  # type: ignore[arg-type]
    log.error("Recap %s (draft %s) failed: %s", week, draft_id, msg)
    try:
        conn = db.connect(cfg)
        try:
            db.update_draft(conn, draft_id, status="failed", error=msg[:1000])
        finally:
            conn.close()
    except Exception as ee:  # noqa: BLE001
        log.error("Could not mark recap %s failed: %s", draft_id, type(ee).__name__)
    pipeline._notify(cfg, f"Weekly recap {week} failed: {msg}")


def _has_video(row) -> bool:
    vp = row["video_path"]
    try:
        return bool(vp) and Path(vp).is_file()
    except OSError:
        return False


def _check_week(cfg: Config, conn, week: str, force: bool, on_demand: bool) -> bool:
    """True when this week's recap is already made (or waiting for the user) and run should stop."""
    existing = db.find_recap(conn, week)
    if existing is None:
        return False
    status = existing["status"]
    stale = False
    if status == "rendered" and (not _has_video(existing) or not existing["tg_message_id"]):
        # a killed render leaves a 'rendered' row with no video / review message: it never finished
        log.warning("Recap #%s for %s is stale (rendered but no video or review message); marking failed",
                    existing["id"], week)
        db.update_draft(conn, existing["id"], status="failed",
                        error="stale: render did not finish (process was stopped)")
        status, stale = "failed", True
    if force:
        return False
    if status == "failed":
        # only a finished recap (video + review sent) blocks; a run killed mid-render may leave a partial mp4
        if not stale and _has_video(existing) and existing["tg_message_id"]:
            log.info("Recap #%s for %s is rendered but not published", existing["id"], week)
            if on_demand:
                pipeline._notify(cfg, f"Recap #{existing['id']} is rendered but not published - tap Publish "
                                      "on it again, or /recap force to remake")
            return True
        return False
    log.info("Recap for %s already exists (#%s, %s)", week, existing["id"], status)
    if on_demand:
        pipeline._notify(cfg, f"This week's recap is already made (#{existing['id']}). "
                              "Send /recap force to make another.")
    return True


def run(cfg: Config, *, force: bool = False, on_demand: bool = False) -> int | None:
    """Make this week's recap and send it for review. Returns the draft id, or None if skipped."""
    if not cfg.get("recap.enabled", True):
        log.info("Weekly recap is disabled (recap.enabled: false)")
        return None
    week = week_key(cfg)
    conn = db.connect(cfg)
    try:
        db.init(conn)
        if _check_week(cfg, conn, week, force, on_demand):
            return None
        stories = select_stories(cfg, conn)
        episode = db.count_published_recaps(conn) + 1
    finally:
        conn.close()
    label = episode_label(cfg, episode)
    need = int(cfg.get("recap.min_stories", 3))
    if len(stories) < need:
        msg = f"Weekly recap skipped: {len(stories)} stories this week, need {need}"
        log.info(msg)
        pipeline._notify(cfg, msg)
        return None

    try:
        data = write_recap_script(cfg, stories, week, label=label, dates=date_range(cfg))
        if cfg.get("llm.fact_check", True):
            joined = "\n\n".join(f"[Story {i}]\n{source_text(cfg, s)}" for i, s in enumerate(stories, 1))
            data["fact_check"] = writer.fact_check(
                cfg, {"title": data["title"], "summary": joined},
                {"on_screen_title": data["on_screen_title"], "script": data["script"],
                 "description": data["description"]})
    except Exception as e:  # noqa: BLE001
        log.error("Recap script failed: %s", pipeline._short_error(cfg, e))
        pipeline._notify(cfg, f"Weekly recap failed: {pipeline._short_error(cfg, e)}")
        raise
    data["week"] = week
    data["episode"] = episode
    data["week_label"] = label
    data["stories"] = [{"draft_id": s["draft_id"], "youtube_id": s["youtube_id"], "url": s["url"],
                        "title": s["title"]} for s in stories]
    data["words"] = len(data["script"].split())
    conn = db.connect(cfg)
    try:
        draft_id = db.create_draft(conn, f"recap:{week}", "rendered", json.dumps(data), kind="recap")
    finally:
        conn.close()

    clips: list[Path] = []
    try:
        _produce(cfg, draft_id, week, stories, data, clips)
    except BaseException as e:  # noqa: BLE001  (also SIGTERM -> SystemExit, Ctrl-C: never leave a stale row)
        _fail(cfg, draft_id, week, e)
        raise
    finally:
        try:
            pipeline._cleanup_broll(cfg, draft_id, clips)
        except Exception as e:  # noqa: BLE001
            log.warning("B-roll cleanup failed: %s", type(e).__name__)
    return draft_id


def _produce(cfg: Config, draft_id: int, week: str, stories: list[dict], data: dict, clips: list[Path]) -> None:
    from . import captions, music, recap_captions, recap_publish, recap_render, render, sound, tts, visuals

    wav = pipeline._voice(cfg, draft_id, data)
    spans = tts.load_spans(wav)
    script = data["script"]
    words = captions.transcribe_by_spans(cfg, wav, spans, script) if spans else None
    if words is None:
        words = captions.align_to_script(captions.transcribe_words(cfg, wav, script), script)
    if not words:
        raise RuntimeError("Transcription produced no words")
    chunks = captions.chunk_words(words, int(cfg.get("recap.words_per_chunk", 6)))
    total = float(render.probe_duration(wav)) + render.PAD_SECONDS
    data["duration"] = round(total, 2)

    pc = data["para_counts"]
    starts = part_starts(spans, pc) if spans else None
    if starts is None:
        starts = part_starts_from_words(words, script, pc)
    exact = starts is not None
    if starts is None:  # proportional guess: good enough for the render, not for chapters
        wc = _word_count_parts(script, pc)
        acc, starts = 0, []
        for w in wc:
            starts.append(total * acc / max(sum(wc), 1))
            acc += w
    starts = [0.0] + [max(s, 0.0) for s in starts[1:]]
    n = len(stories)
    parts = []
    for i, st in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else total
        kind = "intro" if i == 0 else ("outro" if i == n + 1 else "story")
        title = "Intro" if i == 0 else ("Subscribe" if i == n + 1 else data["segments"][i - 1]["chapter"])
        parts.append({"kind": kind, "start": st, "end": end, "title": title,
                      "index": i if kind == "story" else 0, "total": n})
    ch = chapters(parts, total) if exact else []
    if valid_chapters(ch, total):
        data["chapters"] = [[int(s), t] for s, t in ch]
    else:
        data["chapters"] = []
        data["chapters_warning"] = ("timings unavailable" if not exact
                                    else "too few or too short parts")
    data["description"] = build_description(cfg, data, data["chapters"], stories)

    n_clips = min(len(parts) * int(cfg.get("recap.broll_per_part", 2)), int(cfg.get("recap.max_clips", 12)))
    if n_clips > 0:
        clips.extend(visuals.fetch_broll_set(cfg, n_clips, "technology", cfg.path("work") / f"draft_{draft_id}_broll",
                                             orientation="landscape", max_clips=n_clips) or [])
    per = max(1, int(cfg.get("recap.broll_per_part", 2)))
    part_clips = [[clips[(i * per + j) % len(clips)] for j in range(per)] if clips else []
                  for i in range(len(parts))]
    overlays = {}
    for i, s in enumerate(stories, 1):
        extra = pipeline._overlay_kwargs(cfg, s.get("data") or {})
        if extra:
            overlays[i] = extra
    work = cfg.path("work")
    ass = recap_captions.write_recap_ass(
        cfg, words, chunks, parts, total, title=data["on_screen_title"], week_label=data.get("week_label") or week,
        handle=cfg.get("channel.handle", ""), overlays=overlays, out_path=work / f"draft_{draft_id}.ass")
    out = cfg.path("videos") / f"draft_{draft_id}.mp4"
    durations = [p["end"] - p["start"] for p in parts]
    prefix = str(work / f"draft_{draft_id}")
    mix_wav = work / f"draft_{draft_id}_recap_mix.wav"
    try:
        mixed, has_bed = sound.recap_audio(cfg, wav, parts, mix_wav)
    except Exception as e:  # noqa: BLE001
        log.warning("Recap sound mix failed: %s", pipeline._short_error(cfg, e, 100))
        mixed, has_bed = None, False
    audio = mixed or wav
    try:
        try:
            recap_render.render_recap(cfg, audio, ass, part_clips, durations, out, prefix=prefix)
        except RuntimeError as e:
            if not clips:
                raise
            log.warning("Recap render with b-roll failed (%s); retrying without b-roll", type(e).__name__)
            recap_render.render_recap(cfg, audio, ass, [[] for _ in parts], durations, out, prefix=prefix)
    finally:
        if mixed:
            try:
                Path(mixed).unlink(missing_ok=True)
            except OSError:
                pass
    thumb = work / f"draft_{draft_id}_thumb.jpg"
    try:
        still = recap_render.extract_still(clips[0] if clips else None, work / f"draft_{draft_id}_still.png")
        tass = work / f"draft_{draft_id}_thumb.ass"
        tass.write_text(recap_captions.build_thumbnail_ass(cfg, data["thumbnail_text"]), encoding="utf-8")
        recap_render.render_thumbnail(cfg, still, tass, thumb)
    except Exception as e:  # noqa: BLE001
        log.warning("Recap thumbnail failed: %s", pipeline._short_error(cfg, e, 100))
        thumb = None
    pipeline._cleanup_broll(cfg, draft_id, clips)
    clips.clear()

    with_music, _track = (None, None) if has_bed else music.add_music(cfg, draft_id, out)
    preview = work / f"draft_{draft_id}_preview.mp4"
    try:
        recap_render.render_preview(cfg, with_music or out, preview, total)
    except Exception as e:  # noqa: BLE001
        log.warning("Recap preview failed: %s", pipeline._short_error(cfg, e, 100))
        preview = None

    conn = db.connect(cfg)
    try:
        db.update_draft(conn, draft_id, status="rendered", video_path=str(out),
                        data_json=json.dumps(data), error=None)
    finally:
        conn.close()
    msg_id = recap_publish.send_review(cfg, draft_id, data, preview, thumb, out)
    if msg_id is not None:
        conn = db.connect(cfg)
        try:
            db.update_draft(conn, draft_id, tg_message_id=msg_id)
        finally:
            conn.close()
