import os
import tempfile
import unittest
from unittest import mock


class VisualsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("APP_DIR")
        os.environ["APP_DIR"] = self._tmp.name
        from cybershorts import config, visuals

        self.v = visuals
        self.cfg = config.load()
        self.cfg.pixabay_api_key = "KEY"
        self.out = self.cfg.path("work") / "draft_1_broll.mp4"

    def tearDown(self):
        if self._old is None:
            os.environ.pop("APP_DIR", None)
        else:
            os.environ["APP_DIR"] = self._old
        self._tmp.cleanup()

    def test_pixabay_pick_prefers_tall_rendition(self):
        hits = [{"videos": {
            "large": {"url": "L", "width": 3840, "height": 2160},
            "medium": {"url": "M", "width": 1920, "height": 1080},
            "small": {"url": "S", "width": 1280, "height": 720}}},
            {"videos": {"small": {"url": "tiny", "width": 640, "height": 360}}}]
        self.assertEqual(self.v._pixabay_pick(hits), ["M"])

    def test_pixabay_success_downloads(self):
        resp = mock.Mock()
        resp.json.return_value = {"hits": [{"tags": "cyber, security, code", "videos": {"medium": {"url": "U", "width": 1920, "height": 1080}}}]}
        with mock.patch.object(self.v.requests, "get", return_value=resp) as g, \
             mock.patch.object(self.v, "_download") as dl:
            out = self.v.fetch_broll(self.cfg, "server room", self.out)
        self.assertEqual(out, self.out)
        dl.assert_called_once_with("U", self.out)
        self.assertEqual(g.call_args.kwargs["params"]["key"], "KEY")

    def test_falls_back_to_local_folder(self):
        clip = self.v.local_dir(self.cfg) / "mine.mp4"
        clip.write_bytes(b"x")
        resp = mock.Mock()
        resp.json.return_value = {"hits": []}
        with mock.patch.object(self.v.requests, "get", return_value=resp):
            self.assertEqual(self.v.fetch_broll(self.cfg, "q", self.out), clip)

    def test_no_key_no_clips_gives_none(self):
        self.cfg.pixabay_api_key = ""
        self.assertIsNone(self.v.fetch_broll(self.cfg, "q", self.out))

    def test_provider_none(self):
        self.cfg.raw.setdefault("broll", {})["provider"] = "none"
        self.assertIsNone(self.v.fetch_broll(self.cfg, "q", self.out))

    def test_provider_local(self):
        self.cfg.raw.setdefault("broll", {})["provider"] = "local"
        clip = self.v.local_dir(self.cfg) / "a.mov"
        clip.write_bytes(b"x")
        with mock.patch.object(self.v.requests, "get") as g:
            self.assertEqual(self.v.fetch_broll(self.cfg, "q", self.out), clip)
        g.assert_not_called()


class QueryPoolTests(VisualsTests):
    def test_generic_pool_used_by_default(self):
        self.cfg.raw["broll"]["queries"] = ["neon lines"]
        resp = mock.Mock()
        resp.json.return_value = {"hits": [{"tags": "cyber, security, code", "videos": {"medium": {"url": "U", "width": 1920, "height": 1080}}}]}
        with mock.patch.object(self.v.requests, "get", return_value=resp) as g, mock.patch.object(self.v, "_download"):
            self.v.fetch_broll(self.cfg, "apache roller exploit", self.out)
        self.assertEqual(g.call_args_list[0].kwargs["params"]["q"], "neon lines")

    def test_match_story_uses_story_query(self):
        self.cfg.raw["broll"]["match_story"] = True
        resp = mock.Mock()
        resp.json.return_value = {"hits": [{"tags": "cyber, security, code", "videos": {"medium": {"url": "U", "width": 1920, "height": 1080}}}]}
        with mock.patch.object(self.v.requests, "get", return_value=resp) as g, mock.patch.object(self.v, "_download"):
            self.v.fetch_broll(self.cfg, "server room", self.out)
        self.assertEqual(g.call_args_list[0].kwargs["params"]["q"], "server room")


class TagFilterTests(unittest.TestCase):
    def test_tags_ok(self):
        from cybershorts.visuals import tags_ok
        req, exc = ["cyber", "code"], ["nature", "woman"]
        self.assertTrue(tags_ok("cybersecurity, hacker, technology", req, exc))
        self.assertFalse(tags_ok("forest, nature, green", req, exc))
        self.assertFalse(tags_ok("code, woman, laptop", req, exc))
        self.assertFalse(tags_ok("sunset, beach", req, exc))
        self.assertTrue(tags_ok("anything", None, None))

    def test_pick_skips_off_theme(self):
        from cybershorts.visuals import _pixabay_pick
        hits = [{"tags": "flower, nature", "videos": {"medium": {"url": "BAD", "width": 1920, "height": 1080}}},
                {"tags": "binary, code", "videos": {"medium": {"url": "GOOD", "width": 1920, "height": 1080}}}]
        self.assertEqual(_pixabay_pick(hits, ["code"], ["nature"]), ["GOOD"])
