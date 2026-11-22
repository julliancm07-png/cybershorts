"""SQLite schema and helpers."""
from __future__ import annotations

import calendar
import sqlite3
import time
from typing import Any, Iterable

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
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS kev_seen(cve_id TEXT PRIMARY KEY, date_added TEXT, seen_at TEXT);
CREATE TABLE IF NOT EXISTS urgency(url TEXT PRIMARY KEY, score INTEGER, scored_at TEXT);
CREATE TABLE IF NOT EXISTS cve_used(cve_id TEXT PRIMARY KEY, draft_id INTEGER, used_at TEXT);
CREATE TABLE IF NOT EXISTS video_stats(
  youtube_id TEXT PRIMARY KEY, draft_id INTEGER, views INTEGER, likes INTEGER, comments INTEGER,
  avg_view_pct REAL, avg_view_sec REAL, fetched_at TEXT
);
"""

# Columns added to `drafts` in v3. migrate() adds whichever are missing, so it is safe to run repeatedly
# and on databases created with the v2 schema (existing rows get the default).
V3_DRAFT_COLUMNS = {
    "kind": "TEXT DEFAULT 'regular'",
    "series": "TEXT",
    "yt_comment_id": "TEXT",
}

DRAFT_FIELDS = {
    "story_url", "status", "data_json", "audio_path", "video_path", "tg_message_id",
    "youtube_id", "tiktok_publish_id", "error", "kind", "series", "yt_comment_id",
}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def connect(cfg: Config) -> sqlite3.Connection:
    conn = sqlite3.connect(str(cfg.path("cybershorts.db")), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Add v3 columns to `drafts` that are missing. Idempotent."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(drafts)").fetchall()}
    for name, ddl in V3_DRAFT_COLUMNS.items():
        if name in have:
            continue
        try:
            conn.execute(f"ALTER TABLE drafts ADD COLUMN {name} {ddl}")
        except sqlite3.OperationalError as e:
            # Another process added it between our PRAGMA and ALTER.
            if "duplicate column name" not in str(e):
                raise
    conn.commit()


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    migrate(conn)
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
                 audio_path: str | None = None, *, kind: str = "regular",
                 series: str | None = None) -> int:
    now = _now()
    cur = conn.execute(
        "INSERT INTO drafts(story_url,status,data_json,audio_path,created_at,updated_at,kind,series)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (story_url, status, data_json, audio_path, now, now, kind, series),
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


def count_drafts_since(conn: sqlite3.Connection, since_iso: str, *,
                       kind: str | None = None, series: str | None = None, include_all: bool = False) -> int:
    """Drafts created at or after since_iso (UTC, _now() format); rejected and failed ones are
    ignored unless include_all."""
    sql = "SELECT COUNT(*) FROM drafts WHERE created_at >= ?"
    if not include_all:
        sql += " AND status NOT IN ('rejected','failed')"
    args: list[Any] = [since_iso]
    if kind is not None:
        sql += " AND kind = ?"
        args.append(kind)
    if series is not None:
        sql += " AND series = ?"
        args.append(series)
    return int(conn.execute(sql, args).fetchone()[0])


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


# --- kv ----------------------------------------------------------------------

def kv_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()


# --- KEV ---------------------------------------------------------------------

def kev_seen_ids(conn: sqlite3.Connection) -> set[str]:
    return {r["cve_id"] for r in conn.execute("SELECT cve_id FROM kev_seen").fetchall()}


def mark_kev_seen(conn: sqlite3.Connection, entries: Iterable[tuple[str, str]]) -> None:
    """entries: (cve_id, date_added). Already-seen CVEs keep their original seen_at."""
    now = _now()
    conn.executemany(
        "INSERT OR IGNORE INTO kev_seen(cve_id,date_added,seen_at) VALUES(?,?,?)",
        [(cve, date, now) for cve, date in entries],
    )
    conn.commit()


# --- RSS urgency -------------------------------------------------------------

def get_urgency(conn: sqlite3.Connection, url: str) -> int | None:
    row = conn.execute("SELECT score FROM urgency WHERE url=?", (url,)).fetchone()
    return int(row["score"]) if row else None


def set_urgency(conn: sqlite3.Connection, url: str, score: int) -> None:
    conn.execute(
        "INSERT INTO urgency(url,score,scored_at) VALUES(?,?,?)"
        " ON CONFLICT(url) DO UPDATE SET score=excluded.score, scored_at=excluded.scored_at",
        (url, int(score), _now()),
    )
    conn.commit()


# --- CVE de-duplication ------------------------------------------------------

def cve_used(conn: sqlite3.Connection, cve_id: str, since: str | None = None) -> bool:
    """True if the CVE was used; with `since` (UTC '%Y-%m-%dT%H:%M:%S', as stored in used_at) only newer rows count."""
    if since:
        return conn.execute("SELECT 1 FROM cve_used WHERE cve_id=? AND used_at>=?", (cve_id, since)).fetchone() is not None
    return conn.execute("SELECT 1 FROM cve_used WHERE cve_id=?", (cve_id,)).fetchone() is not None


def mark_cve_used(conn: sqlite3.Connection, cve_id: str, draft_id: int | None) -> None:
    conn.execute(
        "INSERT INTO cve_used(cve_id,draft_id,used_at) VALUES(?,?,?)"
        " ON CONFLICT(cve_id) DO UPDATE SET draft_id=excluded.draft_id, used_at=excluded.used_at",
        (cve_id, draft_id, _now()),
    )
    conn.commit()


# --- analytics ---------------------------------------------------------------

def published_videos(conn: sqlite3.Connection, since_iso: str) -> list[sqlite3.Row]:
    """Published drafts with a YouTube ID, last updated at or after since_iso."""
    return conn.execute(
        "SELECT * FROM drafts WHERE status='published' AND youtube_id IS NOT NULL"
        " AND updated_at >= ? ORDER BY id",
        (since_iso,),
    ).fetchall()


def upsert_video_stats(conn: sqlite3.Connection, youtube_id: str, *, draft_id: int | None = None,
                       views: int = 0, likes: int = 0, comments: int = 0,
                       avg_view_pct: float | None = None, avg_view_sec: float | None = None) -> None:
    """Store one video's stats. A None draft_id or average keeps the value already stored,
    so a fetch without the analytics scope does not erase earlier averages."""
    conn.execute(
        """INSERT INTO video_stats(youtube_id,draft_id,views,likes,comments,avg_view_pct,avg_view_sec,fetched_at)
           VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(youtube_id) DO UPDATE SET
             draft_id=COALESCE(excluded.draft_id, video_stats.draft_id),
             views=excluded.views, likes=excluded.likes, comments=excluded.comments,
             avg_view_pct=COALESCE(excluded.avg_view_pct, video_stats.avg_view_pct),
             avg_view_sec=COALESCE(excluded.avg_view_sec, video_stats.avg_view_sec),
             fetched_at=excluded.fetched_at""",
        (youtube_id, draft_id, int(views), int(likes), int(comments), avg_view_pct, avg_view_sec, _now()),
    )
    conn.commit()


def all_video_stats(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM video_stats ORDER BY youtube_id").fetchall()


# --- weekly recap ------------------------------------------------------------

def recap_candidates(conn: sqlite3.Connection, since_iso: str) -> list[sqlite3.Row]:
    """Published Shorts (not recaps) with a YouTube ID, last updated at or after since_iso, newest first.
    Each row also carries the source story's summary and title (None when the story row is missing)."""
    return conn.execute(
        "SELECT d.*, s.summary AS story_summary, s.title AS story_title"
        " FROM drafts d LEFT JOIN stories s ON s.url = d.story_url"
        " WHERE COALESCE(d.kind,'regular') != 'recap' AND d.youtube_id IS NOT NULL"
        " AND d.status IN ('published','partial') AND d.updated_at >= ?"
        " ORDER BY d.updated_at DESC",
        (since_iso,),
    ).fetchall()


