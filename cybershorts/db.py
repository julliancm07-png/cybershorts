"""SQLite schema and helpers."""
from __future__ import annotations

import calendar
import sqlite3
import time
from typing import Any

from .config import Config

SCHEMA = """
CREATE TABLE IF NOT EXISTS stories(
  url TEXT PRIMARY KEY, title TEXT, source TEXT, published TEXT, summary TEXT,
  seen_at TEXT, used INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS drafts(
  id INTEGER PRIMARY KEY, story_url TEXT, status TEXT, data_json TEXT,
  audio_path TEXT, video_path TEXT, tg_message_id INTEGER, youtube_id TEXT,
  tiktok_publish_id TEXT, error TEXT, created_at TEXT, updated_at TEXT
);
"""

DRAFT_FIELDS = {
    "story_url", "status", "data_json", "audio_path", "video_path", "tg_message_id",
    "youtube_id", "tiktok_publish_id", "error",
}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def connect(cfg: Config) -> sqlite3.Connection:
    conn = sqlite3.connect(str(cfg.path("cybershorts.db")), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def upsert_story(conn: sqlite3.Connection, url: str, title: str, source: str,
                 published: str | None, summary: str) -> None:
    conn.execute(
        """INSERT INTO stories(url,title,source,published,summary,seen_at,used)
           VALUES(?,?,?,?,?,?,0)
           ON CONFLICT(url) DO UPDATE SET title=excluded.title, source=excluded.source,
             published=excluded.published, summary=excluded.summary""",
        (url, title, source, published, summary, _now()),
    )
    conn.commit()


def story_seen(conn: sqlite3.Connection, url: str) -> bool:
    return conn.execute("SELECT 1 FROM stories WHERE url=?", (url,)).fetchone() is not None


def story_used(conn: sqlite3.Connection, url: str) -> bool:
    r = conn.execute("SELECT used FROM stories WHERE url=?", (url,)).fetchone()
    return bool(r and r["used"])


def mark_used(conn: sqlite3.Connection, url: str) -> None:
    conn.execute("UPDATE stories SET used=1 WHERE url=?", (url,))
    conn.commit()


def recent_used_titles(conn: sqlite3.Connection, n: int = 10) -> list[str]:
    rows = conn.execute(
        "SELECT title FROM stories WHERE used=1 ORDER BY seen_at DESC, rowid DESC LIMIT ?", (n,)
    ).fetchall()
    return [r["title"] for r in rows]


def create_draft(conn: sqlite3.Connection, story_url: str, status: str, data_json: str,
                 audio_path: str | None = None) -> int:
    now = _now()
    cur = conn.execute(
        "INSERT INTO drafts(story_url,status,data_json,audio_path,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        (story_url, status, data_json, audio_path, now, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def update_draft(conn: sqlite3.Connection, draft_id: int, **fields: Any) -> None:
    if not fields:
        return
    bad = set(fields) - DRAFT_FIELDS
    if bad:
        raise ValueError(f"Unknown draft fields: {sorted(bad)}")
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE drafts SET {sets}, updated_at=? WHERE id=?",
                 (*fields.values(), _now(), draft_id))
    conn.commit()


def get_draft(conn: sqlite3.Connection, draft_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()


def recent_drafts(conn: sqlite3.Connection, n: int = 5) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM drafts ORDER BY id DESC LIMIT ?", (n,)).fetchall()


def drafts_to_cleanup(conn: sqlite3.Connection, days: int) -> list[sqlite3.Row]:
    """Finished drafts (published/rejected/failed/partial) not updated within `days` days,
    plus abandoned in-flight ones (rendered/awaiting_voice/publishing) older than 2*days."""
    def fmt(d):
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - d * 86400))
    return conn.execute(
        "SELECT * FROM drafts WHERE "
        "(status IN ('published','rejected','failed','partial') AND updated_at < ?) OR "
        "(status IN ('rendered','awaiting_voice','publishing') AND updated_at < ?)",
        (fmt(days), fmt(2 * days)),
    ).fetchall()


def seconds_since(ts: str | None) -> float | None:
    """Seconds since a UTC timestamp written by _now(); None if unparsable."""
    try:
        return time.time() - calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return None


def latest_awaiting_voice(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM drafts WHERE status='awaiting_voice' ORDER BY id DESC LIMIT 1"
    ).fetchone()
