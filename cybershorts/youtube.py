"""YouTube Data API upload, first comments and analytics."""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

SCOPE_UPLOAD = "https://www.googleapis.com/auth/youtube.upload"
SCOPE_FORCE_SSL = "https://www.googleapis.com/auth/youtube.force-ssl"
SCOPE_ANALYTICS = "https://www.googleapis.com/auth/yt-analytics.readonly"
SCOPE_BROAD = "https://www.googleapis.com/auth/youtube"
SCOPES = [SCOPE_UPLOAD, SCOPE_FORCE_SSL, SCOPE_ANALYTICS]


def token_path(cfg) -> Path:
    return Path(cfg.path("youtube_token.json"))


def _clean(text: str) -> str:
    """YouTube rejects < and > in titles/descriptions."""
    return (text or "").replace("<", "").replace(">", "")


def _cap_bytes(text: str, limit: int) -> str:
    """YouTube's description limit is 5000 BYTES; cut on a character boundary."""
    b = text.encode("utf-8")
    return text if len(b) <= limit else b[:limit].decode("utf-8", "ignore")


def _save(creds, path: Path) -> None:
    path = Path(path)
    prior = {}
    try:
        prior = json.loads(path.read_text()).get("scopes") or []
    except (OSError, ValueError, AttributeError):
        pass
    try:
        data = json.loads(creds.to_json())
        if prior:  # a refresh must not rewrite the recorded (granted) scopes
            data["scopes"] = prior
        text = json.dumps(data)
    except ValueError:
        text = creds.to_json()
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def granted_scopes(cfg) -> set[str]:
    """Scopes recorded in the token file (no network). Empty set if unreadable."""
    try:
        data = json.loads(token_path(cfg).read_text())
    except (OSError, ValueError):
        return set()
    sc = data.get("scopes") if isinstance(data, dict) else None
    if isinstance(sc, str):
        sc = sc.split()
    return {str(x) for x in (sc or [])}


def has_scope(cfg, scope: str) -> bool:
    have = granted_scopes(cfg)
    if scope in have:
        return True
    # the broad youtube scope also covers comment/force-ssl calls
    return scope == SCOPE_FORCE_SSL and SCOPE_BROAD in have


def get_service(cfg, api: str = "youtube", version: str = "v3"):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    path = token_path(cfg)
    if not path.exists():
        raise RuntimeError(f"{path} not found; run scripts/auth_youtube.py first")
    # No scopes argument: passing one would make refresh request scopes the token never
    # granted (invalid_scope), which breaks upload-only tokens.
    creds = Credentials.from_authorized_user_file(str(path))
    if not creds.valid:
        if creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(f"YouTube token refresh failed: {e}. Re-run scripts/auth_youtube.py") from e
            _save(creds, path)
        else:
            raise RuntimeError("YouTube token has no refresh token; re-run scripts/auth_youtube.py")
    return build(api, version, credentials=creds, cache_discovery=False)


def upload(cfg, video, title: str, description: str, tags: list[str] | None = None, *,
           category_id: str | None = None) -> str:
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    title = _clean(title)[:100]
    description = _cap_bytes(_clean(description), 4900)
    yt = get_service(cfg)
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": list(tags or [])[:15],
            "categoryId": str(category_id if category_id else cfg.get("youtube.category_id", "28")),
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


def set_thumbnail(cfg, video_id: str, path) -> tuple[bool, str]:
    """Set a custom thumbnail (JPEG). Never raises. Returns (ok, reason)."""
    try:
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload

        if not path or not Path(path).exists():
            return False, "thumbnail file missing"
        yt = get_service(cfg)
        for attempt in (0, 1):
            try:
                media = MediaFileUpload(str(path), mimetype="image/jpeg")
                yt.thumbnails().set(videoId=video_id, media_body=media).execute()
                return True, "ok"
            except HttpError as e:
                status = getattr(e.resp, "status", 0)
                if status == 403:
                    return False, "channel not allowed custom thumbnails (verify phone)"
                if attempt == 0 and status >= 500:
                    log.warning("Thumbnail set failed (HTTP %s), retrying once", status)
                    time.sleep(5)
                    continue
                return False, f"HTTP {status}"
    except Exception as e:  # noqa: BLE001
        log.warning("Thumbnail failed: %s", type(e).__name__)
        return False, f"{type(e).__name__}: {str(e)[:100]}"
    return False, "unknown"


def post_comment(cfg, video_id: str, text: str) -> tuple[str | None, str]:
    """Post a top-level comment. Never raises. Returns (comment_id, reason); id is None on skip/failure."""
    try:
        text = _clean(text).strip()
        if not text:
            return None, "empty comment"
        if not has_scope(cfg, SCOPE_FORCE_SSL):
            log.info("First comment skipped: token lacks the youtube.force-ssl scope (re-run auth_youtube.py)")
            return None, "token lacks youtube.force-ssl scope; re-run auth_youtube.py"
        from googleapiclient.errors import HttpError

        yt = get_service(cfg)
        body = {"snippet": {"videoId": video_id, "topLevelComment": {"snippet": {"textOriginal": text[:10000]}}}}
        for attempt in (0, 1):
            try:
                resp = yt.commentThreads().insert(part="snippet", body=body).execute()
                return resp.get("id"), "ok"
            except HttpError as e:
                status = getattr(e.resp, "status", 0)
                retryable = status >= 500 or "processing" in str(e).lower()
                if attempt == 0 and retryable:
                    log.warning("Comment insert failed (HTTP %s), retrying once", status)
                    time.sleep(5)
                    continue
                return None, f"HTTP {status}"
    except Exception as e:  # noqa: BLE001
        log.warning("Comment failed: %s", type(e).__name__)
        return None, f"{type(e).__name__}: {str(e)[:100]}"
    return None, "unknown"


def _batches(ids, n):
    ids = [i for i in dict.fromkeys(ids) if i]
    for i in range(0, len(ids), n):
        yield ids[i:i + n]


def video_stats(cfg, ids) -> dict[str, dict]:
    """videos.list statistics -> {id: {views, likes, comments}}. Raises on API failure."""
    yt = get_service(cfg)
    out: dict[str, dict] = {}
    for batch in _batches(ids, 50):
        try:  # maxResults is unsupported together with id
            resp = yt.videos().list(part="statistics", id=",".join(batch)).execute()
        except Exception as e:  # noqa: BLE001
            log.warning("videos.list batch failed: %s", type(e).__name__)
            continue
        for it in resp.get("items", []):
            st = it.get("statistics", {})
            out[it["id"]] = {"views": int(st.get("viewCount", 0)), "likes": int(st.get("likeCount", 0)),
                             "comments": int(st.get("commentCount", 0))}
    return out


def analytics_stats(cfg, ids, start: str) -> dict[str, dict]:
    """YouTube Analytics v2 per-video retention -> {id: {views, avg_view_sec, avg_view_pct}}."""
    yta = get_service(cfg, "youtubeAnalytics", "v2")
    out: dict[str, dict] = {}
    end = time.strftime("%Y-%m-%d")
    for batch in _batches(ids, 50):
        resp = yta.reports().query(
            ids="channel==MINE", startDate=start, endDate=end, dimensions="video",
            filters="video==" + ",".join(batch),
            metrics="views,averageViewDuration,averageViewPercentage",
            sort="-views", maxResults=200,  # video dimension requires sort; batches stay <= 200 ids
        ).execute()
        for row in resp.get("rows", []) or []:
            out[str(row[0])] = {"views": int(row[1]), "avg_view_sec": float(row[2]), "avg_view_pct": float(row[3])}
    return out
