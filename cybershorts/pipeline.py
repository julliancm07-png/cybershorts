"""Orchestration: make_draft, render_draft, attach_voice, publish, reject, cleanup."""
from __future__ import annotations

import html
import json
import logging
import re
import subprocess
from pathlib import Path

from . import db, news, writer
from .config import Config

log = logging.getLogger("cybershorts")

MAX_TG_VIDEO_BYTES = 49 * 1024 * 1024
STUCK_PUBLISHING_SECONDS = 30 * 60


def _telegram(cfg: Config):
    from .telegram import Telegram

    return Telegram(cfg.telegram_bot_token, cfg.telegram_chat_id)


def _notify(cfg: Config, text: str) -> None:
    if not (cfg.telegram_bot_token and cfg.telegram_chat_id):
        return
    try:
        _telegram(cfg).send_message(html.escape(text, quote=False))
    except Exception as e:
        log.warning("Telegram notify failed: %s", e)


def _short_error(cfg: Config, e: Exception, limit: int = 200) -> str:
    """Short error text with any configured secret values removed."""
    msg = f"{type(e).__name__}: {e}"
    for name in ("telegram_bot_token", "openrouter_api_key", "pexels_api_key", "pixabay_api_key"):
        secret = getattr(cfg, name, None)
        if secret and isinstance(secret, str):
            msg = msg.replace(secret, "***")
    return msg.replace("\n", " ")[:limit]


def _first_sentence(text: str) -> str:
    parts = re.split(r"(?<=[.!?])\s+", text.strip().split("\n")[0], maxsplit=1)
    return parts[0]


def tiktok_caption(data: dict) -> str:
    tags = " ".join(f"#{t}" for t in data.get("hashtags", []))
    return f"{_first_sentence(data.get('description', ''))} {tags}".strip()


def _fact_check_lines(fc, max_issues: int = 5) -> list[str]:
    """Plain-text lines (to be HTML-escaped by the caller) for the fact-check result."""
    if not isinstance(fc, dict):
        return []
    if fc.get("skipped"):
        reason = " ".join(str(fc["skipped"]).split())[:80]
        return [f"ℹ️ Fact-check skipped ({reason})"]
    issues = [str(i) for i in (fc.get("issues") or []) if str(i).strip()][:max_issues]
    if fc.get("ok", True) and not issues:
        return ["✅ Fact-check: nothing unsupported found"]
    return ["⚠️ Fact-check:"] + [f"• {i}" for i in issues]


def _telegram_caption(data: dict, story_url: str, draft_id: int) -> str:
    """HTML-escaped caption, <= 1024 chars (script preview is trimmed first)."""
    title = data.get("on_screen_title", "")
    script = data.get("script", "")
    full_preview = script if len(script) <= 300 else script[:299].rstrip() + "…"
    tt = tiktok_caption(data)
    fc = data.get("fact_check")

    def build(prev_len: int, tt_len: int, max_issues: int) -> str:
        preview = full_preview
        if len(preview) > prev_len:
            preview = preview[:prev_len].rstrip() + "…" if prev_len > 0 else ""
        blocks = [f"<b>{html.escape(title)}</b>", f"Draft #{draft_id}"]
        if preview:
            blocks.append(html.escape(preview))
        fcl = _fact_check_lines(fc, max_issues)
        if fcl:
            blocks.append("\n".join(html.escape(x) for x in fcl))
        blocks.append(html.escape(story_url))
        blocks.append("TikTok caption: " + html.escape(tt[:tt_len]))
        return "\n\n".join(blocks)

    prev_len, tt_len, max_issues = len(full_preview), len(tt), 5
    cap = build(prev_len, tt_len, max_issues)
    while len(cap) > 1024 and prev_len > 0:  # trim the script preview first
        prev_len = max(0, prev_len - 40)
        cap = build(prev_len, tt_len, max_issues)
    while len(cap) > 1024 and tt_len > 0:
        tt_len = max(0, tt_len - 40)
        cap = build(prev_len, tt_len, max_issues)
    while len(cap) > 1024 and max_issues > 0:
        max_issues -= 1
        cap = build(prev_len, tt_len, max_issues)
    return cap[:1024]


