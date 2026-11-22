"""Weekly recap: Telegram review message and YouTube-only publish."""
from __future__ import annotations

import html
import logging
from pathlib import Path

from . import db, pipeline

log = logging.getLogger("cybershorts")

CAPTION_LIMIT = 1024
VERIFY_HINT = "Verify your channel at youtube.com/verify"  # set_thumbnail already explains the 403 itself


def _esc(x) -> str:
    return html.escape(str(x if x is not None else ""), quote=False)


def _fmt_time(sec) -> str:
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _chapter_lines(data: dict) -> list[str]:
    out = []
    for ch in data.get("chapters") or []:
        try:
            out.append(f"{_fmt_time(ch[0])} {' '.join(str(ch[1]).split())}")
        except (TypeError, ValueError, IndexError):
            continue
    return out


def recap_caption(data: dict, draft_id: int) -> str:
    """HTML caption, every dynamic value escaped, <= 1024 chars. Trims chapters first, then fact-check issues."""
    title = " ".join(str(data.get("youtube_title") or data.get("on_screen_title") or "").split())
    week = data.get("week_label") or data.get("week") or ""
    dur = data.get("duration")
    words = data.get("words") or len(str(data.get("script") or "").split())
    meta = []
    if dur:
        try:
            meta.append(_fmt_time(dur))
        except (TypeError, ValueError):
            pass
    if words:
        meta.append(f"{words} words")
    chapters = _chapter_lines(data)
    warn = str(data.get("chapters_warning") or "").strip()
    thumb = str(data.get("thumbnail_text") or "").strip()
    fc = data.get("fact_check")
    vs = data.get("visuals_stats")
    stats_line = ""
    if isinstance(vs, dict):
        try:
            stats_line = "🎞 scenes {} · points {} · dropped {}".format(
                *(int(vs.get(k) or 0) for k in ("scenes", "points", "dropped")))
        except (TypeError, ValueError):
            stats_line = ""

    def build(max_ch: int, max_issues: int, title_len: int, with_stats: bool = True) -> str:
        t = title if len(title) <= title_len else title[: title_len - 1].rstrip() + "…"
        blocks = [f"🎬 Weekly recap #{draft_id}" + (f" · {_esc(week)}" if week else ""), f"<b>{_esc(t)}</b>"]
        if meta:
            blocks.append(_esc(" · ".join(meta)))
        if stats_line and with_stats:
            blocks.append(stats_line)
        if chapters:
            shown = chapters[:max_ch]
            if len(shown) < len(chapters):
                shown = shown + [f"… +{len(chapters) - len(shown)} more"]
            blocks.append("\n".join(_esc(c) for c in shown))
        else:
            blocks.append(_esc(f"⚠️ No chapters: {warn or 'too few or too short parts'}"))
        if thumb:
            blocks.append(f"🖼 {_esc(thumb)}")
        fcl = pipeline._fact_check_lines(fc, max_issues)
        if fcl:
            blocks.append("\n".join(_esc(x) for x in fcl))
        return "\n\n".join(b for b in blocks if b)

    max_ch, max_issues, title_len = len(chapters), 5, 100
    with_stats = True
    cap = build(max_ch, max_issues, title_len)
    if len(cap) > CAPTION_LIMIT:  # the stats line goes first, before any chapter is cut
        with_stats = False
        cap = build(max_ch, max_issues, title_len, with_stats)
    while len(cap) > CAPTION_LIMIT and max_ch > 0:
        max_ch -= 1
        cap = build(max_ch, max_issues, title_len, with_stats)
    while len(cap) > CAPTION_LIMIT and max_issues > 0:
        max_issues -= 1
        cap = build(max_ch, max_issues, title_len, with_stats)
    while len(cap) > CAPTION_LIMIT and title_len > 20:
        title_len -= 20
        cap = build(max_ch, max_issues, title_len, with_stats)
    return cap[:CAPTION_LIMIT]


