"""Per-story b-roll queries and the recently-used clip memory."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cybershorts import config, visuals, writer


def _cfg(**broll):
    raw = copy.deepcopy(config.DEFAULTS)
    raw["broll"].update(broll)
    return config.Config(tempfile.mkdtemp(), raw, {"PIXABAY_API_KEY": "k" * 20})


class CleanQueriesTests(unittest.TestCase):
    def test_cleaning(self):
        got = writer.clean_broll_queries(["Hospital Hallway!", "phone text message at night now", "", "x",
                                          "credit card payment"] + ["server room"] * 10)
        self.assertEqual(got[:3], ["hospital hallway", "phone text message at", "credit card payment"])
        self.assertLessEqual(len(got), 8)
        self.assertEqual(writer.clean_broll_queries("a b, c d"), ["a b", "c d"])
        self.assertEqual(writer.clean_broll_queries(None), [])


class FetchSetTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def fake_search(key, q, require, exclude, skip=None, orientation="portrait"):
            self.calls.append((q, require, exclude))
            n = len(self.calls)
            link = f"https://cdn.example/v{n}.mp4?key=SECRET"
            return None if (skip and (link in skip or visuals._clip_id(link) in skip)) else link

        self.p1 = mock.patch.object(visuals, "_pixabay_search", side_effect=fake_search)
        self.p2 = mock.patch.object(visuals, "_download", side_effect=lambda link, out: Path(out).write_bytes(b"v"))
        self.p1.start()
        self.p2.start()

    def tearDown(self):
        self.p1.stop()
        self.p2.stop()

    def test_story_queries_first_with_relaxed_tags(self):
        cfg = _cfg()
        clips = visuals.fetch_broll_set(cfg, 3, "technology", Path(cfg.path("work")) / "d1_broll",
                                        story_queries=["hospital hallway", "phone text message", "server room"])
        self.assertEqual(len(clips), 3)
        self.assertEqual([c[0] for c in self.calls], ["hospital hallway", "phone text message", "server room"])
        self.assertTrue(all(c[1] is None for c in self.calls))  # no cyber-only require rule
        self.assertIn("child", self.calls[0][2])

    def test_mixed_alternates(self):
        cfg = _cfg(variety="mixed")
        visuals.fetch_broll_set(cfg, 2, "technology", Path(cfg.path("work")) / "d2_broll",
                                story_queries=["hospital hallway", "bank card"])
        self.assertEqual(self.calls[0][0], "hospital hallway")
        self.assertIn(self.calls[1][0], cfg.get("broll.queries"))  # second clip from the cyber pool

    def test_cyber_mode_ignores_story(self):
        cfg = _cfg(variety="cyber")
        visuals.fetch_broll_set(cfg, 2, "technology", Path(cfg.path("work")) / "d3_broll",
                                story_queries=["hospital hallway", "bank card"])
        self.assertTrue(all(c[0] in cfg.get("broll.queries") for c in self.calls))

    def test_recent_memory_saved_without_key_and_respected(self):
        cfg = _cfg()
        visuals.fetch_broll_set(cfg, 2, "t", Path(cfg.path("work")) / "d4_broll", story_queries=["a b", "c d"])
        saved = json.loads(cfg.path(visuals.RECENT_FILE).read_text())
        self.assertEqual(len(saved), 2)
        self.assertNotIn("SECRET", json.dumps(saved))
        self.assertEqual(visuals._load_recent(cfg), saved)

    def test_recent_memory_skips(self):
        cfg = _cfg()
        cfg.path(visuals.RECENT_FILE).write_text(json.dumps([visuals._clip_id("https://cdn.example/v1.mp4")]))
        clips = visuals.fetch_broll_set(cfg, 1, "t", Path(cfg.path("work")) / "d5_broll", story_queries=["a b"])
        self.assertEqual(len(clips), 1)  # v1 was skipped, the next candidate was used
        self.assertGreaterEqual(len(self.calls), 2)


if __name__ == "__main__":
    unittest.main()