def _draft_data(row) -> dict:
    return json.loads(row["data_json"] or "{}")


def cleanup(cfg: Config) -> int:
    """Delete media files of old finished drafts. Never deletes db rows."""
    days = int(cfg.get("cleanup.keep_days", 7))
    conn = db.connect(cfg)
    removed = 0
    try:
        db.init(conn)
        for row in db.drafts_to_cleanup(conn, days):
            for key in ("video_path", "audio_path"):
                p = row[key]
                if p and Path(p).exists():
                    try:
                        Path(p).unlink()
                        removed += 1
                    except OSError as e:
                        log.warning("Could not delete %s: %s", p, e)
            work = cfg.path("work")
            did = row["id"]
            # draft_{id}.* / draft_{id}_* only (draft_1 must not match draft_12), plus voice upload
            victims = [*work.glob(f"draft_{did}.*"), *work.glob(f"draft_{did}_*"),
                       work / f"voice_{did}.ogg"]
            for f in victims:
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
    finally:
        conn.close()
    if removed:
        log.info("Cleanup removed %d files", removed)
    return removed


def make_draft(cfg: Config) -> int | None:
    conn = db.connect(cfg)
    try:
        db.init(conn)
        cleanup(cfg)
        try:
            candidates = news.fetch_candidates(cfg, conn)
            if not candidates:
                log.info("No new candidate stories")
                return None
            idx = writer.pick_story(cfg, candidates, db.recent_used_titles(conn, 10))
            story = candidates[idx]
            log.info("Picked story: %s", story["title"])
            if cfg.get("news.fetch_full_text", True):
                from . import article
                story = dict(story, article=article.fetch_text(
                    story["url"], int(cfg.get("news.max_article_chars", 6000))))
            data = writer.write_script(cfg, story)
            if cfg.get("llm.fact_check", True):
                data["fact_check"] = writer.fact_check(cfg, story, data)
        except Exception as e:
            log.error("Draft creation failed before draft row: %s", _short_error(cfg, e))
            _notify(cfg, f"Scheduled draft failed: {_short_error(cfg, e)}")
            raise
        data["source_url"] = story["url"]
        data["source_title"] = story["title"]
        mode = cfg.get("voice.mode", "tts")
        status = "awaiting_voice" if mode == "record" else "rendered"
        draft_id = db.create_draft(conn, story["url"], status, json.dumps(data))
        db.mark_used(conn, story["url"])
    finally:
        conn.close()

    if mode == "record":
        _notify(cfg, f"Draft #{draft_id} awaiting voice.\n\n{data['on_screen_title']}\n\n"
                     f"{data['script']}\n\nReply with a voice message to record this one.")
        return draft_id

    try:
        from . import tts

        tts.ensure_voice(cfg)
        wav = cfg.path("work") / f"draft_{draft_id}.wav"
        tts.synthesize(cfg, data["script"], wav)
        conn = db.connect(cfg)
        try:
            db.update_draft(conn, draft_id, audio_path=str(wav))
        finally:
            conn.close()
    except Exception as e:
        _fail(cfg, draft_id, e)
        raise
    render_draft(cfg, draft_id)
    return draft_id


def _fail(cfg: Config, draft_id: int, err: Exception) -> None:
    log.error("Draft %s failed: %s", draft_id, err)
    conn = db.connect(cfg)
    try:
        db.update_draft(conn, draft_id, status="failed", error=str(err)[:1000])
    finally:
        conn.close()
    _notify(cfg, f"Draft {draft_id} failed: {str(err)[:500]}")


