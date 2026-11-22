"""Thin Telegram Bot API client (requests only)."""
from __future__ import annotations

import html
import json
import logging
import time
from pathlib import Path

import requests

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
RETRY_DELAYS = (3, 8, 20)  # seconds between attempts on network errors


def esc(text: str) -> str:
    """Escape text for HTML parse_mode."""
    return html.escape(text or "", quote=False)


def keyboard(draft_id, yt_enabled: bool = True, tt_enabled: bool = True,
             manual_fallback: bool = True, *, alt_hooks=()) -> dict:
    """Review keyboard. Pass yt/tt_enabled=False to omit already-done or disabled targets.

    alt_hooks: hook numbers (ints); one "Use hook N" button each (callback hook:N:id)."""
    rows = []
    if yt_enabled and tt_enabled:
        rows.append([{"text": "✅ Post both", "callback_data": f"pub:all:{draft_id}"}])
    row2 = []
    if yt_enabled:
        row2.append({"text": "▶️ YouTube only", "callback_data": f"pub:yt:{draft_id}"})
    if tt_enabled:
        row2.append({"text": "\U0001f3b5 TikTok only", "callback_data": f"pub:tt:{draft_id}"})
    if row2:
        rows.append(row2)
    if manual_fallback and not yt_enabled and not tt_enabled:
        rows.append([{"text": "\U0001f4e5 Posted manually", "callback_data": f"man:{draft_id}"}])
    hook_row = []
    for n in alt_hooks or ():
        hook_row.append({"text": f"\U0001fa9d Use hook {n}", "callback_data": f"hook:{n}:{draft_id}"})
    if hook_row:
        rows.append(hook_row)
    rows.append([{"text": "❌ Reject", "callback_data": f"rej:{draft_id}"}])
    return {"inline_keyboard": rows}


def recap_keyboard(draft_id) -> dict:
    """Review keyboard for a weekly recap: YouTube only (no TikTok)."""
    return {"inline_keyboard": [
        [{"text": "▶️ Publish to YouTube", "callback_data": f"pub:yt:{draft_id}"}],
        [{"text": "❌ Reject", "callback_data": f"rej:{draft_id}"}],
    ]}