def find_recap(conn: sqlite3.Connection, week: str) -> sqlite3.Row | None:
    """Newest recap draft for an ISO week key such as '2026-W47' (story_url 'recap:<week>')."""
    return conn.execute(
        "SELECT * FROM drafts WHERE kind='recap' AND story_url='recap:' || ?"
        " ORDER BY id DESC LIMIT 1",
        (week,),
    ).fetchone()


def count_published_recaps(conn: sqlite3.Connection) -> int:
    """Recaps that reached YouTube or TikTok (status published or partial)."""
    row = conn.execute(
        "SELECT COUNT(*) FROM drafts WHERE kind='recap' AND status IN ('published','partial')").fetchone()
    return int(row[0] or 0)


def views_for(conn: sqlite3.Connection, ids: Iterable[str]) -> dict[str, int]:
    """Stored view counts keyed by YouTube ID. IDs with no video_stats row are omitted."""
    ids = list({str(i) for i in ids if i})
    out: dict[str, int] = {}
    for start in range(0, len(ids), 500):  # stay well under SQLite's bound-parameter limit
        chunk = ids[start:start + 500]
        marks = ",".join("?" * len(chunk))
        for r in conn.execute(
            f"SELECT youtube_id, views FROM video_stats WHERE youtube_id IN ({marks})", chunk
        ).fetchall():
            out[r["youtube_id"]] = int(r["views"] or 0)
    return out
