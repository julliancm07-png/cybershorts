"""Orchestration: make_draft, render_draft, attach_voice, publish, reject, cleanup."""
from __future__ import annotations

import contextlib
import fcntl
import html
import json
import logging
import re
import subprocess
import time
from pathlib import Path

from . import db, news, writer
from .config import Config
from .cvedata import CVE_RE

log = logging.getLogger("cybershorts")

MAX_TG_VIDEO_BYTES = 49 * 1024 * 1024
STUCK_PUBLISHING_SECONDS = 30 * 60


class Busy(RuntimeError):
    """Another draft job holds data/draft.lock."""


@contextlib.contextmanager
def draft_lock(cfg: Config, blocking: bool = True, timeout: float | None = None):
    """flock on data/draft.lock. Entry points (make_draft.py, breaking.py, bot) take it;
    pipeline functions never do. Raises Busy if it can't be had (non-blocking, or timeout)."""
    f = open(cfg.path("draft.lock"), "a+")
    try:
        if not blocking:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise Busy("another draft job is running") from None
        elif timeout is None:
            fcntl.flock(f, fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + max(0.0, float(timeout))
            while True:
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise Busy("timed out waiting for another draft job") from None
                    time.sleep(0.5)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(f, fcntl.LOCK_UN)
    finally:
        f.close()


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
    for name in ("telegram_bot_token", "openrouter_api_key", "pexels_api_key", "pixabay_api_key",
                 "elevenlabs_api_key", "nvd_api_key"):
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


def tiktok_caption_for(cfg: Config, draft_id: int) -> str | None:
    """The TikTok caption for a draft, if its TikTok upload succeeded."""
    conn = db.connect(cfg)
    try:
        row = db.get_draft(conn, draft_id)
    finally:
        conn.close()
    if row is None or not row["tiktok_publish_id"]:
        return None
    return tiktok_caption(_draft_data(row))


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


def _alt_hook_numbers(data: dict) -> list[int]:
    """1-based numbers of the hooks that are not currently in the script."""
    hooks = data.get("hooks") or []
    try:
        active = int(data.get("hook_index") or 0)
    except (TypeError, ValueError):
        active = 0
    return [i + 1 for i in range(len(hooks)) if i != active] if len(hooks) > 1 else []


def _telegram_caption(data: dict, story_url: str, draft_id: int) -> str:
    """HTML-escaped caption, <= 1024 chars. Trim order: script preview, alt hooks,
    TikTok caption, fact-check issues."""
    title = data.get("on_screen_title", "")
    script = data.get("script", "")
    full_preview = script if len(script) <= 300 else script[:299].rstrip() + "…"
    tt = tiktok_caption(data)
    fc = data.get("fact_check")
    hooks = data.get("hooks") or []
    alt_nums = _alt_hook_numbers(data)
    header = [f"Draft #{draft_id}"]
    if data.get("breaking"):
        header.append("🚨 BREAKING")
    if data.get("series"):
        header.append(f"📺 {data['series']}")

    def alt_block(alt_len: int) -> str:
        if alt_len <= 0 or not alt_nums:
            return ""
        parts = []
        for n in alt_nums:
            h = " ".join(str(hooks[n - 1]).split())
            if len(h) > alt_len:
                h = h[: alt_len - 1].rstrip() + "…"
            parts.append(f"{n}) {h}")
        return "🪝 Alt hooks: " + " ".join(parts)

    def build(prev_len: int, alt_len: int, tt_len: int, max_issues: int) -> str:
        preview = full_preview
        if len(preview) > prev_len:
            preview = preview[:prev_len].rstrip() + "…" if prev_len > 0 else ""
        blocks = [f"<b>{html.escape(title)}</b>", html.escape("\n".join(header), quote=False)]
        if preview:
            blocks.append(html.escape(preview))
        alt = alt_block(alt_len)
        if alt:
            blocks.append(html.escape(alt))
        fcl = _fact_check_lines(fc, max_issues)
        if fcl:
            blocks.append("\n".join(html.escape(x) for x in fcl))
        blocks.append(html.escape(story_url))
        blocks.append("TikTok caption (tap to copy): <code>" + html.escape(tt[:tt_len]) + "</code>")
        return "\n\n".join(blocks)

    prev_len, alt_len, tt_len, max_issues = len(full_preview), 90, len(tt), 5
    cap = build(prev_len, alt_len, tt_len, max_issues)
    while len(cap) > 1024 and prev_len > 0:  # trim the script preview first
        prev_len = max(0, prev_len - 40)
        cap = build(prev_len, alt_len, tt_len, max_issues)
    while len(cap) > 1024 and alt_len > 0:  # then the alt hooks
        alt_len = alt_len - 20 if alt_len > 30 else 0
        cap = build(prev_len, alt_len, tt_len, max_issues)
    while len(cap) > 1024 and tt_len > 0:
        tt_len = max(0, tt_len - 40)
        cap = build(prev_len, alt_len, tt_len, max_issues)
    while len(cap) > 1024 and max_issues > 0:
        max_issues -= 1
        cap = build(prev_len, alt_len, tt_len, max_issues)
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


def _series_items(cfg: Config) -> list[dict]:
    items = cfg.get("series.items", None) or []
    return [dict(i) for i in items if isinstance(i, dict) and i.get("name")]


def _series_options(cfg: Config, conn) -> list[dict]:
    """Configured series still allowed this week (per_week caps counted over the last 7 days)."""
    if not cfg.get("series.enabled", True):
        return []
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 7 * 86400))
    out = []
    for item in _series_items(cfg):
        cap = item.get("per_week")
        if cap is not None:
            try:
                if db.count_drafts_since(conn, since, series=item["name"]) >= int(cap):
                    continue
            except (TypeError, ValueError):
                pass
        out.append(item)
    return out


