"""b-roll orientation: portrait default unchanged, landscape picks, Pexels orientation param, max_clips override."""
import os
import random
import tempfile
import unittest
from unittest import mock


def _pix_hit(tags, **sizes):
    return {"tags": tags, "videos": {k: {"url": u, "width": w, "height": h} for k, (u, w, h) in sizes.items()}}


class VisualsOrientationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("APP_DIR")
        os.environ["APP_DIR"] = self._tmp.name
        from cybershorts import config, visuals

        self.v = visuals
        self.cfg = config.load()
        self.cfg.pixabay_api_key = "KEY"
        self.cfg.pexels_api_key = "PKEY"

    def tearDown(self):
        if self._old is None:
            os.environ.pop("APP_DIR", None)
        else:
            os.environ["APP_DIR"] = self._old
        self._tmp.cleanup()

    # --- Pixabay pick ---------------------------------------------------------

    def test_portrait_default_matches_explicit_portrait(self):
        hits = [
            _pix_hit("cyber, security", large=("L", 3840, 2160), medium=("M", 1920, 1080), small=("S", 1280, 720)),
            _pix_hit("server", medium=("P", 1080, 1920), small=("PS", 720, 1280)),
            _pix_hit("cat, pets", medium=("X", 1920, 1080)),
        ]
        default = self.v._pixabay_pick(hits)
        self.assertEqual(default, ["M", "P", "X"])
        self.assertEqual(self.v._pixabay_pick(hits, None, None, "portrait"), default)
        self.assertEqual(self.v._pixabay_pick(hits, ["cyber"], ["pets"]), ["M"])

    def test_landscape_prefers_1920_over_960_and_portrait_1080x1920(self):
        hits = [_pix_hit("cyber", medium=("P", 1080, 1920), small=("S960", 960, 540),
                         large=("L1920", 1920, 1080))]
        self.assertEqual(self.v._pixabay_pick(hits, orientation="landscape"), ["L1920"])

    def test_landscape_ignores_small_and_portrait_when_no_landscape_1280_plus(self):
        hits = [_pix_hit("cyber", medium=("P", 1080, 1920), small=("S960", 960, 540))]
        # Nothing >= 1280 wide: no candidate at all (portrait-only clip cannot fill a landscape frame)
        self.assertEqual(self.v._pixabay_pick(hits, orientation="landscape"), [])

    def test_landscape_fallback_is_smallest_1280_plus(self):
        hits = [_pix_hit("cyber", large=("L4K", 3840, 2160), medium=("M2K", 2560, 1440))]
        self.assertEqual(self.v._pixabay_pick(hits, orientation="landscape"), ["M2K"])

    def test_landscape_respects_tag_filters(self):
        hits = [_pix_hit("cat", large=("CAT", 1920, 1080)), _pix_hit("cyber", small=("OK", 1280, 720))]
        self.assertEqual(self.v._pixabay_pick(hits, ["cyber"], ["cat"], "landscape"), ["OK"])

    # --- Pexels pick ----------------------------------------------------------

    def test_pexels_portrait_pick_unchanged(self):
        videos = [{"video_files": [{"link": "a", "width": 1080, "height": 1920},
                                   {"link": "b", "width": 2160, "height": 3840}]}]
        self.assertEqual(self.v._pexels_pick(videos), ["a"])
        self.assertEqual(self.v._pexels_pick(videos, "portrait"), ["a"])

    def test_pexels_landscape_pick_closest_to_1920(self):
        videos = [{"video_files": [{"link": "tiny", "width": 1280, "height": 720},
                                   {"link": "full", "width": 1920, "height": 1080},
                                   {"link": "port", "width": 1080, "height": 1920},
                                   {"link": "4k", "width": 3840, "height": 2160}]}]
        self.assertEqual(self.v._pexels_pick(videos, "landscape"), ["full"])

    # --- request params -------------------------------------------------------

    def _pexels_call(self, orientation=None):
        resp = mock.Mock()
        resp.json.return_value = {"videos": []}
        with mock.patch.object(self.v.requests, "get", return_value=resp) as g:
            if orientation is None:
                self.v._pexels_search("PKEY", "server room")
            else:
                self.v._pexels_search("PKEY", "server room", orientation=orientation)
        return g.call_args

    def test_pexels_default_request_params_unchanged(self):
        call = self._pexels_call()
        self.assertEqual(call.args, (self.v.PEXELS_URL,))
        self.assertEqual(call.kwargs, {
            "params": {"query": "server room", "orientation": "portrait", "per_page": 15},
            "headers": {"Authorization": "PKEY"},
            "timeout": 20,
        })

    def test_pexels_landscape_request_includes_orientation(self):
        call = self._pexels_call("landscape")
        self.assertEqual(call.kwargs["params"]["orientation"], "landscape")
        self.assertEqual(call.kwargs["params"]["query"], "server room")

    def test_pixabay_request_params_unchanged_by_orientation(self):
        resp = mock.Mock()
        resp.json.return_value = {"hits": []}
        with mock.patch.object(self.v.requests, "get", return_value=resp) as g:
            self.v._pixabay_search("KEY", "q", orientation="landscape")
            landscape = g.call_args
            self.v._pixabay_search("KEY", "q")
            default = g.call_args
        self.assertEqual(landscape.kwargs, default.kwargs)
        self.assertEqual(default.kwargs["params"], {"key": "KEY", "q": "q", "safesearch": "true",
                                                     "per_page": 20, "order": "popular"})

    # --- fetch_broll_set plumbing ---------------------------------------------

    def test_fetch_broll_set_threads_orientation_to_provider(self):
        self.cfg.raw.setdefault("broll", {})["provider"] = "pexels"
        self.cfg.raw["broll"]["match_story"] = True
        out = self.cfg.path("work") / "draft_9"
        with mock.patch.object(self.v, "_fetch_remote", return_value=None) as fr:
            self.v.fetch_broll_set(self.cfg, 1, "q", out, orientation="landscape")
        self.assertEqual(fr.call_args.kwargs.get("orientation"), "landscape")

    def test_fetch_broll_set_default_orientation_is_portrait(self):
        self.cfg.raw.setdefault("broll", {})["provider"] = "pixabay"
        self.cfg.raw["broll"]["match_story"] = True
        out = self.cfg.path("work") / "draft_9"
        with mock.patch.object(self.v, "_fetch_remote", return_value=None) as fr:
            self.v.fetch_broll_set(self.cfg, 1, "q", out)
        self.assertEqual(fr.call_args.kwargs.get("orientation"), "portrait")

    def test_max_clips_override_is_honoured(self):
        self.cfg.raw.setdefault("broll", {})["provider"] = "local"
        self.cfg.raw["broll"]["max_clips"] = 2
        local = self.v.local_dir(self.cfg)
        local.mkdir(parents=True, exist_ok=True)
        for i in range(6):
            (local / f"c{i}.mp4").write_bytes(b"x")
        out = self.cfg.path("work") / "draft_9"
        random.seed(0)
        self.assertEqual(len(self.v.fetch_broll_set(self.cfg, 5, "q", out)), 2)
        self.assertEqual(len(self.v.fetch_broll_set(self.cfg, 5, "q", out, max_clips=4)), 4)
        self.assertEqual(len(self.v.fetch_broll_set(self.cfg, 5, "q", out, max_clips=0)), 1)

    def test_max_clips_none_uses_config(self):
        self.cfg.raw.setdefault("broll", {})["provider"] = "local"
        self.cfg.raw["broll"]["max_clips"] = 3
        local = self.v.local_dir(self.cfg)
        local.mkdir(parents=True, exist_ok=True)
        for i in range(6):
            (local / f"c{i}.mp4").write_bytes(b"x")
        out = self.cfg.path("work") / "draft_9"
        self.assertEqual(len(self.v.fetch_broll_set(self.cfg, 5, "q", out, max_clips=None)), 3)


if __name__ == "__main__":
    unittest.main()