class Telegram:
    def __init__(self, token: str, chat_id):
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
        self.token = token
        self.chat_id = str(chat_id) if chat_id is not None else ""
        self.base = f"{API}/bot{token}"

    # -- internals -------------------------------------------------------
    def _why(self, e: Exception) -> str:
        """Short, token-free description of a network error (e.g. DNS vs. reset)."""
        detail = str(e).replace(self.token, "***")
        for hint in ("NameResolutionError", "Failed to resolve", "Connection reset", "Connection refused",
                     "Network is unreachable", "timed out", "RemoteDisconnected", "Max retries"):
            if hint in detail:
                return f"{type(e).__name__} ({hint})"
        return type(e).__name__

    def _call(self, method: str, data=None, files=None, timeout: int = 30, retries: bool = True):
        """POST to the Bot API. `files` may be a dict or a zero-arg callable returning one
        (so file uploads can be re-opened on retry). Network errors are retried."""
        url = f"{self.base}/{method}"
        delays = RETRY_DELAYS if retries else ()
        attempt = 0
        while True:
            try:
                f = files() if callable(files) else files
                try:
                    r = requests.post(url, data=data, files=f, timeout=timeout)
                finally:
                    if callable(files) and f:
                        for v in f.values():
                            fh = v[1] if isinstance(v, tuple) else v
                            if hasattr(fh, "close"):
                                fh.close()
                break
            except (requests.ConnectionError, requests.Timeout) as e:
                why = self._why(e)
                if attempt >= len(delays):
                    # never leak the token in the message
                    raise RuntimeError(f"Telegram {method} request failed: {why}") from None
                log.warning("Telegram %s failed (%s); retrying in %ss", method, why, delays[attempt])
                time.sleep(delays[attempt])
                attempt += 1
            except requests.RequestException as e:
                raise RuntimeError(f"Telegram {method} request failed: {self._why(e)}") from None
        try:
            payload = r.json()
        except ValueError:
            raise RuntimeError(f"Telegram {method}: non-JSON response (HTTP {r.status_code})") from None
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {payload.get('description', 'unknown error')}")
        return payload.get("result")

    @staticmethod
    def _markup(reply_markup):
        return json.dumps(reply_markup) if reply_markup else None

    # -- API -------------------------------------------------------------
    def get_me(self) -> dict:
        return self._call("getMe", timeout=15)

    def send_message(self, text: str, reply_markup=None) -> dict:
        data = {
            "chat_id": self.chat_id,
            "text": text[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
        if reply_markup:
            data["reply_markup"] = self._markup(reply_markup)
        try:
            return self._call("sendMessage", data=data)
        except RuntimeError as e:
            if "parse entities" not in str(e):
                raise
            data.pop("parse_mode")  # fall back to plain text
            return self._call("sendMessage", data=data)

    def send_document(self, path, caption: str = "") -> dict:
        data = {"chat_id": self.chat_id, "caption": (caption or "")[:1024], "parse_mode": "HTML"}
        name = Path(path).name
        return self._call("sendDocument", data=data, timeout=300,
                          files=lambda: {"document": (name, open(path, "rb"), "application/octet-stream")})

    def send_audio(self, path, caption: str = "", title: str | None = None,
                   performer: str | None = None) -> dict:
        data = {"chat_id": self.chat_id, "caption": (caption or "")[:1024], "parse_mode": "HTML"}
        if title:
            data["title"] = title
        if performer:
            data["performer"] = performer
        name = Path(path).name
        return self._call("sendAudio", data=data, timeout=300,
                          files=lambda: {"audio": (name, open(path, "rb"), "audio/mpeg")})

    def send_photo(self, path, caption: str = "") -> dict:
        data = {"chat_id": self.chat_id, "caption": (caption or "")[:1024], "parse_mode": "HTML"}
        name = Path(path).name
        return self._call("sendPhoto", data=data, timeout=60,
                          files=lambda: {"photo": (name, open(path, "rb"), "image/jpeg")})

    def send_video(self, path, caption: str, reply_markup=None) -> dict:
        data = {
            "chat_id": self.chat_id,
            "caption": (caption or "")[:1024],
            "parse_mode": "HTML",
            "supports_streaming": "true",
        }
        if reply_markup:
            data["reply_markup"] = self._markup(reply_markup)
        name = Path(path).name
        return self._call("sendVideo", data=data, timeout=300,
                          files=lambda: {"video": (name, open(path, "rb"), "video/mp4")})

    def edit_caption(self, message_id, caption: str, reply_markup=None) -> dict:
        data = {
            "chat_id": self.chat_id,
            "message_id": message_id,
            "caption": (caption or "")[:1024],
            "parse_mode": "HTML",
        }
        # omitting reply_markup leaves the keyboard; send empty markup to remove it
        data["reply_markup"] = self._markup(reply_markup) if reply_markup else json.dumps({"inline_keyboard": []})
        return self._call("editMessageCaption", data=data)

    def edit_reply_markup(self, message_id, reply_markup=None) -> dict:
        """Replace only the keyboard of a message (None removes it); the body is untouched."""
        data = {
            "chat_id": self.chat_id,
            "message_id": message_id,
            "reply_markup": self._markup(reply_markup) if reply_markup else json.dumps({"inline_keyboard": []}),
        }
        return self._call("editMessageReplyMarkup", data=data)

    def edit_text(self, message_id, text: str) -> dict:
        """Replace a text message's body and remove its keyboard."""
        data = {
            "chat_id": self.chat_id,
            "message_id": message_id,
            "text": (text or "")[:4096],
            "parse_mode": "HTML",
            "reply_markup": json.dumps({"inline_keyboard": []}),
        }
        return self._call("editMessageText", data=data)

    def answer_callback(self, callback_id: str, text: str = "") -> bool:
        return self._call("answerCallbackQuery", data={"callback_query_id": callback_id, "text": text[:200]}, timeout=15)

    def get_updates(self, offset: int | None = None, timeout: int = 50) -> list:
        data = {"timeout": timeout, "allowed_updates": json.dumps(["message", "callback_query"])}
        if offset is not None:
            data["offset"] = offset
        return self._call("getUpdates", data=data, timeout=timeout + 10, retries=False) or []  # bot loop retries

    def download_file(self, file_id: str, dest) -> Path:
        info = self._call("getFile", data={"file_id": file_id}, timeout=20)
        file_path = info.get("file_path")
        if not file_path:
            raise RuntimeError("Telegram getFile returned no file_path")
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with requests.get(f"{API}/file/bot{self.token}/{file_path}", stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(65536):
                        f.write(chunk)
        except requests.RequestException as e:
            raise RuntimeError(f"Telegram file download failed: {type(e).__name__}") from None
        return dest
