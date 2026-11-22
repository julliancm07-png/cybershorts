"""v3 database changes: idempotent migration on v2 databases, new columns and helper round-trips."""
import os
import sqlite3
import tempfile
import unittest

# The v2 schema exactly as shipped before v3. Used to build a database that already holds rows.
V2_SCHEMA = """
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

V3_TABLES = {"kv", "kev_seen", "urgency", "cve_used", "video_stats"}


class DbV3Tests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_app_dir = os.environ.get("APP_DIR")
        os.environ["APP_DIR"] = self._tmp.name
        from cybershorts import config, db

        self.db = db
        self.cfg = config.load()
        self.db_path = str(self.cfg.path("cybershorts.db"))

    def tearDown(self):
        if self._old_app_dir is None:
            os.environ.pop("APP_DIR", None)
        else:
            os.environ["APP_DIR"] = self._old_app_dir
        self._tmp.cleanup()

    def _make_v2_db_with_rows(self):
        raw = sqlite3.connect(self.db_path)
        raw.executescript(V2_SCHEMA)
        raw.execute(
            "INSERT INTO stories(url,title,source,published,summary,seen_at,used) VALUES(?,?,?,?,?,?,1)",
            ("https://example.com/old", "Old story", "Test", None, "s", "2026-10-01T00:00:00"),
        )
        raw.execute(
            "INSERT INTO drafts(story_url,status,data_json,audio_path,video_path,tg_message_id,youtube_id,"
            "tiktok_publish_id,error,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("https://example.com/old", "published", '{"script": "kept"}', "/a.wav", "/v.mp4", 7,
             "yt-old", None, None, "2026-10-01T10:00:00", "2026-10-02T10:00:00"),
        )
        raw.execute(
            "INSERT INTO drafts(story_url,status,data_json,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("https://example.com/old", "rejected", "{}", "2026-10-03T10:00:00", "2026-10-03T11:00:00"),
        )
        raw.commit()
        raw.close()

    def _columns(self, conn, table):
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    def _tables(self, conn):
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    def test_migrates_v2_database_with_rows_without_data_loss(self):
        self._make_v2_db_with_rows()
        conn = self.db.connect(self.cfg)
        self.db.init(conn)

        cols = self._columns(conn, "drafts")
        self.assertTrue({"kind", "series", "yt_comment_id"} <= cols)
        self.assertTrue(V3_TABLES <= self._tables(conn))

        rows = conn.execute("SELECT * FROM drafts ORDER BY id").fetchall()
        self.assertEqual(len(rows), 2)
        published = rows[0]
        self.assertEqual(published["status"], "published")
        self.assertEqual(published["data_json"], '{"script": "kept"}')
        self.assertEqual(published["youtube_id"], "yt-old")
        self.assertEqual(published["tg_message_id"], 7)
        self.assertEqual(published["video_path"], "/v.mp4")
        self.assertEqual(published["kind"], "regular")
        self.assertIsNone(published["series"])
        self.assertIsNone(published["yt_comment_id"])
        self.assertEqual(rows[1]["kind"], "regular")

        story = conn.execute("SELECT title, used FROM stories").fetchone()
        self.assertEqual((story["title"], story["used"]), ("Old story", 1))
        conn.close()

    def test_init_and_migrate_are_idempotent_on_v2_database(self):
        self._make_v2_db_with_rows()
        conn = self.db.connect(self.cfg)
        self.db.init(conn)
        self.db.init(conn)
        self.db.migrate(conn)
        self.db.migrate(conn)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0], 2)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0], 1)
        conn.close()

    def test_migrate_on_fresh_database_creates_columns_once(self):
        conn = self.db.connect(self.cfg)
        self.db.init(conn)
        self.db.migrate(conn)
        cols = self._columns(conn, "drafts")
        self.assertTrue({"kind", "series", "yt_comment_id"} <= cols)
        self.assertEqual(len(self._tables(conn) & V3_TABLES), len(V3_TABLES))
        conn.close()

    def test_migrate_tolerates_column_added_by_another_process(self):
        self._make_v2_db_with_rows()
        conn = self.db.connect(self.cfg)

        class Racing:
            """Simulates a second process adding `kind` between PRAGMA and ALTER."""
            def __init__(self, inner):
                self.inner = inner
                self.raced = False

            def execute(self, sql, *args):
                if sql.startswith("ALTER TABLE drafts ADD COLUMN kind") and not self.raced:
                    self.raced = True
                    self.inner.execute("ALTER TABLE drafts ADD COLUMN kind TEXT DEFAULT 'regular'")
                return self.inner.execute(sql, *args)

            def commit(self):
                return self.inner.commit()

        racing = Racing(conn)
        self.db.migrate(racing)
        self.assertTrue(racing.raced)
        self.assertTrue({"kind", "series", "yt_comment_id"} <= self._columns(conn, "drafts"))
        conn.close()

    def test_create_draft_defaults_and_explicit_kind_series(self):
        conn = self.db.connect(self.cfg)
        self.db.init(conn)
        plain = self.db.create_draft(conn, "https://example.com/a", "awaiting_voice", "{}")
        special = self.db.create_draft(conn, "https://example.com/b", "awaiting_voice", "{}",
                                       kind="breaking", series="CVE in 30s")
        row = self.db.get_draft(conn, plain)
        self.assertEqual(row["kind"], "regular")
        self.assertIsNone(row["series"])
        row = self.db.get_draft(conn, special)
        self.assertEqual((row["kind"], row["series"]), ("breaking", "CVE in 30s"))
        conn.close()

    def test_update_draft_accepts_v3_fields(self):
        conn = self.db.connect(self.cfg)
        self.db.init(conn)
        draft_id = self.db.create_draft(conn, "https://example.com/c", "published", "{}")
        self.db.update_draft(conn, draft_id, kind="breaking", series="Scam of the Week",
                             yt_comment_id="cmt-1")
        row = self.db.get_draft(conn, draft_id)
        self.assertEqual((row["kind"], row["series"], row["yt_comment_id"]),
                         ("breaking", "Scam of the Week", "cmt-1"))
        with self.assertRaises(ValueError):
            self.db.update_draft(conn, draft_id, not_a_column="x")
        conn.close()

    def test_count_drafts_since_filters_and_excludes_rejected_failed(self):
        conn = self.db.connect(self.cfg)
        self.db.init(conn)
        self.db.create_draft(conn, "u1", "published", "{}", series="Scam of the Week")
        self.db.create_draft(conn, "u2", "rendered", "{}", series="Scam of the Week")
        self.db.create_draft(conn, "u3", "rejected", "{}", series="Scam of the Week")
        self.db.create_draft(conn, "u4", "failed", "{}", series="Scam of the Week")
        self.db.create_draft(conn, "u5", "awaiting_voice", "{}", kind="breaking")
        self.db.create_draft(conn, "u6", "awaiting_voice", "{}")

        past = "2000-01-01T00:00:00"
        future = "2999-01-01T00:00:00"
        self.assertEqual(self.db.count_drafts_since(conn, past), 4)
        self.assertEqual(self.db.count_drafts_since(conn, future), 0)
        self.assertEqual(self.db.count_drafts_since(conn, past, series="Scam of the Week"), 2)
        self.assertEqual(self.db.count_drafts_since(conn, past, kind="breaking"), 1)
        self.assertEqual(self.db.count_drafts_since(conn, past, kind="regular"), 3)
        self.assertEqual(self.db.count_drafts_since(conn, past, include_all=True), 6)
        self.assertEqual(self.db.count_drafts_since(conn, past, series="Scam of the Week", include_all=True), 4)
        conn.close()

    def test_count_drafts_since_counts_migrated_rows_as_regular(self):
        self._make_v2_db_with_rows()
        conn = self.db.connect(self.cfg)
        self.db.init(conn)
        # The 'published' v2 row (created 2026-10-01) counts; the 'rejected' one does not.
        self.assertEqual(self.db.count_drafts_since(conn, "2026-09-01T00:00:00", kind="regular"), 1)
        conn.close()

    def test_kv_round_trip(self):
        conn = self.db.connect(self.cfg)
        self.db.init(conn)
        self.assertIsNone(self.db.kv_get(conn, "kev_seeded"))
        self.assertEqual(self.db.kv_get(conn, "kev_seeded", "no"), "no")
        self.db.kv_set(conn, "kev_seeded", "1")
        self.assertEqual(self.db.kv_get(conn, "kev_seeded"), "1")
        self.db.kv_set(conn, "kev_seeded", "2")
        self.assertEqual(self.db.kv_get(conn, "kev_seeded"), "2")
        conn.close()

    def test_kev_seen_round_trip_and_duplicates(self):
        conn = self.db.connect(self.cfg)
        self.db.init(conn)
        self.assertEqual(self.db.kev_seen_ids(conn), set())
        self.db.mark_kev_seen(conn, [("CVE-2026-0001", "2026-11-20"), ("CVE-2026-0002", "2026-11-20")])
        self.db.mark_kev_seen(conn, [("CVE-2026-0002", "2026-11-20"), ("CVE-2026-0003", "2026-11-21")])
        self.assertEqual(self.db.kev_seen_ids(conn),
                         {"CVE-2026-0001", "CVE-2026-0002", "CVE-2026-0003"})
        conn.close()

    def test_urgency_round_trip(self):
        conn = self.db.connect(self.cfg)
        self.db.init(conn)
        self.assertIsNone(self.db.get_urgency(conn, "https://example.com/x"))
        self.db.set_urgency(conn, "https://example.com/x", 6)
        self.assertEqual(self.db.get_urgency(conn, "https://example.com/x"), 6)
        self.db.set_urgency(conn, "https://example.com/x", 9)
        self.assertEqual(self.db.get_urgency(conn, "https://example.com/x"), 9)
        conn.close()

    def test_cve_used_round_trip(self):
        conn = self.db.connect(self.cfg)
        self.db.init(conn)
        self.assertFalse(self.db.cve_used(conn, "CVE-2026-0001"))
        self.db.mark_cve_used(conn, "CVE-2026-0001", 5)
        self.assertTrue(self.db.cve_used(conn, "CVE-2026-0001"))
        self.db.mark_cve_used(conn, "CVE-2026-0001", 6)
        self.assertEqual(conn.execute("SELECT draft_id FROM cve_used").fetchone()["draft_id"], 6)
        conn.close()

    def test_published_videos_filters_status_id_and_age(self):
        conn = self.db.connect(self.cfg)
        self.db.init(conn)
        old = self.db.create_draft(conn, "u1", "published", "{}")
        new = self.db.create_draft(conn, "u2", "published", "{}")
        self.db.create_draft(conn, "u3", "published", "{}")  # no youtube_id: excluded
        self.db.create_draft(conn, "u4", "rendered", "{}")
        self.db.update_draft(conn, old, youtube_id="yt-1")
        self.db.update_draft(conn, new, youtube_id="yt-2")
        conn.execute("UPDATE drafts SET updated_at='2026-10-01T00:00:00' WHERE id=?", (old,))
        conn.execute("UPDATE drafts SET updated_at='2026-11-20T00:00:00' WHERE id=?", (new,))
        conn.commit()
        ids = [r["youtube_id"] for r in self.db.published_videos(conn, "2026-11-01T00:00:00")]
        self.assertEqual(ids, ["yt-2"])
        all_ids = [r["youtube_id"] for r in self.db.published_videos(conn, "2000-01-01T00:00:00")]
        self.assertEqual(all_ids, ["yt-1", "yt-2"])
        conn.close()

    def test_video_stats_upsert_keeps_averages_when_missing(self):
        conn = self.db.connect(self.cfg)
        self.db.init(conn)
        self.db.upsert_video_stats(conn, "yt-1", draft_id=3, views=100, likes=5, comments=1,
                                   avg_view_pct=42.5, avg_view_sec=21.0)
        # Second fetch without analytics scope: only public counts.
        self.db.upsert_video_stats(conn, "yt-1", views=150, likes=7, comments=2)
        rows = self.db.all_video_stats(conn)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["views"], row["likes"], row["comments"]), (150, 7, 2))
        self.assertEqual(row["draft_id"], 3)
        self.assertEqual(row["avg_view_pct"], 42.5)
        self.assertEqual(row["avg_view_sec"], 21.0)
        self.assertTrue(row["fetched_at"])
        conn.close()

    def test_video_stats_upsert_overwrites_when_values_given(self):
        conn = self.db.connect(self.cfg)
        self.db.init(conn)
        self.db.upsert_video_stats(conn, "yt-2", views=1, avg_view_pct=10.0)
        self.db.upsert_video_stats(conn, "yt-2", views=2, avg_view_pct=55.0, avg_view_sec=30.0)
        row = self.db.all_video_stats(conn)[0]
        self.assertEqual(row["views"], 2)
        self.assertEqual(row["avg_view_pct"], 55.0)
        self.assertEqual(row["avg_view_sec"], 30.0)
        self.assertEqual(len(self.db.all_video_stats(conn)), 1)
        conn.close()

    def test_video_stats_migrated_database_accepts_upsert(self):
        self._make_v2_db_with_rows()
        conn = self.db.connect(self.cfg)
        self.db.init(conn)
        self.db.upsert_video_stats(conn, "yt-old", draft_id=1, views=9, likes=0, comments=0)
        self.assertEqual(self.db.all_video_stats(conn)[0]["views"], 9)
        conn.close()

    def test_cve_used_since(self):
        conn = self.db.connect(self.cfg)
        self.db.init(conn)
        self.db.mark_cve_used(conn, "CVE-2026-1", None)
        conn.execute("UPDATE cve_used SET used_at='2020-01-01T00:00:00'")
        conn.commit()
        self.assertTrue(self.db.cve_used(conn, "CVE-2026-1"))
        self.assertFalse(self.db.cve_used(conn, "CVE-2026-1", since="2026-01-01T00:00:00"))
        self.db.mark_cve_used(conn, "CVE-2026-1", None)
        self.assertTrue(self.db.cve_used(conn, "CVE-2026-1", since="2026-01-01T00:00:00"))
        conn.close()


if __name__ == "__main__":
    unittest.main()
