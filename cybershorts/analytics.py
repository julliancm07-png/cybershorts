"""Weekly YouTube performance pull, learnings for the writer, and a Telegram report."""
from __future__ import annotations

import html
import json
import logging
import re
import time
from pathlib import Path

from . import db, writer, youtube

log = logging.getLogger("cybershorts")

HEADER = "# Learnings from past videos (style guidance, not facts)\n"
_URL_RE = re.compile(r"https?://\S+")


def learnings_path(cfg) -> Path:
    return Path(cfg.path("learnings.md"))


def read_learnings(cfg) -> str:
    """Learnings text, capped to analytics.learnings_max_chars. Empty on any problem."""
    try:
        text = learnings_path(cfg).read_text(encoding="utf-8")
    except OSError:
        return ""
    return cap_text(text, int(cfg.get("analytics.learnings_max_chars", 800) or 800))


def cap_text(text: str, limit: int) -> str:
    """Trim to `limit` chars, cutting at a line boundary where possible."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if "\n" in cut:
        cut = cut[: cut.rindex("\n")]
    return cut.rstrip()


def _iso(seconds_ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - seconds_ago))


def _send(cfg, text: str) -> None:
    if not (cfg.telegram_bot_token and cfg.telegram_chat_id):
        return
    try:
        from .telegram import Telegram

        Telegram(cfg.telegram_bot_token, cfg.telegram_chat_id).send_message(text)
    except Exception as e:  # noqa: BLE001
        log.warning("Analytics Telegram message failed: %s", type(e).__name__)


def _row(r) -> dict:
    try:
        return dict(r)
    except (TypeError, ValueError):
        return {}


def _describe(v: dict) -> dict:
    try:
        data = json.loads(v.get("data_json") or "{}")
    except ValueError:
        data = {}
    script = str(data.get("script") or "")
    first = script.strip().split("\n\n")[0].strip() if script.strip() else ""
    return {
        "title": str(data.get("youtube_title") or data.get("on_screen_title") or "")[:100],
        "hook": first[:160],
        "series": v.get("series") or "",
        "kind": v.get("kind") or "regular",
        "words": len(script.split()),
    }


def build_prompt(top: list[dict], bottom: list[dict]) -> list[dict]:
    def fmt(rows):
        return "\n".join(
            "- views=%s retention=%s%% | title: %s | hook: %s | series: %s | kind: %s | words: %s" % (
                r["views"], r.get("pct", "?"), r["title"], r["hook"], r["series"] or "none", r["kind"], r["words"])
            for r in rows)
    system = ("You analyse short-form video performance for a cybersecurity news channel. Compare the best "
              "and worst videos and infer what writing patterns (hook style, length, topic, series) seem to "
              "help. Reply with at most 6 bullet points, one per line starting with '- ', each under 120 "
              "characters. Style guidance only: no facts about specific news stories, no URLs.")
    user = "BEST videos:\n%s\n\nWORST videos:\n%s" % (fmt(top), fmt(bottom))
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_bullets(text: str, max_items: int = 6, max_len: int = 120) -> list[str]:
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not re.match(r"^([-*•]|\d+[.)])\s+", line):
            continue
        line = re.sub(r"^([-*•]|\d+[.)])\s+", "", line)
        line = _URL_RE.sub("", line).strip()
        if line:
            out.append(line[:max_len].rstrip())
        if len(out) >= max_items:
            break
    return out


def write_learnings(cfg, bullets: list[str]) -> str:
    limit = int(cfg.get("analytics.learnings_max_chars", 800) or 800)
    body = cap_text(HEADER + "\n".join("- " + b for b in bullets), limit)
    p = learnings_path(cfg)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(body + "\n", encoding="utf-8")
    tmp.replace(p)
    return body


def build_report(cfg, vids: list[dict], learnings: str) -> str:
    """HTML (Telegram) weekly report. Every dynamic value is escaped."""
    e = lambda x: html.escape(str(x), quote=False)  # noqa: E731
    total_views = sum(v["views"] for v in vids)
    total_likes = sum(v["likes"] for v in vids)
    total_comments = sum(v["comments"] for v in vids)
    lines = ["<b>Weekly analytics</b>",
             "Videos tracked: %d | Views: %s | Likes: %s | Comments: %s" % (
                 len(vids), f"{total_views:,}", f"{total_likes:,}", f"{total_comments:,}"), ""]
    lines.append("<b>Top 3</b>")
    for v in sorted(vids, key=lambda x: x["views"], reverse=True)[:3]:
        pct = ("%.0f%% retained" % v["pct"]) if v.get("pct") else "retention n/a"
        lines.append("%s views | %s | %s" % (f"{v['views']:,}", e(pct), e(v["title"] or v["id"])))
    if learnings:
        lines += ["", "<b>Learnings</b>", e(learnings)]
    return "\n".join(lines)[:4000]


def run(cfg, telegram: bool = True) -> dict | None:
    """Pull stats, update learnings, send report. Never raises. Returns a summary dict or None."""
    try:
        return _run(cfg, telegram)
    except Exception as e:  # noqa: BLE001
        log.error("Analytics failed: %s: %s", type(e).__name__, str(e)[:150])
        return None


def _run(cfg, telegram: bool) -> dict | None:
    if not cfg.get("analytics.enabled", True):
        log.info("Analytics disabled")
        return None
    if not youtube.has_scope(cfg, youtube.SCOPE_FORCE_SSL):
        log.info("Analytics skipped: YouTube token lacks the force-ssl scope (re-run scripts/auth_youtube.py)")
        if telegram:
            _send(cfg, "Analytics skipped: YouTube token lacks the youtube.force-ssl scope. "
                       "Re-run scripts/auth_youtube.py and tick every box.")
        return None
    have_yta = youtube.has_scope(cfg, youtube.SCOPE_ANALYTICS)
    lookback = int(cfg.get("analytics.lookback_days", 90))
    min_age = float(cfg.get("analytics.min_age_hours", 48))
    conn = db.connect(cfg)
    try:
        db.init(conn)
        rows = [_row(r) for r in db.published_videos(conn, _iso(lookback * 86400))]
        rows = [r for r in rows if r.get("youtube_id")]
        aged = []
        for r in rows:
            age = db.seconds_since(r.get("updated_at") or r.get("created_at"))
            if age is None or age >= min_age * 3600:
                aged.append(r)
        if not aged:
            log.info("Analytics: no videos old enough yet")
            return {"videos": 0, "learnings": False}
        ids = [r["youtube_id"] for r in aged]
        stats = youtube.video_stats(cfg, ids)
        ana: dict = {}
        if have_yta:
            try:
                ana = youtube.analytics_stats(cfg, ids, time.strftime("%Y-%m-%d", time.gmtime(time.time() - lookback * 86400)))
            except Exception as e:  # noqa: BLE001
                log.warning("YouTube Analytics query failed: %s", type(e).__name__)
        else:
            log.info("Analytics: no yt-analytics scope; views/likes/comments only")
        vids = []
        for r in aged:
            st = stats.get(r["youtube_id"])
            if not st:
                continue
            a = ana.get(r["youtube_id"], {})
            db.upsert_video_stats(conn, r["youtube_id"], draft_id=r.get("id"), views=st["views"],
                                  likes=st["likes"], comments=st["comments"],
                                  avg_view_pct=a.get("avg_view_pct"), avg_view_sec=a.get("avg_view_sec"))
            d = _describe(r)
            d.update(id=r["youtube_id"], views=st["views"], likes=st["likes"], comments=st["comments"],
                     pct=a.get("avg_view_pct"))
            vids.append(d)
    finally:
        conn.close()

    learned = False
    if len(vids) >= int(cfg.get("analytics.min_videos", 6)):
        ranked = sorted(vids, key=lambda v: (v["views"], v.get("pct") or 0), reverse=True)
        n = max(1, min(int(cfg.get("analytics.top_n", 5)), len(ranked) // 2))
        try:
            text = writer.chat(cfg, build_prompt(ranked[:n], ranked[-n:]), temperature=0.3,
                               model=cfg.get("analytics.model") or None)
            bullets = parse_bullets(text)
            if bullets:
                write_learnings(cfg, bullets)
                learned = True
        except Exception as e:  # noqa: BLE001
            log.warning("Learnings generation failed: %s", type(e).__name__)
    else:
        log.info("Analytics: %d videos, need %s for learnings", len(vids), cfg.get("analytics.min_videos", 6))

    if telegram and cfg.get("analytics.report", True):
        _send(cfg, build_report(cfg, vids, read_learnings(cfg)))
    return {"videos": len(vids), "learnings": learned}
