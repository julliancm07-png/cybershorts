import os
import tempfile
import unittest
from unittest import mock


def _hits(n):
    return {"hits": [{"tags": "cyber, code", "videos": {"medium": {"url": f"U{i}", "width": 1920, "height": 1080}}}
                     for i in range(n)]}


class FetchSetTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("APP_DIR")
        os.environ["APP_DIR"] = self._tmp.name
        from cybershorts import config, visuals

        self.v = visuals
        self.cfg = config.load()
        self.cfg.pixabay_api_key = "KEY"
        self.prefix = self.cfg.path("work") / "draft_1_broll"

    def tearDown(self):
        if self._old is None:
            os.environ.pop("APP_DIR", None)
        else:
            os.environ["APP_DIR"] = self._old
        self._tmp.cleanup()

    def _run(self, n, hits=4):
        resp = mock.Mock()
        resp.json.return_value = _hits(hits)
        with mock.patch.object(self.v.requests, "get", return_value=resp) as g, \
             mock.patch.object(self.v, "_download") as dl:
            out = self.v.fetch_broll_set(self.cfg, n, "server room", self.prefix)
        return out, g, dl

    def test_distinct_urls_and_names(self):
        out, _, dl = self._run(3)
        self.assertEqual([p.name for p in out],
                         ["draft_1_broll_0.mp4", "draft_1_broll_1.mp4", "draft_1_broll_2.mp4"])
        urls = [c.args[0] for c in dl.call_args_list]
        self.assertEqual(len(set(urls)), 3)

    def test_distinct_queries_when_not_matching_story(self):
        self.cfg.raw["broll"]["queries"] = ["a", "b", "c"]
        self.cfg.raw["broll"]["match_story"] = False
        _, g, _ = self._run(3)
        qs = [c.kwargs["params"]["q"] for c in g.call_args_list]
        self.assertEqual(sorted(qs), ["a", "b", "c"])

    def test_match_story_same_query(self):
        self.cfg.raw["broll"]["match_story"] = True
        _, g, _ = self._run(2)
        self.assertEqual({c.kwargs["params"]["q"] for c in g.call_args_list}, {"server room"})

    def test_max_clips_cap(self):
        self.cfg.raw["broll"]["max_clips"] = 2
        out, _, _ = self._run(5, hits=8)
        self.assertEqual(len(out), 2)

    def test_local_fill_distinct(self):
        d = self.v.local_dir(self.cfg)
        d.mkdir(parents=True, exist_ok=True)
        for n in ("a.mp4", "b.mp4", "c.mp4"):
            (d / n).write_bytes(b"x")
        out, _, _ = self._run(3, hits=1)  # only one distinct remote URL available
        self.assertEqual(len(out), 3)
        self.assertEqual(len(set(out)), 3)
        self.assertEqual(sum(1 for p in out if p.parent == d), 2)

    def test_provider_none(self):
        self.cfg.raw["broll"]["provider"] = "none"
        with mock.patch.object(self.v.requests, "get") as g:
            self.assertEqual(self.v.fetch_broll_set(self.cfg, 3, "q", self.prefix), [])
        g.assert_not_called()

    def test_nothing_found(self):
        self.cfg.pixabay_api_key = ""
        self.assertEqual(self.v.fetch_broll_set(self.cfg, 3, "q", self.prefix), [])


if __name__ == "__main__":
    unittest.main()
