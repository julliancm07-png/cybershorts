#!/usr/bin/env python3
"""Long-running Telegram bot: approve/reject drafts, voice replies, /draft, /status."""
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import logging

import time
import traceback

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")


def _read_offset(path: Path):
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _write_offset(path: Path, value: int) -> None:
    try:
        path.write_text(str(value))
    except OSError as e:
        log.warning("Could not persist offset: %s", e)


def main() -> int:
    from cybershorts import db, pipeline
    from cybershorts.config import load
    from cybershorts.telegram import Telegram, esc, keyboard, recap_keyboard

    cfg = load()
    if not cfg.telegram_bot_token or not cfg.telegram_chat_id:
        log.error("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env")
        return 2
    conn = db.connect(cfg)
    try:
        db.init(conn)  # apply migrations before any publish/status handler runs
    finally:
        conn.close()
    tg = Telegram(cfg.telegram_bot_token, cfg.telegram_chat_id)
    allowed = str(cfg.telegram_chat_id)
    offset_file = cfg.path("bot_offset")
    offset = _read_offset(offset_file)

    def reply(text):
        try:
            tg.send_message(text)
        except Exception:  # noqa: BLE001
            log.exception("Failed to send Telegram message")

    def answer(cb_id, text=""):
        """Best-effort: a stale ('query is too old') callback must not abort the action."""
        try:
            tg.answer_callback(cb_id, text)
        except Exception as e:  # noqa: BLE001
            log.warning("answer_callback failed: %s", e)

    def offer_retry(draft_id, requested):
        """After failed/partial publish, offer a keyboard for targets not yet done."""
        conn = db.connect(cfg)
        try:
            row = db.get_draft(conn, draft_id)
        finally:
            conn.close()
        if row is None or row["status"] not in ("failed", "partial"):
            return
        yt_left = "yt" in requested and not row["youtube_id"]
        tt_left = "tt" in requested and not row["tiktok_publish_id"]
        if not (yt_left or tt_left):
            return
        try:
            if row["kind"] == "recap":  # YouTube only; Publish uses the same pub:yt button
                tg.send_message("Recap %s: not published. Retry?" % draft_id, recap_keyboard(draft_id))
                return
            tg.send_message(
                "Draft %s: retry remaining targets?" % draft_id,
                keyboard(draft_id, yt_enabled=yt_left, tt_enabled=tt_left, manual_fallback=False))
        except Exception:  # noqa: BLE001
            log.exception("Failed to send retry keyboard")

    def close_buttons(msg, text):
        """Remove the keyboard from the pressed message (video caption or plain text)."""
        msg_id = msg.get("message_id")
        try:
            if "text" in msg:
                tg.edit_text(msg_id, text)
            else:
                tg.edit_caption(msg_id, text)
        except Exception:  # noqa: BLE001
            log.warning("Could not clear buttons on message %s", msg_id, exc_info=True)

    def offer_stuck():
        """Drafts left in 'publishing' (bot restarted mid-upload) get a fresh keyboard."""
        try:
            stuck = pipeline.stuck_publishing(cfg)
        except Exception:  # noqa: BLE001
            log.exception("stuck_publishing check failed")
            return
        for draft_id in stuck:
            conn = db.connect(cfg)
            try:
                row = db.get_draft(conn, draft_id)
            finally:
                conn.close()
            yt_left = cfg.get("youtube.enabled", True) and not row["youtube_id"]
            tt_left = cfg.get("tiktok.enabled", True) and not row["tiktok_publish_id"]
            if row["kind"] == "recap":  # recaps are YouTube only
                tt_left = False
            kb = (recap_keyboard(draft_id) if row["kind"] == "recap" else
                  keyboard(draft_id, yt_enabled=yt_left, tt_enabled=tt_left,
                           manual_fallback=not (yt_left or tt_left)))
            try:
                tg.send_message("Draft %s was interrupted while publishing. Retry?" % draft_id, kb)
            except Exception:  # noqa: BLE001
                log.exception("Failed to send stuck-draft keyboard")

    def handle_callback(cb):
        cb_id = cb.get("id")
        data = cb.get("data") or ""
        msg = cb.get("message") or {}
        try:
            parts = data.split(":")
            action = parts[0]
            if action == "pub" and len(parts) == 3:
                kind, draft_id = parts[1], int(parts[2])
                base = {"all": {"yt", "tt"}, "yt": {"yt"}, "tt": {"tt"}}[kind]
            elif action in ("rej", "man") and len(parts) == 2:
                draft_id = int(parts[1])
            elif action == "hook" and len(parts) == 3:
                hook_n, draft_id = int(parts[1]), int(parts[2])
            else:
                raise ValueError("bad callback")
        except (ValueError, IndexError, KeyError):
            answer(cb_id, "Unknown action")
            return

        if action == "pub":
            targets = {t for t in base if cfg.get("youtube.enabled" if t == "yt" else "tiktok.enabled", True)}
            answer(cb_id, "Publishing...")
            close_buttons(msg, "Publishing draft %s..." % draft_id)
            try:
                summary = pipeline.publish(cfg, draft_id, targets)
            except Exception as e:  # noqa: BLE001
                log.exception("publish failed")
                summary = "Publish failed for draft %s: %s" % (draft_id, e)
            reply(esc(summary))
            if "tt" in targets:
                try:
                    cap = pipeline.tiktok_caption_for(cfg, draft_id)
                    if cap:
                        # <code> text is tap-to-copy in Telegram's mobile apps
                        tg.send_message("<code>" + esc(cap) + "</code>")
                except Exception:  # noqa: BLE001
                    log.warning("Could not send the TikTok caption", exc_info=True)
            offer_retry(draft_id, targets)
        elif action == "hook":
            answer(cb_id, "Swapping hook...")
            reply(pipeline.press_hook(cfg, tg, draft_id, hook_n, msg.get("message_id"), lock_timeout=5))
        elif action == "rej":
            try:
                pipeline.reject(cfg, draft_id)
            except ValueError as e:
                answer(cb_id, "Not rejected")
                close_buttons(msg, esc(str(e)))
                return
            answer(cb_id, "Rejected")
            close_buttons(msg, "Rejected (draft %s)" % draft_id)
        else:  # man
            answer(cb_id, "Marked as posted")
            pipeline.mark_manual(cfg, draft_id)
            close_buttons(msg, "Marked as posted manually (draft %s)" % draft_id)

    def handle_status():
        conn = db.connect(cfg)
        try:
            db.init(conn)
            rows = db.recent_drafts(conn, 5)
        finally:
            conn.close()
        if not rows:
            reply("No drafts yet.")
            return
        lines = []
        for r in rows:
            line = "#%s  %s  %s" % (r["id"], r["status"], r["updated_at"] or r["created_at"] or "")
            if r["error"]:
                line += "\n   error: %s" % esc(str(r["error"])[:150])
            lines.append(line)
        reply("<b>Last drafts</b>\n" + "\n".join(lines))

    def handle_voice(msg):
        conn = db.connect(cfg)
        try:
            db.init(conn)
            row = db.latest_awaiting_voice(conn)
        finally:
            conn.close()
        if row is None:
            reply("No draft is waiting for a voice recording.")
            return
        media = msg.get("voice") or msg.get("audio")
        ogg = cfg.path("work", "voice_%s.ogg" % row["id"])
        tg.download_file(media["file_id"], ogg)
        reply("Got it, rendering draft %s..." % row["id"])
        pipeline.attach_voice(cfg, row["id"], ogg)

    def handle_message(msg):
        text = (msg.get("text") or "").strip()
        cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        if cmd == "/draft":
            reply("Working on a draft...")
            try:
                with pipeline.draft_lock(cfg, blocking=True, timeout=5):
                    made = pipeline.make_draft(cfg)
            except pipeline.Busy:
                reply("A draft is already being made; try /draft again in a moment.")
                return
            if made is None:
                reply("No new stories right now.")
        elif cmd == "/recap":
            if not cfg.get("recap.enabled", True):
                reply("The weekly recap is turned off (recap.enabled: false).")
                return
            words = [w.lower() for w in text.split()[1:]]
            force = "force" in words
            week_n = None
            if "week" in words:
                i = words.index("week")
                if i + 1 < len(words) and words[i + 1].isdigit() and 0 < int(words[i + 1]) < 1000:
                    week_n = int(words[i + 1])
                else:
                    reply("Usage: /recap force week 2")
                    return
            reply("Making this week's recap%s (~20 min)..." % (" as Week %d" % week_n if week_n else ""))
            argv = [sys.executable, str(Path(__file__).resolve().parent / "recap.py"), "--on-demand"]
            if shutil.which("nice"):  # the render is heavy; keep the bot and the rest of the box responsive
                argv = ["nice", "-n", "10"] + argv
            if force:
                argv.append("--force")
            if week_n:
                argv += ["--week", str(week_n)]
            try:
                subprocess.Popen(argv, cwd=str(cfg.app_dir), start_new_session=True)
            except OSError as e:
                reply("Could not start the recap: %s" % esc(str(e)[:200]))
        elif cmd == "/status":
            handle_status()
            offer_stuck()
        elif cmd == "/start":
            reply("Cyber Shorts bot ready. Commands: /draft, /recap [force] [week N], /status.")
        elif msg.get("voice") or msg.get("audio"):
            handle_voice(msg)

    def handle_update(upd):
        if "callback_query" in upd:
            cb = upd["callback_query"]
            chat_id = (cb.get("message") or {}).get("chat", {}).get("id")
            if str(chat_id) != allowed:
                log.warning("Ignoring callback from unauthorized chat id %s", chat_id)
                try:
                    tg.answer_callback(cb.get("id"), "")
                except Exception:  # noqa: BLE001
                    pass
                return
            handle_callback(cb)
        elif "message" in upd:
            msg = upd["message"]
            chat_id = (msg.get("chat") or {}).get("id")
            if str(chat_id) != allowed:
                log.warning("Ignoring message from unauthorized chat id %s", chat_id)
                return
            handle_message(msg)

    log.info("Bot started")
    from cybershorts import health
    health.start_heartbeat(cfg)
    offer_stuck()
    while True:
        try:
            updates = tg.get_updates(offset, timeout=50)
        except Exception as e:  # noqa: BLE001
            log.warning("get_updates failed: %s", e)
            time.sleep(5)
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            _write_offset(offset_file, offset)
            try:
                handle_update(upd)
            except Exception as e:  # noqa: BLE001
                log.error("Error handling update: %s\n%s", e, traceback.format_exc())
                reply("Error: %s" % esc(str(e)[:300]))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