def _replace_first_paragraph(text: str, new: str) -> str:
    from .tags import split_paragraphs

    paras = split_paragraphs(text)
    return "\n\n".join([" ".join(new.split())] + paras[1:])


def _kev_cve(kev) -> str | None:
    if isinstance(kev, dict):
        for k in ("cveID", "cve_id", "cve"):
            if kev.get(k):
                return str(kev[k])
    elif kev:
        return str(kev)
    return None


def _prefix_title(cfg: Config, title: str) -> str:
    prefix = str(cfg.get("breaking.title_prefix", "🚨 ") or "")
    if not prefix or title.startswith(prefix):
        return title
    body = re.sub(r"\s*#Shorts\s*$", "", title, flags=re.I)
    room = 95 - len(" #Shorts") - len(prefix)
    if len(body) > room:
        body = body[: room - 1].rstrip() + "…"
    return f"{prefix}{body} #Shorts"


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
        except Exception as e:
            log.error("Draft creation failed before draft row: %s", _short_error(cfg, e))
            _notify(cfg, f"Scheduled draft failed: {_short_error(cfg, e)}")
            raise
    finally:
        conn.close()
    return make_draft_for_story(cfg, story)


def make_draft_for_story(cfg: Config, story: dict, *, kind: str = "regular", kev=None) -> int:
    """Write, fact-check, voice and render one draft for `story`. Callers hold the draft lock."""
    breaking = kind == "breaking"
    conn = db.connect(cfg)
    try:
        db.init(conn)
        try:
            db.upsert_story(conn, story["url"], story["title"], story.get("source", ""),
                            story.get("published"), story.get("summary", ""))
            options = _series_options(cfg, conn)
            if cfg.get("news.fetch_full_text", True) and "article" not in story:
                from . import article
                story = dict(story, article=article.fetch_text(
                    story["url"], int(cfg.get("news.max_article_chars", 6000))))
            kwargs = {}
            if options:
                kwargs["series_options"] = options
            if breaking:
                kwargs["breaking"] = True
            data = writer.write_script(cfg, story, **kwargs)
            hooks = data.get("hooks") or []
            if len(hooks) > 1:
                idx = writer.pick_hook(cfg, story, hooks)
                if not isinstance(idx, int) or not 0 <= idx < len(hooks):
                    idx = 0
                if idx != 0:
                    data["script"] = _replace_first_paragraph(data["script"], hooks[idx])
                    if data.get("voice_script"):
                        data["voice_script"] = _replace_first_paragraph(data["voice_script"], hooks[idx])
                data["hook_index"] = idx
            if cfg.get("llm.fact_check", True):
                data["fact_check"] = writer.fact_check(cfg, story, data)
            data["data_card"] = _confirmed_card(cfg, story, data.get("data_card"))
            if not data["data_card"]:
                data.pop("data_card")
        except Exception as e:
            log.error("Draft creation failed before draft row: %s", _short_error(cfg, e))
            _notify(cfg, f"Scheduled draft failed: {_short_error(cfg, e)}")
            raise
        data["source_url"] = story["url"]
        data["source_title"] = story["title"]
        if breaking:
            data["breaking"] = True
            data["youtube_title"] = _prefix_title(cfg, data["youtube_title"])
        mode = cfg.get("voice.mode", "tts")
        status = "awaiting_voice" if mode == "record" else "rendered"
        draft_id = db.create_draft(conn, story["url"], status, json.dumps(data),
                                   kind=kind, series=data.get("series") or None)
        db.mark_used(conn, story["url"])
        # only the CVEs this draft is about: card, KEV, title, generated description (not roundup mentions)
        cve_src = "\n".join([story.get("title") or "", str(data.get("description") or "")])
        found = [c.upper() for c in CVE_RE.findall(cve_src)]
        main = [str(c).upper() for c in [(data.get("data_card") or {}).get("cve_id"), _kev_cve(kev)] if c]
        for cve in dict.fromkeys(main + found):
            try:
                db.mark_cve_used(conn, cve, draft_id)
            except Exception as e:  # noqa: BLE001
                log.warning("mark_cve_used failed: %s", type(e).__name__)
    finally:
        conn.close()

    if mode == "record":
        _notify(cfg, f"Draft #{draft_id} awaiting voice.\n\n{data['on_screen_title']}\n\n"
                     f"{data['script']}\n\nReply with a voice message to record this one.")
        return draft_id

    try:
        _voice(cfg, draft_id, data)
    except Exception as e:
        _fail(cfg, draft_id, e)
        raise
    render_draft(cfg, draft_id)
    return draft_id


