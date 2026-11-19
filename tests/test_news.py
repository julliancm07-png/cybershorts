import os
import tempfile
import time
import unittest
from email.utils import formatdate
from unittest import mock


class FakeResponse:
    def __init__(self, body):
        self.content = body.encode("utf-8")
        self.text = body
        self.status_code = 200

    def raise_for_status(self):
        return None


def _rss(items):
    parts = []
    for title, link, epoch in items:
        parts.append(
            f"<item><title>{title}</title><link>{link}</link>"
            f"<description>&lt;p&gt;Summary for {title}.&lt;/p&gt;</description>"
            f"<pubDate>{formatdate(epoch, usegmt=True)}</pubDate></item>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>Test</title>'
        "<link>https://example.com/</link><description>Test feed</description>"
        + "".join(parts)
        + "</channel></rss>"
    )


class NewsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_app_dir = os.environ.get("APP_DIR")
        os.environ["APP_DIR"] = self._tmp.name
        from cybershorts import config, db, news

        self.news = news
        self.db = db
        self.cfg = config.load()
        self.cfg.raw["feeds"] = [{"name": "Test", "url": "https://feeds.example.invalid/rss"}]
        self.conn = db.connect(self.cfg)
        db.init(self.conn)

    def tearDown(self):
        self.conn.close()
        if self._old_app_dir is None:
            os.environ.pop("APP_DIR", None)
        else:
            os.environ["APP_DIR"] = self._old_app_dir
        self._tmp.cleanup()

    def test_only_recent_items_are_returned(self):
        now = time.time()
        body = _rss([
            ("Fresh breach story", "https://example.com/fresh", now - 3600),
            ("Old breach story", "https://example.com/old", now - 10 * 86400),
        ])
        with mock.patch("cybershorts.news.requests.get", return_value=FakeResponse(body)) as get:
            stories = self.news.fetch_candidates(self.cfg, self.conn)
        self.assertEqual(get.call_count, 1)
        self.assertEqual([s["url"] for s in stories], ["https://example.com/fresh"])
        self.assertEqual(stories[0]["title"], "Fresh breach story")
        self.assertEqual(stories[0]["source"], "Test")
        self.assertIn("Summary for Fresh breach story", stories[0]["summary"])
        self.assertNotIn("<p>", stories[0]["summary"])
        self.assertTrue(self.db.story_seen(self.conn, "https://example.com/fresh"))

    def test_used_stories_are_skipped(self):
        now = time.time()
        body = _rss([("Already used", "https://example.com/used", now - 600)])
        self.db.upsert_story(self.conn, "https://example.com/used", "Already used", "Test", None, "")
        self.db.mark_used(self.conn, "https://example.com/used")
        with mock.patch("cybershorts.news.requests.get", return_value=FakeResponse(body)):
            stories = self.news.fetch_candidates(self.cfg, self.conn)
        self.assertEqual(stories, [])

    def test_one_failing_feed_is_skipped_not_raised(self):
        self.cfg.raw["feeds"].append({"name": "Second", "url": "https://feeds2.example.invalid/rss"})
        body = _rss([("Fresh", "https://example.com/f", time.time() - 600)])
        with mock.patch("cybershorts.news.requests.get",
                        side_effect=[OSError("network down"), FakeResponse(body)]):
            stories = self.news.fetch_candidates(self.cfg, self.conn)
        self.assertEqual(len(stories), 1)

    def test_all_feeds_failing_raises(self):
        with mock.patch("cybershorts.news.requests.get", side_effect=OSError("network down")):
            with self.assertRaisesRegex(RuntimeError, "All feeds failed"):
                self.news.fetch_candidates(self.cfg, self.conn)


if __name__ == "__main__":
    unittest.main()
