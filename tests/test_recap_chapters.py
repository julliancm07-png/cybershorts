import copy
import tempfile
import unittest
from pathlib import Path

from cybershorts import config, recap


def parts(starts, titles=None):
    n = len(starts)
    out = []
    for i, s in enumerate(starts):
        kind = "intro" if i == 0 else ("outro" if i == n - 1 else "story")
        out.append({"kind": kind, "start": s, "title": (titles or {}).get(i, f"Story {i}")})
    return out


class ChapterTests(unittest.TestCase):
    def test_basic(self):
        ch = recap.chapters(parts([0, 20.7, 80, 140, 200]), 240)
        self.assertEqual(ch, [(0, "Intro"), (20, "Story 1"), (80, "Story 2"), (140, "Story 3")])
        self.assertTrue(recap.valid_chapters(ch, 240))

    def test_short_intro_merges_forward(self):
        ch = recap.chapters(parts([0, 4, 60, 120, 180]), 240)
        self.assertEqual(ch[0], (0, "Story 1"))
        self.assertEqual([c[0] for c in ch], [0, 60, 120])
        self.assertTrue(recap.valid_chapters(ch, 240))

    def test_short_middle_part_merges_back(self):
        ch = recap.chapters(parts([0, 20, 25, 80, 140, 200]), 240)
        self.assertEqual(ch, [(0, "Intro"), (25, "Story 2"), (80, "Story 3"), (140, "Story 4")])
        self.assertTrue(recap.valid_chapters(ch, 240))

    def test_short_last_story_merges_and_outro_merged(self):
        # last story only 5 s before the end (outro merged into it counts toward its length)
        ch = recap.chapters(parts([0, 20, 80, 140, 236]), 240)
        self.assertEqual([c[0] for c in ch], [0, 20, 80, 140])

    def test_too_few(self):
        self.assertEqual(recap.chapters(parts([0, 30, 60]), 90), [])
        self.assertEqual(recap.chapters(parts([0, 5, 8, 12, 15]), 20), [])

    def test_valid_rules(self):
        v = recap.valid_chapters
        self.assertTrue(v([(0, "a"), (10, "b"), (20, "c")], 30))
        self.assertFalse(v([(1, "a"), (20, "b"), (40, "c")], 60))  # first != 0
        self.assertFalse(v([(0, "a"), (20, "b")], 60))  # < 3
        self.assertFalse(v([(0, "a"), (20, "b"), (20, "c")], 60))  # not increasing
        self.assertFalse(v([(0, "a"), (20, "b"), (29, "c")], 60))  # gap < 10
        self.assertFalse(v([(0, "a"), (20, "b"), (40, "c")], 49))  # last vs duration
        self.assertFalse(v([], 60))

    def test_format(self):
        self.assertEqual(recap.format_chapters([(0, "Intro"), (75, "A"), (3600, "B"), (3725, "C")]),
                         "00:00 Intro\n01:15 A\n1:00:00 B\n1:02:05 C")


class StartTests(unittest.TestCase):
    def test_from_spans(self):
        spans = [[0, 4], [5, 9], [10, 20], [21, 30], [31, 40]]
        self.assertEqual(recap.part_starts(spans, [1, 2, 2]), [0.0, 5.0, 21.0])
        self.assertIsNone(recap.part_starts(spans, [1, 2]))
        self.assertIsNone(recap.part_starts([], [1]))

    def test_from_words_exact_and_fallback(self):
        script = "a b\n\nc d e\n\nf"
        words = [(float(i), i + 0.5, "w") for i in range(6)]
        self.assertEqual(recap.part_starts_from_words(words, script, [1, 1, 1]), [0.0, 2.0, 5.0])
        self.assertEqual(recap.part_starts_from_words(words, script, [2, 1]), [0.0, 5.0])
        # word list length mismatch: positions scaled by word share
        short = [(float(i * 2), i * 2 + 1.0, "w") for i in range(3)]
        got = recap.part_starts_from_words(short, script, [1, 1, 1])
        self.assertEqual(got[0], 0.0)
        self.assertEqual(sorted(got), got)
        self.assertIsNone(recap.part_starts_from_words(words, script, [1, 1]))
        self.assertIsNone(recap.part_starts_from_words([], script, [1, 1, 1]))


class DescriptionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = config.Config(Path(self._tmp.name), copy.deepcopy(config.DEFAULTS), {})
        self.stories = [{"title": f"Story {i}", "youtube_id": f"yt{i}", "url": f"https://ex.com/{i}"}
                        for i in range(1, 4)]

    def tearDown(self):
        self._tmp.cleanup()

    def data(self, **kw):
        d = {"description": "Body text.", "hashtags": ["a", "b"], "duration": 240.6}
        d.update(kw)
        return d

    def test_with_chapters(self):
        ch = [(0, "Intro"), (20, "Story 1"), (80, "Story 2"), (140, "Story 3")]
        desc = recap.build_description(self.cfg, self.data(), ch, self.stories)
        self.assertIn("Chapters\n00:00 Intro\n00:20 Story 1", desc)
        self.assertIn("• Story 1\nShort: https://youtube.com/shorts/yt1\nSource: https://ex.com/1", desc)
        self.assertTrue(desc.endswith("#a #b"))
        self.assertLess(desc.index("Body text."), desc.index("Chapters"))

    def test_invalid_chapters_omitted(self):
        for ch in ([], [(0, "Intro"), (5, "x"), (9, "y")]):
            desc = recap.build_description(self.cfg, self.data(), ch, self.stories)
            self.assertNotIn("Chapters", desc)

    def test_cap_trims_sources_first(self):
        ch = [(0, "Intro"), (20, "Story 1"), (80, "Story 2"), (140, "Story 3")]
        stories = [{"title": "T" * 200, "youtube_id": "y" * 11, "url": "https://ex.com/" + "u" * 800}
                   for _ in range(8)]
        desc = recap.build_description(self.cfg, self.data(), ch, stories)
        self.assertLessEqual(len(desc), 4900)
        self.assertIn("Chapters\n00:00 Intro", desc)
        self.assertIn("02:20 Story 3", desc)
        self.assertNotIn("Source: ", desc)

    def test_cap_extreme_body(self):
        ch = [(0, "Intro"), (20, "Story 1"), (80, "Story 2"), (140, "Story 3")]
        desc = recap.build_description(self.cfg, self.data(description="B" * 9000), ch, self.stories)
        self.assertLessEqual(len(desc), 4900)
        self.assertIn("02:20 Story 3", desc)


if __name__ == "__main__":
    unittest.main()