def render_draft(cfg: Config, draft_id: int) -> Path:
    conn = db.connect(cfg)
    try:
        row = db.get_draft(conn, draft_id)
    finally:
        conn.close()
    if row is None:
        raise ValueError(f"Draft {draft_id} not found")
    try:
        from . import captions, render

        data = _draft_data(row)
        if not row["audio_path"] or not Path(row["audio_path"]).exists():
            raise RuntimeError("Draft has no audio file")
        wav = Path(row["audio_path"])
        words = captions.transcribe_words(cfg, wav, data.get("script", ""))
        words = captions.align_to_script(words, data.get("script", ""))
        if not words:
            raise RuntimeError("Transcription produced no words")
        chunks = captions.chunk_words(words, int(cfg.get("captions.words_per_chunk", 3)))
        duration = render.probe_duration(wav)
        script = data.get("script", "")
        beats = captions.beat_times(words, script, duration)
        clips: list[Path] = []
        if str(cfg.get("broll.provider", "pixabay")).lower() != "none":
            from . import visuals

            clips = visuals.fetch_broll_set(cfg, len(beats), data.get("pexels_query") or "technology",
                                            cfg.path("work") / f"draft_{draft_id}_broll")
        ass = cfg.path("work") / f"draft_{draft_id}.ass"
        captions.write_ass(chunks, cfg, data.get("on_screen_title", ""),
                           cfg.get("channel.handle", ""), duration + render.PAD_SECONDS, ass,
                           words=words, keywords=data.get("keywords"), beats=beats)
        out = cfg.path("videos") / f"draft_{draft_id}.mp4"
        try:
            render.render(cfg, wav, ass, clips or None, out, beats=beats)
        except RuntimeError as e:
            if not clips:
                raise
            log.warning("Render with b-roll failed (%s); retrying without b-roll", e)
            render.render(cfg, wav, ass, None, out, beats=beats)

        from .telegram import keyboard

        caption = _telegram_caption(data, row["story_url"], draft_id)
        kb = keyboard(draft_id, yt_enabled=cfg.get("youtube.enabled", True),
                      tt_enabled=cfg.get("tiktok.enabled", True))
        tg = _telegram(cfg)
        if out.stat().st_size > MAX_TG_VIDEO_BYTES:
            resp = tg.send_message(
                caption[:3800] + "\n\n" + html.escape(
                    f"Video is too large for Telegram ({out.stat().st_size // (1024 * 1024)} MB). "
                    f"Server path: {out}", quote=False), kb)
        else:
            resp = tg.send_video(str(out), caption, kb)
        msg_id = None
        if isinstance(resp, dict):
            msg_id = (resp.get("result") or resp).get("message_id")
        conn = db.connect(cfg)
        try:
            db.update_draft(conn, draft_id, status="rendered", video_path=str(out),
                            tg_message_id=msg_id, error=None)
        finally:
            conn.close()
        return out
    except Exception as e:
        _fail(cfg, draft_id, e)
        raise


def attach_voice(cfg: Config, draft_id: int, ogg_path: Path | str) -> Path:
    wav = cfg.path("work") / f"draft_{draft_id}.wav"
    r = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(ogg_path),
         "-ar", "22050", "-ac", "1", str(wav)],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg voice conversion failed: {r.stderr.strip()[-500:]}")
    conn = db.connect(cfg)
    try:
        db.update_draft(conn, draft_id, audio_path=str(wav))
    finally:
        conn.close()
    return render_draft(cfg, draft_id)