def _confirmed_card(cfg: Config, story: dict, raw) -> dict | None:
    """The LLM's data card, confirmed in code against the source/NVD/KEV; None if unconfirmed."""
    if not raw or not cfg.get("data_card.enabled", True):
        return None
    try:
        from . import cvedata

        card = cvedata.build_card(cfg, story, raw)
        return dict(card) if card else None
    except Exception as e:  # noqa: BLE001
        log.warning("Data card skipped: %s", _short_error(cfg, e))
        return None


def _voice(cfg: Config, draft_id: int, data: dict, wav: Path | None = None) -> Path:
    """Synthesize the draft's narration (voice_script only goes to TTS) and store audio_path."""
    from . import tts

    tts.ensure_voice(cfg)
    final = cfg.path("work") / f"draft_{draft_id}.wav"
    target = wav or final
    tts.synthesize(cfg, data.get("voice_script") or data["script"], target)
    if wav is None:
        conn = db.connect(cfg)
        try:
            db.update_draft(conn, draft_id, audio_path=str(final))
        finally:
            conn.close()
    return target


def swap_hook(cfg: Config, draft_id: int, n: int) -> Path | str:
    """Replace the draft's first paragraph with hook number `n` (1-based), re-voice and re-render.
    Only for rendered drafts in tts voice mode. Returns a message string (no-op) if hook n is already in use. The old audio is replaced only once the new one exists."""
    conn = db.connect(cfg)
    try:
        row = db.get_draft(conn, draft_id)
    finally:
        conn.close()
    if row is None:
        raise ValueError(f"Draft {draft_id} not found")
    if row["status"] != "rendered":
        raise ValueError(f"Draft {draft_id} is {row['status']}; hooks can only be swapped before publishing")
    if cfg.get("voice.mode", "tts") != "tts":
        raise ValueError("Hook swap needs voice.mode: tts")
    data = _draft_data(row)
    hooks = data.get("hooks") or []
    if not isinstance(n, int) or not 1 <= n <= len(hooks):
        raise ValueError(f"Draft {draft_id} has no hook {n}")
    if data.get("hook_index") == n - 1:
        return f"Draft {draft_id} already uses hook {n}."
    hook = hooks[n - 1]
    data["script"] = _replace_first_paragraph(data.get("script", ""), hook)
    if data.get("voice_script"):
        data["voice_script"] = _replace_first_paragraph(data["voice_script"], hook)
    data["hook_index"] = n - 1

    import shutil

    from .music import music_version
    from .tts import spans_path

    work = cfg.path("work")
    wav = work / f"draft_{draft_id}.wav"
    tmp = work / f"draft_{draft_id}_swap.wav"
    mp4 = cfg.path("videos") / f"draft_{draft_id}.mp4"
    originals = [wav, spans_path(wav), mp4, music_version(cfg, draft_id)]
    backups: list[tuple[Path, Path]] = []
    try:
        for src in originals:
            if src.exists():
                bak = src.with_name(src.name + ".swapbak")
                shutil.copy2(src, bak)
                backups.append((src, bak))
    except OSError:
        for _, bak in backups:
            bak.unlink(missing_ok=True)
        raise
    old_json, old_audio = row["data_json"], row["audio_path"]

    def discard_backups():
        for _, bak in backups:
            bak.unlink(missing_ok=True)

    try:
        _voice(cfg, draft_id, data, wav=tmp)
        spans_path(wav).unlink(missing_ok=True)
        wav.unlink(missing_ok=True)
        tmp.replace(wav)
        if spans_path(tmp).exists():
            spans_path(tmp).replace(spans_path(wav))
    except BaseException:
        discard_backups()
        raise
    finally:
        tmp.unlink(missing_ok=True)
        spans_path(tmp).unlink(missing_ok=True)
    conn = db.connect(cfg)
    try:
        db.update_draft(conn, draft_id, data_json=json.dumps(data), audio_path=str(wav))
    finally:
        conn.close()
    try:
        out = render_draft(cfg, draft_id, fail_on_error=False)
    except Exception:
        log.exception("Hook swap render failed for draft %s; restoring the previous version", draft_id)
        for src, bak in backups:
            try:
                bak.replace(src)
            except OSError:
                log.warning("Could not restore %s", src.name)
        for src in originals:  # drop files the old draft did not have (new spans, music version)
            if not any(s == src for s, _ in backups):
                src.unlink(missing_ok=True)
        conn = db.connect(cfg)
        try:
            db.update_draft(conn, draft_id, data_json=old_json, audio_path=old_audio,
                            status="rendered")
        finally:
            conn.close()
        discard_backups()
        raise
    discard_backups()
    return out


