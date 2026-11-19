import os
import tempfile
import time
import unittest


class DbTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_app_dir = os.environ.get("APP_DIR")
        os.environ["APP_DIR"] = self._tmp.name
        from cybershorts import config, db

        self.db = db
        self.cfg = config.load()
        self.conn = db.connect(self.cfg)
        db.init(self.conn)

    def tearDown(self):
        self.conn.close()
        if self._old_app_dir is None:
            os.environ.pop("APP_DIR", None)
        else:
            os.environ["APP_DIR"] = self._old_app_dir
        self._tmp.cleanup()

    def _story(self, n, title=None):
        url = f"https://example.com/story-{n}"
        self.db.upsert_story(self.conn, url, title or f"Story {n}", "Test", "2026-11-16T00:00:00Z", "summary")
        return url

    def test_init_is_idempotent(self):
        self.db.init(self.conn)
        self.assertFalse(self.db.story_seen(self.conn, "https://nope.example/"))

    def test_story_seen_and_used(self):
        url = self._story(1)
        self.assertTrue(self.db.story_seen(self.conn, url))
        self.assertFalse(self.db.story_used(self.conn, url))
        self.db.mark_used(self.conn, url)
        self.assertTrue(self.db.story_used(self.conn, url))

    def test_upsert_story_does_not_duplicate(self):
        url = self._story(2, "First title")
        self.db.upsert_story(self.conn, url, "Updated title", "Test", None, "new summary")
        rows = self.conn.execute("SELECT title FROM stories WHERE url=?", (url,)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Updated title")

    def test_recent_used_titles_only_returns_used(self):
        u1 = self._story(1, "Used one")
        self._story(2, "Unused two")
        u3 = self._story(3, "Used three")
        self.db.mark_used(self.conn, u1)
        self.db.mark_used(self.conn, u3)
        titles = self.db.recent_used_titles(self.conn, 10)
        self.assertEqual(sorted(titles), ["Used one", "Used three"])
        self.assertEqual(len(self.db.recent_used_titles(self.conn, 1)), 1)

    def test_draft_roundtrip(self):
        url = self._story(4)
        draft_id = self.db.create_draft(self.conn, url, "awaiting_voice", '{"script": "x"}')
        self.assertIsInstance(draft_id, int)
        row = self.db.get_draft(self.conn, draft_id)
        self.assertEqual(row["story_url"], url)
        self.assertEqual(row["status"], "awaiting_voice")
        self.assertEqual(row["data_json"], '{"script": "x"}')
        self.assertIsNone(row["video_path"])

        self.db.update_draft(self.conn, draft_id, status="rendered", video_path="/tmp/d.mp4",
                             tg_message_id=42)
        row = self.db.get_draft(self.conn, draft_id)
        self.assertEqual(row["status"], "rendered")
        self.assertEqual(row["video_path"], "/tmp/d.mp4")
        self.assertEqual(row["tg_message_id"], 42)

    def test_update_draft_rejects_unknown_field(self):
        url = self._story(5)
        draft_id = self.db.create_draft(self.conn, url, "rendered", "{}")
        with self.assertRaises(ValueError):
            self.db.update_draft(self.conn, draft_id, not_a_column="x")

    def test_get_draft_missing_returns_none(self):
        self.assertIsNone(self.db.get_draft(self.conn, 999999))

    def test_latest_awaiting_voice(self):
        url = self._story(6)
        self.assertIsNone(self.db.latest_awaiting_voice(self.conn))
        self.db.create_draft(self.conn, url, "awaiting_voice", "{}")
        newest = self.db.create_draft(self.conn, url, "awaiting_voice", "{}")
        self.db.create_draft(self.conn, url, "rendered", "{}")
        self.assertEqual(self.db.latest_awaiting_voice(self.conn)["id"], newest)

    def test_drafts_to_cleanup_selects_old_finished_only(self):
        url = self._story(7)
        old_done = self.db.create_draft(self.conn, url, "published", "{}")
        old_partial = self.db.create_draft(self.conn, url, "partial", "{}")
        fresh_done = self.db.create_draft(self.conn, url, "rejected", "{}")
        open_10d = self.db.create_draft(self.conn, url, "rendered", "{}")
        open_30d = self.db.create_draft(self.conn, url, "rendered", "{}")
        stuck_30d = self.db.create_draft(self.conn, url, "publishing", "{}")
        voice_30d = self.db.create_draft(self.conn, url, "awaiting_voice", "{}")

        def ago(d):
            return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - d * 86400))

        for ids, d in (((old_done, old_partial, open_30d, stuck_30d, voice_30d), 30), ((open_10d,), 10)):
            for i in ids:
                self.conn.execute("UPDATE drafts SET updated_at=? WHERE id=?", (ago(d), i))
        self.conn.commit()
        ids = sorted(r["id"] for r in self.db.drafts_to_cleanup(self.conn, 7))
        self.assertEqual(ids, sorted([old_done, old_partial, open_30d, stuck_30d, voice_30d]))
        self.assertNotIn(fresh_done, ids)
        self.assertNotIn(open_10d, ids)  # in-flight drafts only after 2*keep_days


if __name__ == "__main__":
    unittest.main()