def publish(cfg: Config, draft_id: int, targets: set[str]) -> str:
    conn = db.connect(cfg)
    try:
        row = db.get_draft(conn, draft_id)
        if row is None:
            raise ValueError(f"Draft {draft_id} not found")
        if row["status"] == "published":
            return f"Draft {draft_id} is already published."
        if row["status"] == "publishing":
            age = db.seconds_since(row["updated_at"])
            if age is None or age <= STUCK_PUBLISHING_SECONDS:
                return f"Draft {draft_id} is already publishing."
            log.warning("Draft %s stuck in publishing for %.0fs; retrying", draft_id, age)
        if not row["video_path"] or not Path(row["video_path"]).exists():
            raise RuntimeError(f"Draft {draft_id} has no rendered video")
        data = _draft_data(row)
        video = Path(row["video_path"])
        db.update_draft(conn, draft_id, status="publishing")
    finally:
        conn.close()

    ok: list[str] = []
    errors: list[str] = []
    fields: dict = {}
    lines: list[str] = []
    # targets finished by an earlier attempt are kept and not uploaded again
    if "yt" in targets and row["youtube_id"]:
        ok.append("yt")
        lines.append(f"YouTube: already uploaded https://youtube.com/shorts/{row['youtube_id']}")
    if "tt" in targets and row["tiktok_publish_id"]:
        ok.append("tt")
        lines.append("TikTok: already sent to your inbox")

    if "yt" in targets and "yt" not in ok:
        try:
            from . import youtube

            vid = youtube.upload(cfg, video, data["youtube_title"], data["description"],
                                 data.get("hashtags", []))
            fields["youtube_id"] = vid
            _save_fields(cfg, draft_id, youtube_id=vid)
            ok.append("yt")
            lines.append(f"YouTube: https://youtube.com/shorts/{vid}")
        except Exception as e:
            log.error("YouTube upload failed: %s", e)
            errors.append(f"YouTube: {e}")
            lines.append(f"YouTube failed: {e}")
    if "tt" in targets and "tt" not in ok:
        try:
            from . import tiktok

            pid = tiktok.upload_draft(cfg, video)
            fields["tiktok_publish_id"] = pid
            _save_fields(cfg, draft_id, tiktok_publish_id=pid)
            ok.append("tt")
            lines.append("TikTok: Draft sent to your TikTok inbox — open TikTok to post (expires ~24h)")
            lines.append(f"Suggested caption: {tiktok_caption(data)}")
        except Exception as e:
            log.error("TikTok upload failed: %s", e)
            errors.append(f"TikTok: {e}")
            lines.append(f"TikTok failed: {e}")

    if targets and set(ok) == set(targets):
        status = "published"
    elif ok:
        status = "partial"
    else:
        status = "failed"
    conn = db.connect(cfg)
    try:
        db.update_draft(conn, draft_id, status=status, error="; ".join(errors)[:1000] or None, **fields)
    finally:
        conn.close()
    return f"Draft {draft_id}: {status}\n" + "\n".join(lines)


def _save_fields(cfg: Config, draft_id: int, **fields) -> None:
    """Persist an upload id immediately so a crash mid-publish can't cause a re-upload."""
    conn = db.connect(cfg)
    try:
        db.update_draft(conn, draft_id, **fields)
    finally:
        conn.close()


def stuck_publishing(cfg: Config) -> list[int]:
    """Ids of drafts left in 'publishing' longer than STUCK_PUBLISHING_SECONDS (e.g. bot restarted mid-upload)."""
    conn = db.connect(cfg)
    try:
        rows = conn.execute("SELECT id, updated_at FROM drafts WHERE status = 'publishing'").fetchall()
        out = []
        for r in rows:
            age = db.seconds_since(r["updated_at"])
            if age is not None and age > STUCK_PUBLISHING_SECONDS:
                out.append(int(r["id"]))
        return out
    finally:
        conn.close()


def reject(cfg: Config, draft_id: int) -> None:
    conn = db.connect(cfg)
    try:
        row = db.get_draft(conn, draft_id)
        if row is None:
            raise ValueError(f"Draft {draft_id} not found")
        if row["status"] in ("published", "partial", "publishing"):
            raise ValueError(f"Draft {draft_id} is {row['status']}; not rejecting")
        db.update_draft(conn, draft_id, status="rejected")
    finally:
        conn.close()


def mark_manual(cfg: Config, draft_id: int) -> None:
    """User posted the video by hand: mark published, no uploads."""
    conn = db.connect(cfg)
    try:
        row = db.get_draft(conn, draft_id)
        if row is None:
            raise ValueError(f"Draft {draft_id} not found")
        if row["status"] == "published":
            return
        db.update_draft(conn, draft_id, status="published", error=None)
    finally:
        conn.close()