def _fail(cfg: Config, draft_id: int, err: Exception) -> None:
    log.error("Draft %s failed: %s", draft_id, err)
    conn = db.connect(cfg)
    try:
        db.update_draft(conn, draft_id, status="failed", error=str(err)[:1000])
    finally:
        conn.close()
    _notify(cfg, f"Draft {draft_id} failed: {str(err)[:500]}")


def _cleanup_broll(cfg: Config, draft_id: int, clips: list[Path]) -> None:
    """Delete downloaded b-roll for this draft; never touch files outside the work dir."""
    work = cfg.path("work").resolve()
    victims = list(work.glob(f"draft_{draft_id}_broll_*.mp4")) + list(work.glob(f"draft_{draft_id}_broll_*.part"))
    for c in clips:
        try:
            if Path(c).resolve().parent == work:
                victims.append(Path(c))
        except OSError:
            pass
    for v in set(victims):
        try:
            if v.resolve().parent == work:
                v.unlink()
        except OSError as e:
            log.warning("Could not delete %s: %s", v.name, e)


def _overlay_kwargs(cfg: Config, data: dict) -> dict:
    """Badge (breaking first, else series) and data card kwargs for captions.write_ass."""
    extra: dict = {}
    if data.get("breaking"):
        extra["badge"] = str(cfg.get("breaking.badge", "BREAKING") or "BREAKING")
        extra["badge_color"] = cfg.get("breaking.badge_color", "#FF3B30")
    elif data.get("series"):
        for item in _series_items(cfg):
            if str(item["name"]).lower() == str(data["series"]).lower() and item.get("badge"):
                extra["badge"] = str(item["badge"])
                break
    if cfg.get("data_card.enabled", True) and data.get("data_card"):
        extra["card"] = data["data_card"]
    return extra


