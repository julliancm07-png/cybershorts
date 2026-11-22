import json
import os
import tempfile
import unittest
from unittest import mock

from cybershorts import youtube


class Cfg:
    def __init__(self, d, **vals):
        self.d = d
        self.vals = vals

    def path(self, *parts):
        return os.path.join(self.d, *parts)

    def get(self, k, default=None):
        return self.vals.get(k, default)


def write_token(d, scopes):
    p = os.path.join(d, "youtube_token.json")
    with open(p, "w") as f:
        json.dump({"token": "t", "refresh_token": "r", "client_id": "c", "client_secret": "s",
                   "token_uri": "https://oauth2.googleapis.com/token", "scopes": scopes}, f)
    return p


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = Cfg(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_all_scopes_constant(self):
        self.assertEqual(len(youtube.SCOPES), 3)
        self.assertIn(youtube.SCOPE_FORCE_SSL, youtube.SCOPES)

    def test_granted_and_has_scope(self):
        write_token(self._tmp.name, [youtube.SCOPE_UPLOAD])
        self.assertEqual(youtube.granted_scopes(self.cfg), {youtube.SCOPE_UPLOAD})
        self.assertTrue(youtube.has_scope(self.cfg, youtube.SCOPE_UPLOAD))
        self.assertFalse(youtube.has_scope(self.cfg, youtube.SCOPE_FORCE_SSL))

    def test_broad_scope_satisfies_force_ssl(self):
        write_token(self._tmp.name, [youtube.SCOPE_BROAD])
        self.assertTrue(youtube.has_scope(self.cfg, youtube.SCOPE_FORCE_SSL))
        self.assertFalse(youtube.has_scope(self.cfg, youtube.SCOPE_ANALYTICS))

    def test_missing_or_bad_token(self):
        self.assertEqual(youtube.granted_scopes(self.cfg), set())
        with open(os.path.join(self._tmp.name, "youtube_token.json"), "w") as f:
            f.write("{nope")
        self.assertEqual(youtube.granted_scopes(self.cfg), set())

    def test_upload_only_token_loads_without_scopes_argument(self):
        path = write_token(self._tmp.name, [youtube.SCOPE_UPLOAD])
        from google.oauth2.credentials import Credentials

        real = Credentials.from_authorized_user_file
        seen = {}

        def spy(*args, **kwargs):
            seen["args"], seen["kwargs"] = args, kwargs
            creds = real(*args, **kwargs)
            creds.refresh = lambda req: setattr(creds, "token", "new") or setattr(creds, "expiry", None)
            return creds

        with mock.patch.object(Credentials, "from_authorized_user_file", side_effect=spy), \
             mock.patch("googleapiclient.discovery.build", return_value="SERVICE") as build:
            self.assertEqual(youtube.get_service(self.cfg), "SERVICE")
        self.assertEqual(seen["args"], (path,))
        self.assertEqual(seen["kwargs"], {})
        build.assert_called_once()
        self.assertEqual(build.call_args.args[:2], ("youtube", "v3"))

    def test_refresh_keeps_granted_scopes_in_file(self):
        path = write_token(self._tmp.name, [youtube.SCOPE_UPLOAD])
        from google.oauth2.credentials import Credentials

        with mock.patch.object(Credentials, "refresh", lambda self_, req: setattr(self_, "token", "new")), \
             mock.patch("googleapiclient.discovery.build", return_value="S"):
            youtube.get_service(self.cfg)
        self.assertEqual(json.loads(open(path).read())["scopes"], [youtube.SCOPE_UPLOAD])

    def test_analytics_service_uses_discovery_name(self):
        write_token(self._tmp.name, [youtube.SCOPE_ANALYTICS])
        from google.oauth2.credentials import Credentials

        with mock.patch.object(Credentials, "refresh", lambda self_, req: None), \
             mock.patch("googleapiclient.discovery.build", return_value="S") as build:
            youtube.get_service(self.cfg, "youtubeAnalytics", "v2")
        self.assertEqual(build.call_args.args[:2], ("youtubeAnalytics", "v2"))


class CommentTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = Cfg(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_scope_skips_without_service_call(self):
        write_token(self._tmp.name, [youtube.SCOPE_UPLOAD])
        with mock.patch.object(youtube, "get_service") as gs, self.assertLogs("cybershorts", "INFO") as lg:
            cid, reason = youtube.post_comment(self.cfg, "vid", "Hello?")
        gs.assert_not_called()
        self.assertIsNone(cid)
        self.assertIn("force-ssl", reason)
        self.assertEqual(len(lg.records), 1)

    def test_success(self):
        write_token(self._tmp.name, [youtube.SCOPE_FORCE_SSL])
        yt = mock.Mock()
        yt.commentThreads.return_value.insert.return_value.execute.return_value = {"id": "c1"}
        with mock.patch.object(youtube, "get_service", return_value=yt):
            self.assertEqual(youtube.post_comment(self.cfg, "vid", "Hi?")[0], "c1")

    def test_retries_once_on_5xx_then_gives_up_without_raising(self):
        from googleapiclient.errors import HttpError

        write_token(self._tmp.name, [youtube.SCOPE_FORCE_SSL])
        err = HttpError(mock.Mock(status=503), b"boom")
        yt = mock.Mock()
        ex = yt.commentThreads.return_value.insert.return_value.execute
        ex.side_effect = err
        with mock.patch.object(youtube, "get_service", return_value=yt), \
             mock.patch.object(youtube.time, "sleep") as sl:
            cid, reason = youtube.post_comment(self.cfg, "vid", "Hi?")
        self.assertIsNone(cid)
        self.assertEqual(ex.call_count, 2)
        sl.assert_called_once_with(5)
        self.assertIn("503", reason)

    def test_4xx_not_retried(self):
        from googleapiclient.errors import HttpError

        write_token(self._tmp.name, [youtube.SCOPE_FORCE_SSL])
        yt = mock.Mock()
        ex = yt.commentThreads.return_value.insert.return_value.execute
        ex.side_effect = HttpError(mock.Mock(status=403), b"forbidden")
        with mock.patch.object(youtube, "get_service", return_value=yt):
            self.assertIsNone(youtube.post_comment(self.cfg, "vid", "Hi?")[0])
        self.assertEqual(ex.call_count, 1)

    def test_never_raises(self):
        write_token(self._tmp.name, [youtube.SCOPE_FORCE_SSL])
        with mock.patch.object(youtube, "get_service", side_effect=RuntimeError("token dead")):
            cid, reason = youtube.post_comment(self.cfg, "vid", "Hi?")
        self.assertIsNone(cid)
        self.assertIn("token dead", reason)


class StatsTests(unittest.TestCase):
    def test_video_stats_batches_of_50(self):
        yt = mock.Mock()
        yt.videos.return_value.list.return_value.execute.return_value = {
            "items": [{"id": "a", "statistics": {"viewCount": "5", "likeCount": "2"}}]}
        with mock.patch.object(youtube, "get_service", return_value=yt):
            out = youtube.video_stats(mock.Mock(), ["v%d" % i for i in range(120)] + ["a"])
        self.assertEqual(yt.videos.return_value.list.call_count, 3)
        self.assertEqual(out["a"], {"views": 5, "likes": 2, "comments": 0})

    def test_analytics_query_shape(self):
        yta = mock.Mock()
        q = yta.reports.return_value.query
        q.return_value.execute.return_value = {"rows": [["a", 10, 22.5, 61.0]]}
        with mock.patch.object(youtube, "get_service", return_value=yta):
            out = youtube.analytics_stats(mock.Mock(), ["a", "b"], "2026-01-01")
        kw = q.call_args.kwargs
        self.assertEqual(kw["ids"], "channel==MINE")
        self.assertEqual(kw["filters"], "video==a,b")
        self.assertEqual(kw["dimensions"], "video")
        self.assertEqual(kw["sort"], "-views")
        self.assertEqual(kw["maxResults"], 200)
        self.assertEqual(out["a"], {"views": 10, "avg_view_sec": 22.5, "avg_view_pct": 61.0})


if __name__ == "__main__":
    unittest.main()
