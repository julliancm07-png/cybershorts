"""TikTok Content Posting API: inbox (draft) upload via FILE_UPLOAD."""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
from pathlib import Path

import requests

log = logging.getLogger(__name__)

TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
INIT_URL = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
SCOPES = "user.info.basic,video.upload"

MB = 1024 * 1024
MAX_SINGLE = 64 * MB
CHUNK = 10 * MB


def token_path(cfg) -> Path:
    return Path(cfg.path("tiktok_token.json"))


def auth_url(cfg, state: str) -> str:
    q = {
        "client_key": cfg.tiktok_client_key,
        "scope": SCOPES,
        "response_type": "code",
        "redirect_uri": cfg.tiktok_redirect_uri,
        "state": state,
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(q, safe=",")


def _save_token(cfg, data: dict, old: dict | None = None) -> dict:
    now = int(time.time())
    tok = dict(old or {})
    for k in ("access_token", "refresh_token", "open_id"):
        if data.get(k):
            tok[k] = data[k]
    tok["expires_at"] = now + int(data.get("expires_in", 0) or 0)
    tok["refresh_expires_at"] = now + int(data.get("refresh_expires_in", 0) or 0)
    path = token_path(cfg)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(tok, indent=2))
    os.replace(tmp, path)
    return tok


def _token_request(cfg, fields: dict) -> dict:
    body = {"client_key": cfg.tiktok_client_key, "client_secret": cfg.tiktok_client_secret, **fields}
    try:
        r = requests.post(
            TOKEN_URL,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        raise RuntimeError(f"TikTok token request failed: {type(e).__name__}") from None
    if data.get("error") or not data.get("access_token"):
        raise RuntimeError(
            f"TikTok token error: {data.get('error')} {data.get('error_description', '')}".strip()
        )
    return data


def exchange_code(cfg, code: str) -> dict:
    data = _token_request(
        cfg,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": cfg.tiktok_redirect_uri,
        },
    )
    return _save_token(cfg, data)


def load_token(cfg) -> dict:
    path = token_path(cfg)
    if not path.exists():
        raise RuntimeError(f"{path} not found; run scripts/auth_tiktok.py first")
    return json.loads(path.read_text())


def refresh(cfg) -> dict:
    old = load_token(cfg)
    if not old.get("refresh_token"):
        raise RuntimeError("TikTok token has no refresh_token; re-run scripts/auth_tiktok.py")
    if old.get("refresh_expires_at") and old["refresh_expires_at"] < time.time():
        raise RuntimeError("TikTok refresh token expired; re-run scripts/auth_tiktok.py")
    data = _token_request(cfg, {"grant_type": "refresh_token", "refresh_token": old["refresh_token"]})
    return _save_token(cfg, data, old)


def get_token(cfg) -> str:
    tok = load_token(cfg)
    if tok.get("expires_at", 0) - time.time() < 300:
        tok = refresh(cfg)
    return tok["access_token"]


def _check(data: dict, what: str) -> None:
    err = data.get("error") or {}
    if err.get("code", "ok") != "ok":
        raise RuntimeError(f"TikTok {what} error: {err.get('code')}: {err.get('message', '')}")


def plan_chunks(size: int) -> tuple[int, int]:
    """Return (chunk_size, total_chunk_count) per TikTok rules."""
    if size <= MAX_SINGLE:
        return size, 1
    return CHUNK, size // CHUNK


def upload_draft(cfg, video) -> str:
    video = Path(video)
    size = video.stat().st_size
    if size <= 0:
        raise RuntimeError("Video file is empty")
    chunk_size, count = plan_chunks(size)
    token = get_token(cfg)
    auth = {"Authorization": f"Bearer {token}"}

    try:
        r = requests.post(
            INIT_URL,
            json={
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": size,
                    "chunk_size": chunk_size,
                    "total_chunk_count": count,
                }
            },
            headers={**auth, "Content-Type": "application/json; charset=UTF-8"},
            timeout=30,
        )
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        raise RuntimeError(f"TikTok init failed: {type(e).__name__}") from None
    _check(data, "init")
    info = data.get("data") or {}
    publish_id, upload_url = info.get("publish_id"), info.get("upload_url")
    if not publish_id or not upload_url:
        raise RuntimeError("TikTok init returned no publish_id/upload_url")

    with open(video, "rb") as f:
        for i in range(count):
            start = i * chunk_size
            end = size - 1 if i == count - 1 else start + chunk_size - 1
            f.seek(start)
            blob = f.read(end - start + 1)
            try:
                pr = requests.put(
                    upload_url,
                    data=blob,
                    headers={
                        "Content-Type": "video/mp4",
                        "Content-Length": str(len(blob)),
                        "Content-Range": f"bytes {start}-{end}/{size}",
                    },
                    timeout=300,
                )
            except requests.RequestException as e:
                raise RuntimeError(f"TikTok chunk {i + 1}/{count} upload failed: {type(e).__name__}") from None
            if pr.status_code not in (200, 201, 206):
                raise RuntimeError(f"TikTok chunk {i + 1}/{count} upload failed: HTTP {pr.status_code}")

    deadline = time.time() + 180
    status = ""
    while time.time() < deadline:
        try:
            sr = requests.post(
                STATUS_URL,
                json={"publish_id": publish_id},
                headers={**auth, "Content-Type": "application/json; charset=UTF-8"},
                timeout=30,
            )
            sd = sr.json()
        except (requests.RequestException, ValueError) as e:
            log.warning("TikTok status poll error: %s", type(e).__name__)
            time.sleep(5)
            continue
        _check(sd, "status")
        status = (sd.get("data") or {}).get("status", "")
        if status in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"):
            return publish_id
        if status == "FAILED":
            reason = (sd.get("data") or {}).get("fail_reason", "unknown")
            raise RuntimeError(f"TikTok upload failed: {reason}")
        time.sleep(5)
    raise RuntimeError(f"TikTok status poll timed out (last status: {status or 'none'})")
