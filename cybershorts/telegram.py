"""Thin Telegram Bot API client (requests only)."""
from __future__ import annotations

import html
import json
import logging
from pathlib import Path

import requests

log = logging.getLogger(__name__)

API = "https://api.telegram.org"


def esc(text: str) -> str:
    """Escape text for HTML parse_mode."""
    return html.escape(text or "", quote=False)


def keyboard(draft_id, yt_enabled: bool = True, tt_enabled: bool = True,
             manual_fallback: bool = True) -> dict:
    """Review keyboard. Pass yt/tt_enabled=False to omit already-done or disabled targets."""
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
    rows.append([{"text": "❌ Reject", "callback_data": f"rej:{draft_id}"}])
    return {"inline_keyboard": rows}


class Telegram:
    def __init__(self, token: str, chat_id):
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
        self.token = token
        self.chat_id = str(chat_id) if chat_id is not None else ""
        self.base = f"{API}/bot{token}"

    # -- internals -------------------------------------------------------
    def _call(self, method: str, data=None, files=None, timeout: int = 30):
        url = f"{self.base}/{method}"
        try:
            r = requests.post(url, data=data, files=files, timeout=timeout)
        except requests.RequestException as e:
            # never leak the token in the message
            raise RuntimeError(f"Telegram {method} request failed: {type(e).__name__}") from None
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

    def send_video(self, path, caption: str, reply_markup=None) -> dict:
        data = {
            "chat_id": self.chat_id,
            "caption": (caption or "")[:1024],
            "parse_mode": "HTML",
            "supports_streaming": "true",
        }
        if reply_markup:
            data["reply_markup"] = self._markup(reply_markup)
        with open(path, "rb") as f:
            return self._call("sendVideo", data=data, files={"video": (Path(path).name, f, "video/mp4")}, timeout=300)

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
        return self._call("getUpdates", data=data, timeout=timeout + 10) or []

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
