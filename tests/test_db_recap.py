"""Weekly recap DB helpers: candidate selection, recap lookup and stored view counts."""
import os
import tempfile
import unittest


class DbRecapTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_app_dir = os.environ.get("APP_DIR")
        os.environ["APP_DIR"] = self._tmp.name
        from cybershorts import config, db

        self.db = db
        self.cfg = config.load()
        self.conn = db.connect(self.cfg)
        db.init(self.conn)
        self.conn.execute(
            "INSERT INTO stories(url,title,source,published,summary,seen_at,used) VALUES(?,?,?,?,?,?,1)",
            ("https://example.com/a", "Story A", "Test", None, "Summary A", "2026-11-01T00:00:00"),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        if self._old_app_dir is None:
            os.environ.pop("APP_DIR", None)
        else:
            os.environ["APP_DIR"] = self._old_app_dir
        self._tmp.cleanup()

    def _draft(self, *, story_url="https://example.com/a", status="published", youtube_id="yt1",
               updated_at="2026-11-20T10:00:00", kind=None):
        cur = self.conn.execute(
            "INSERT INTO drafts(story_url,status,data_json,youtube_id,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?)",
            (story_url, status, "{}", youtube_id, updated_at, updated_at),
        )
        if kind is not None:
            self.conn.execute("UPDATE drafts SET kind=? WHERE id=?", (kind, cur.lastrowid))
        self.conn.commit()
        return int(cur.lastrowid)

    def test_candidates_filter_and_join(self):
        keep = self._draft(youtube_id="yt-keep", updated_at="2026-11-20T10:00:00")
        partial = self._draft(youtube_id="yt-partial", status="partial", updated_at="2026-11-19T10:00:00")
        self._draft(kind="recap", youtube_id="yt-recap", story_url="recap:2026-W47")
        self._draft(status="rendered", youtube_id=None)
        self._draft(status="awaiting_voice", youtube_id="yt-unpub")
        self._draft(youtube_id=None)
        self._draft(youtube_id="yt-old", updated_at="2026-11-01T10:00:00")

        rows = self.db.recap_candidates(self.conn, "2026-11-13T00:00:00")
        ids = [r["id"] for r in rows]
        self.assertEqual(ids, [keep, partial])  # newest first, recap/unpublished/no-id/old excluded
        self.assertEqual(rows[0]["story_summary"], "Summary A")
        self.assertEqual(rows[0]["story_title"], "Story A")

    def test_candidates_keep_row_without_story(self):
        self._draft(story_url="https://example.com/missing", youtube_id="yt-orphan")
        rows = self.db.recap_candidates(self.conn, "2026-11-13T00:00:00")
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["story_summary"])

    def test_candidates_treat_null_kind_as_regular(self):
        self.conn.execute(
            "INSERT INTO drafts(story_url,status,data_json,youtube_id,created_at,updated_at,kind)"
            " VALUES(?,?,?,?,?,?,NULL)",
            ("https://example.com/a", "published", "{}", "yt-null", "2026-11-20T10:00:00", "2026-11-20T10:00:00"),
        )
        self.conn.commit()
        rows = self.db.recap_candidates(self.conn, "2026-11-13T00:00:00")
        self.assertEqual([r["youtube_id"] for r in rows], ["yt-null"])

    def test_find_recap_returns_newest_for_week(self):
        first = self._draft(kind="recap", story_url="recap:2026-W47", status="failed")
        second = self._draft(kind="recap", story_url="recap:2026-W47", status="rendered")
        self._draft(kind="recap", story_url="recap:2026-W46")
        row = self.db.find_recap(self.conn, "2026-W47")
        self.assertIsNotNone(row)
        self.assertEqual(row["id"], second)
        self.assertNotEqual(row["id"], first)
        self.assertIsNone(self.db.find_recap(self.conn, "2026-W45"))

    def test_views_for_round_trip(self):
        self.db.upsert_video_stats(self.conn, "yt1", draft_id=1, views=120)
        self.db.upsert_video_stats(self.conn, "yt2", draft_id=2, views=0)
        self.assertEqual(self.db.views_for(self.conn, ["yt1", "yt2", "yt-missing"]),
                         {"yt1": 120, "yt2": 0})
        self.db.upsert_video_stats(self.conn, "yt1", draft_id=1, views=300)
        self.assertEqual(self.db.views_for(self.conn, ["yt1"]), {"yt1": 300})
        self.assertEqual(self.db.views_for(self.conn, []), {})


if __name__ == "__main__":
    unittest.main()