def _review_keyboard(cfg: Config, draft_id: int, data: dict) -> dict:
    from .telegram import keyboard

    return keyboard(draft_id, yt_enabled=cfg.get("youtube.enabled", True),
                    tt_enabled=cfg.get("tiktok.enabled", True),
                    alt_hooks=_alt_hook_numbers(data) if cfg.get("voice.mode", "tts") == "tts" else ())


def press_hook(cfg: Config, tg, draft_id: int, hook_n: int, message_id, lock_timeout: float = 5) -> str:
    """Handle a "Use hook N" button press; returns the text to send back to the chat.
    Ignores presses on stale messages, keeps the keyboard when the swap cannot run or fails."""
    conn = db.connect(cfg)
    try:
        row = db.get_draft(conn, draft_id)
    finally:
        conn.close()
    if row is None:
        return f"Draft {draft_id} not found."
    if message_id is None or row["tg_message_id"] != message_id:
        return "That preview is out of date; use the newest message for this draft."

    def set_keyboard(kb):
        try:
            tg.edit_reply_markup(message_id, kb)
        except Exception:  # noqa: BLE001
            log.warning("Could not update buttons on message %s", message_id, exc_info=True)

    def restore():
        conn = db.connect(cfg)
        try:
            cur = db.get_draft(conn, draft_id)
        finally:
            conn.close()
        if cur is not None and cur["tg_message_id"] == message_id and cur["status"] == "rendered":
            set_keyboard(_review_keyboard(cfg, draft_id, _draft_data(cur)))

    set_keyboard(None)  # one press at a time
    try:
        with draft_lock(cfg, blocking=True, timeout=lock_timeout):
            res = swap_hook(cfg, draft_id, hook_n)
    except Busy:
        restore()
        return "Another draft job is still running; try the hook button again in a moment."
    except Exception as e:  # noqa: BLE001
        log.exception("swap_hook failed")
        restore()
        return "Hook swap failed for draft %s: %s" % (draft_id, html.escape(_short_error(cfg, e), quote=False))
    if isinstance(res, str):
        restore()
        return res
    return f"Hook {hook_n} applied to draft {draft_id}; the new preview is below."