def send_review(cfg, draft_id: int, data: dict, preview, thumb, full) -> int | None:
    """Send thumbnail (best effort) then the preview video with the review keyboard.
    Falls back to a text message (server path + keyboard) if the preview is missing or too big."""
    from .telegram import recap_keyboard

    tg = pipeline._telegram(cfg)
    caption = recap_caption(data, draft_id)
    kb = recap_keyboard(draft_id)
    if thumb and Path(thumb).exists():
        try:
            tg.send_photo(thumb)
        except Exception as e:  # noqa: BLE001
            log.warning("Recap thumbnail send failed: %s", type(e).__name__)
    use_video = False
    if preview and Path(preview).exists():
        try:
            use_video = Path(preview).stat().st_size <= pipeline.MAX_TG_VIDEO_BYTES
        except OSError:
            use_video = False
    msg = None
    if use_video:
        try:
            msg = tg.send_video(preview, caption, kb)
        except Exception as e:  # noqa: BLE001
            log.warning("Recap preview send failed: %s", type(e).__name__)
    if msg is None:
        note = "\n\n" + _esc(f"Preview unavailable. Full video on server: {full}") if full else ""
        msg = tg.send_message(caption[: 4096 - len(note)] + note, kb)
    return msg.get("message_id") if isinstance(msg, dict) else None


def publish(cfg, draft_id: int) -> str:
    """Upload a recap to YouTube (never TikTok). Thumbnail/comment failures never affect the result."""
    conn = db.connect(cfg)
    try:
        row = db.get_draft(conn, draft_id)
        if row is None:
            raise ValueError(f"Draft {draft_id} not found")
        if row["status"] == "published":
            return f"Draft {draft_id} is already published."
        if not cfg.get("youtube.enabled", True):
            return f"Recap {draft_id}: YouTube is turned off (youtube.enabled: false); nothing was uploaded."
        if row["status"] == "publishing":
            age = db.seconds_since(row["updated_at"])
            if age is None or age <= pipeline.STUCK_PUBLISHING_SECONDS:
                return f"Draft {draft_id} is already publishing."
            log.warning("Recap %s stuck in publishing for %.0fs; retrying", draft_id, age)
        if not row["video_path"] or not Path(row["video_path"]).exists():
            raise RuntimeError(f"Draft {draft_id} has no rendered video")
        data = pipeline._draft_data(row)
        from . import music

        mv = music.music_version(cfg, draft_id)
        video = mv if mv.exists() else Path(row["video_path"])
        db.update_draft(conn, draft_id, status="publishing")
    finally:
        conn.close()

    lines: list[str] = []
    vid = row["youtube_id"]
    error = None
    if vid:
        lines.append(f"YouTube: already uploaded https://youtu.be/{vid}")
    else:
        try:
            from . import youtube

            vid = youtube.upload(cfg, video, data.get("youtube_title", ""), data.get("description", ""),
                                 data.get("hashtags", []), category_id=str(cfg.get("recap.category_id", "28")))
            pipeline._save_fields(cfg, draft_id, youtube_id=vid)
            lines.append(f"YouTube: https://youtu.be/{vid}")
        except Exception as e:  # noqa: BLE001
            log.error("Recap YouTube upload failed: %s", e)
            error = f"YouTube: {pipeline._short_error(cfg, e)}"
            lines.append(f"YouTube failed: {pipeline._short_error(cfg, e)}")
            vid = None
    if vid:
        # always (re)try the thumbnail: a retry after a crash post-upload must not skip it
        thumb = Path(cfg.path("work", f"draft_{draft_id}_thumb.jpg"))
        ok, reason = False, "no thumbnail file"
        try:
            from . import youtube

            if thumb.exists():
                ok, reason = youtube.set_thumbnail(cfg, vid, thumb)
        except Exception as e:  # noqa: BLE001
            ok, reason = False, pipeline._short_error(cfg, e, 100)
        if ok:
            lines.append("🖼 Thumbnail set")
        else:
            low = str(reason).lower()
            hint = f". {VERIFY_HINT}" if ("403" in low or "not allowed" in low) else ""
            lines.append(f"⚠️ Thumbnail not set: {reason}{hint}")
        if not (row["yt_comment_id"] or ""):
            try:
                note = pipeline._first_comment(cfg, draft_id, vid, data)
            except Exception as e:  # noqa: BLE001
                note = f"Comment skipped: {type(e).__name__}"
            if note:
                lines.append(note)
    status = "published" if vid else "failed"
    conn = db.connect(cfg)
    try:
        db.update_draft(conn, draft_id, status=status, error=error[:1000] if error else None)
    finally:
        conn.close()
    return f"Recap {draft_id}: {status}\n" + "\n".join(lines)
