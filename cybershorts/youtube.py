"""YouTube Data API upload."""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def token_path(cfg) -> Path:
    return Path(cfg.path("youtube_token.json"))


def _clean(text: str) -> str:
    """YouTube rejects < and > in titles/descriptions."""
    return (text or "").replace("<", "").replace(">", "")


def _save(creds, path: Path) -> None:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(creds.to_json())
    os.replace(tmp, path)


def get_service(cfg):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    path = token_path(cfg)
    if not path.exists():
        raise RuntimeError(f"{path} not found; run scripts/auth_youtube.py first")
    creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    if not creds.valid:
        if creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(f"YouTube token refresh failed: {e}. Re-run scripts/auth_youtube.py") from e
            _save(creds, path)
        else:
            raise RuntimeError("YouTube token has no refresh token; re-run scripts/auth_youtube.py")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def upload(cfg, video, title: str, description: str, tags: list[str] | None = None) -> str:
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    title = _clean(title)[:100]
    description = _clean(description)[:4900]
    yt = get_service(cfg)
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": list(tags or [])[:15],
            "categoryId": str(cfg.get("youtube.category_id", "28")),
        },
        "status": {
            "privacyStatus": cfg.get("youtube.privacy", "public"),
            "selfDeclaredMadeForKids": bool(cfg.get("youtube.made_for_kids", False)),
            "containsSyntheticMedia": bool(cfg.get("youtube.contains_synthetic_media", False)),
        },
    }
    media = MediaFileUpload(str(video), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    request = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    retries = 0
    while response is None:
        try:
            _, response = request.next_chunk()
        except HttpError as e:
            if e.resp.status >= 500 and retries < 3:
                retries += 1
                log.warning("YouTube upload 5xx (%s), retry %d/3", e.resp.status, retries)
                time.sleep(2 ** retries)
                continue
            raise RuntimeError(f"YouTube upload failed: HTTP {e.resp.status} {e}") from e
    vid = response.get("id")
    if not vid:
        raise RuntimeError(f"YouTube upload returned no id: {response}")
    return vid
