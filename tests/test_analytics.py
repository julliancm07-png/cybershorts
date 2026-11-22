import json
import os
import tempfile
import time
import unittest
from unittest import mock

from cybershorts import analytics, youtube


class Cfg:
    telegram_bot_token = "tok"
    telegram_chat_id = "1"

    def __init__(self, d, **vals):
        self.d = d
        self.vals = vals

    def path(self, *parts):
        return os.path.join(self.d, *parts)

    def get(self, k, default=None):
        return self.vals.get(k, default)


def video(i, title="T", script="Hook line.\n\nBody text here."):
    return {"id": i, "youtube_id": "yt%d" % i, "kind": "regular", "series": "",
            "updated_at": "2020-01-01T00:00:00",
            "data_json": json.dumps({"youtube_title": title, "script": script})}


class BulletTests(unittest.TestCase):
    def test_parse_bullets_limits(self):
        text = "intro\n" + "\n".join("- point %d http://x.com %s" % (i, "a" * 200) for i in range(9))
        out = analytics.parse_bullets(text)
        self.assertEqual(len(out), 6)
        self.assertTrue(all(len(b) <= 120 and "http" not in b for b in out))

    def test_cap_text_cuts_on_line(self):
        out = analytics.cap_text("a\nbbbb\ncccc", 8)
        self.assertEqual(out, "a\nbbbb")


class RunTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = Cfg(self._tmp.name, **{"analytics.learnings_max_chars": 200})
        self.conn = mock.Mock()
        self.patches = [
            mock.patch.object(analytics.db, "connect", return_value=self.conn),
            mock.patch.object(analytics.db, "init"),
            mock.patch.object(analytics.db, "upsert_video_stats", create=True),
            mock.patch.object(analytics.db, "published_videos", create=True, return_value=[]),
            mock.patch.object(analytics, "_send"),
        ]
        self.m = [p.start() for p in self.patches]
        self.upsert, self.pub, self.send = self.m[2], self.m[3], self.m[4]

    def tearDown(self):
        mock.patch.stopall()
        self._tmp.cleanup()

    def _scopes(self, *s):
        return mock.patch.object(youtube, "granted_scopes", return_value=set(s))

    def test_no_force_ssl_skips_with_hint_and_one_log_line(self):
        with self._scopes(youtube.SCOPE_UPLOAD), mock.patch.object(youtube, "video_stats") as vs, \
             self.assertLogs("cybershorts", "INFO") as lg:
            self.assertIsNone(analytics.run(self.cfg))
        vs.assert_not_called()
        self.assertEqual(len(lg.records), 1)
        self.send.assert_called_once()

    def test_video_stats_no_max_results_and_batch_failure_continues(self):
        calls = []

        def lst(**kw):
            calls.append(kw)
            m = mock.Mock()
            if len(calls) == 1:
                m.execute.side_effect = RuntimeError("boom")
            else:
                m.execute.return_value = {"items": [{"id": "b", "statistics": {"viewCount": "3"}}]}
            return m

        yt = mock.Mock()
        yt.videos.return_value.list.side_effect = lst
        ids = ["a%d" % i for i in range(50)] + ["b"]
        with mock.patch.object(youtube, "get_service", return_value=yt):
            out = youtube.video_stats(self.cfg, ids)
        self.assertEqual(out, {"b": {"views": 3, "likes": 0, "comments": 0}})
        self.assertEqual(len(calls), 2)
        for kw in calls:
            self.assertNotIn("maxResults", kw)

    def test_views_only_without_analytics_scope(self):
        self.pub.return_value = [video(1)]
        with self._scopes(youtube.SCOPE_FORCE_SSL), \
             mock.patch.object(youtube, "video_stats", return_value={"yt1": {"views": 9, "likes": 1, "comments": 0}}), \
             mock.patch.object(youtube, "analytics_stats") as ast:
            res = analytics.run(self.cfg)
        ast.assert_not_called()
        self.assertEqual(res["videos"], 1)
        kw = self.upsert.call_args.kwargs
        self.assertEqual((kw["views"], kw["likes"], kw["comments"], kw["avg_view_pct"]), (9, 1, 0, None))
        self.assertFalse(os.path.exists(analytics.learnings_path(self.cfg)))  # < min_videos

    def test_learnings_written_and_capped(self):
        self.pub.return_value = [video(i) for i in range(8)]
        stats = {"yt%d" % i: {"views": i * 10, "likes": 0, "comments": 0} for i in range(8)}
        bullets = "\n".join("- " + ("word " * 20) for _ in range(6))
        with self._scopes(youtube.SCOPE_FORCE_SSL, youtube.SCOPE_ANALYTICS), \
             mock.patch.object(youtube, "video_stats", return_value=stats), \
             mock.patch.object(youtube, "analytics_stats", return_value={}), \
             mock.patch.object(analytics.writer, "chat", return_value=bullets) as chat:
            res = analytics.run(self.cfg)
        self.assertTrue(res["learnings"])
        chat.assert_called_once()
        text = analytics.learnings_path(self.cfg).read_text()
        self.assertLessEqual(len(text), 201)
        self.assertLessEqual(len(analytics.read_learnings(self.cfg)), 200)

    def test_young_videos_excluded(self):
        v = video(1)
        v["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        self.pub.return_value = [v]
        with self._scopes(youtube.SCOPE_FORCE_SSL), mock.patch.object(youtube, "video_stats") as vs:
            res = analytics.run(self.cfg)
        vs.assert_not_called()
        self.assertEqual(res["videos"], 0)

    def test_no_telegram_flag(self):
        self.pub.return_value = [video(1)]
        with self._scopes(youtube.SCOPE_FORCE_SSL), \
             mock.patch.object(youtube, "video_stats", return_value={"yt1": {"views": 1, "likes": 0, "comments": 0}}):
            analytics.run(self.cfg, telegram=False)
        self.send.assert_not_called()

    def test_run_never_raises(self):
        self.pub.return_value = [video(1)]
        with self._scopes(youtube.SCOPE_FORCE_SSL), \
             mock.patch.object(youtube, "video_stats", side_effect=RuntimeError("api down")):
            self.assertIsNone(analytics.run(self.cfg))

    def test_report_is_html_escaped(self):
        vids = [{"id": "a", "title": "<script>alert(1)</script> & co", "views": 5, "likes": 1, "comments": 0, "pct": None}]
        out = analytics.build_report(self.cfg, vids, "learn <b>bold</b> & more")
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)
        self.assertIn("&lt;b&gt;bold&lt;/b&gt;", out)
        self.assertIn("<b>Weekly analytics</b>", out)


if __name__ == "__main__":
    unittest.main()