def render_draft(cfg: Config, draft_id: int, fail_on_error: bool = True) -> Path:
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
        from .tts import load_spans
        spans = load_spans(wav)
        words = captions.transcribe_by_spans(cfg, wav, spans, data.get("script", "")) if spans else None
        if words is None:  # recorded voice, or older drafts without span data
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
        extra = _overlay_kwargs(cfg, data)
        captions.write_ass(chunks, cfg, data.get("on_screen_title", ""),
                           cfg.get("channel.handle", ""), duration + render.PAD_SECONDS, ass,
                           words=words, keywords=data.get("keywords"), beats=beats, **extra)
        out = cfg.path("videos") / f"draft_{draft_id}.mp4"
        try:
            render.render(cfg, wav, ass, clips or None, out, beats=beats)
        except RuntimeError as e:
            if not clips:
                raise
            log.warning("Render with b-roll failed (%s); retrying without b-roll", e)
            render.render(cfg, wav, ass, None, out, beats=beats, watermark=False)
        _cleanup_broll(cfg, draft_id, clips)

        from . import music
        with_music, track = music.add_music(cfg, draft_id, out)
        preview = with_music or out  # Telegram shows what YouTube will get

        caption = _telegram_caption(data, row["story_url"], draft_id)
        if with_music:
            note = "\n" + html.escape(f"🎵 {track}" + ("" if cfg.get("music.on_tiktok", False)
                                                       else " (YouTube only; add a TikTok sound in the app)"), quote=False)
            if len(caption) + len(note) <= 1024:
                caption += note
        kb = _review_keyboard(cfg, draft_id, data)
        tg = _telegram(cfg)
        if preview.stat().st_size > MAX_TG_VIDEO_BYTES:
            resp = tg.send_message(
                caption[:3800] + "\n\n" + html.escape(
                    f"Video is too large for Telegram ({preview.stat().st_size // (1024 * 1024)} MB). "
                    f"Server path: {preview}", quote=False), kb)
        else:
            resp = tg.send_video(str(preview), caption, kb)
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
        if fail_on_error:
            _fail(cfg, draft_id, e)
        raise


def attach_voice(cfg: Config, draft_id: int, ogg_path: Path | str) -> Path:
    wav = cfg.path("work") / f"draft_{draft_id}.wav"
    from .tts import spans_path
    spans_path(wav).unlink(missing_ok=True)  # a recorded voice has no known beat timings
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
        video = Path(row["video_path"])  # clean master (no music)
        from . import music
        mv = music.music_version(cfg, draft_id)
        video_yt = mv if mv.exists() else video
        video_tt = mv if (mv.exists() and cfg.get("music.on_tiktok", False)) else video
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

            vid = youtube.upload(cfg, video_yt, data["youtube_title"], data["description"],
                                 data.get("hashtags", []))
            fields["youtube_id"] = vid
            _save_fields(cfg, draft_id, youtube_id=vid)
            ok.append("yt")
            lines.append(f"YouTube: https://youtube.com/shorts/{vid}")
            note = _first_comment(cfg, draft_id, vid, data)
            if note:
                lines.append(note)
        except Exception as e:
            log.error("YouTube upload failed: %s", e)
            errors.append(f"YouTube: {e}")
            lines.append(f"YouTube failed: {e}")
    if "tt" in targets and "tt" not in ok:
        try:
            from . import tiktok

            pid = tiktok.upload_draft(cfg, video_tt)
            fields["tiktok_publish_id"] = pid
            _save_fields(cfg, draft_id, tiktok_publish_id=pid)
            ok.append("tt")
            lines.append("TikTok: Draft sent to your TikTok inbox — open TikTok to post (expires ~24h)")
            lines.append("Caption is in the next message: tap it to copy, then paste it in TikTok")
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


def _first_comment(cfg: Config, draft_id: int, vid: str, data: dict) -> str:
    """Post the first comment. Never raises and never affects the publish result."""
    text = str(data.get("first_comment") or "").strip()
    if not cfg.get("youtube.first_comment", True) or not text:
        return ""
    try:
        from . import youtube

        cid, reason = youtube.post_comment(cfg, vid, text)
    except Exception as e:  # noqa: BLE001
        return f"Comment skipped: {_short_error(cfg, e, 100)}"
    if not cid:
        return f"Comment skipped: {reason}"
    try:
        _save_fields(cfg, draft_id, yt_comment_id=cid)
    except Exception as e:  # noqa: BLE001
        log.warning("Could not store comment id: %s", type(e).__name__)
    return "💬 First comment posted. Pin it in the YouTube app (the API can't pin comments)."


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
