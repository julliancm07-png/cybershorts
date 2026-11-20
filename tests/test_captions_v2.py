import os
import tempfile
import unittest

SCRIPT = "One two three.\n\nFour five six seven.\n\nEight nine."


def _words(n, step=0.5):
    return [(i * step, (i + 1) * step, f"w{i}") for i in range(n)]


class CaptionsV2Tests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("APP_DIR")
        os.environ["APP_DIR"] = self._tmp.name
        from cybershorts import captions
        from cybershorts.config import load

        self.c = captions
        self.cfg = load()

    def tearDown(self):
        if self._old is None:
            os.environ.pop("APP_DIR", None)
        else:
            os.environ["APP_DIR"] = self._old
        self._tmp.cleanup()

    def _ass(self, cfg_over=None, **kw):
        if cfg_over:
            self.cfg.raw = {**self.cfg.raw}
            for k, (sect, val) in cfg_over.items():
                self.cfg.raw[sect] = {**self.cfg.raw.get(sect, {}), k: val}
        words = _words(9)
        chunks = self.c.chunk_words(words, 3)
        kw.setdefault("words", words)
        return self.c.build_ass(chunks, self.cfg, "Big VPN flaw", "@me", 5.0, **kw)

    def _dialogues(self, ass, style):
        return [ln for ln in ass.splitlines() if ln.startswith("Dialogue:") and f",{style}," in ln]

    def test_ass_color(self):
        self.assertEqual(self.c.ass_color("#3DDCFF"), "&H00FFDC3D")
        self.assertEqual(self.c.ass_color("garbage"), "&H00FFFFFF")

    def test_beat_ranges(self):
        self.assertEqual(self.c.beat_ranges(SCRIPT), [3, 4, 2])
        self.assertEqual(self.c.beat_ranges("a b\nc d e"), [2, 3])
        self.assertEqual(self.c.beat_ranges(""), [])

    def test_beat_times_aligned(self):
        words = _words(9)
        self.assertEqual(self.c.beat_times(words, SCRIPT, 4.5), [0.0, 1.5, 3.5])

    def test_beat_times_mismatch_proportional(self):
        times = self.c.beat_times(_words(5), SCRIPT, 9.0)
        self.assertEqual(len(times), 3)
        self.assertEqual(times[0], 0.0)
        self.assertAlmostEqual(times[1], 3.0)
        self.assertAlmostEqual(times[2], 7.0)

    def test_beat_times_strictly_increasing_and_min_one(self):
        words = [(0.0, 0.0, f"w{i}") for i in range(9)]
        times = self.c.beat_times(words, SCRIPT, 0.0)
        self.assertTrue(all(b > a for a, b in zip(times, times[1:])))
        self.assertEqual(self.c.beat_times([], "", 3.0), [0.0])
        self.assertEqual(self.c.beat_times([], "just one paragraph", 3.0), [0.0])

    def test_karaoke_one_dialogue_per_word(self):
        ass = self._ass()
        events = self._dialogues(ass, "Caption")
        self.assertEqual(len(events), 9)
        for ln in events:
            self.assertIn("&H00FFDC3D&", ln)  # accent on current word
        self.assertIn("fscx85", events[0])
        self.assertNotIn("fscx85", events[1])

    def test_keywords_get_keyword_colour(self):
        ass = self._ass(keywords=["w1"])
        self.assertIn("&H000AD6FF&", "".join(self._dialogues(ass, "Caption")))

    def test_plain_style_and_no_words_fallback(self):
        chunks = self.c.chunk_words(_words(9), 3)
        plain = self.c.build_ass(chunks, self.cfg, "T", "@me", 5.0)
        self.assertEqual(len(self._dialogues(plain, "Caption")), 3)
        self.cfg.raw["captions"] = {**self.cfg.raw["captions"], "style": "plain"}
        ass = self.c.build_ass(chunks, self.cfg, "T", "@me", 5.0, words=_words(9))
        self.assertEqual(len(self._dialogues(ass, "Caption")), 3)

    def test_hook_only_when_seconds_positive(self):
        self.assertEqual(len(self._dialogues(self._ass(), "Hook")), 1)
        off = self._ass({"hook_seconds": ("video", 0)})
        self.assertEqual(len(self._dialogues(off, "Hook")), 0)
        self.assertIn("0:00:00.00,0:00:05.00,Title", off)

    def test_title_starts_after_hook(self):
        ass = self._ass()
        self.assertTrue(any(ln.startswith("Dialogue: 0,0:00:01.60") for ln in self._dialogues(ass, "Title")))

    def test_progress_bar(self):
        self.assertEqual(len(self._dialogues(self._ass(), "Bar")), 1)
        off = self._ass({"progress_bar": ("branding", False)})
        self.assertEqual(len(self._dialogues(off, "Bar")), 0)

    def test_end_card_with_beats(self):
        ass = self._ass(beats=[0.0, 2.0, 3.5])
        card = [ln for ln in self._dialogues(ass, "Caption") if "FOLLOW" in ln]
        self.assertEqual(len(card), 1)
        self.assertIn("0:00:03.50", card[0])
        self.assertIn("@me", card[0])
        title = self._dialogues(ass, "Title")[0]
        self.assertIn("0:00:03.50", title)  # title ends at last beat start

    def test_handle_line_ends_at_end_card(self):
        ass = self._ass(beats=[0.0, 2.0, 3.5])
        handle = self._dialogues(ass, "Handle")
        self.assertEqual(len(handle), 1)
        self.assertIn("0:00:00.00,0:00:03.50,Handle", handle[0])
        plain = self._ass()
        self.assertIn("0:00:00.00,0:00:05.00,Handle", self._dialogues(plain, "Handle")[0])

    def test_no_end_card_without_beats(self):
        for beats in (None, [0.0]):
            ass = self._ass(beats=beats)
            self.assertFalse([ln for ln in self._dialogues(ass, "Caption") if "FOLLOW" in ln])

    @staticmethod
    def _times(lines):
        out = []
        for ln in lines:
            a, b = ln.split(",")[1:3]
            f = lambda t: round(sum(float(x) * m for x, m in zip(t.split(":"), (3600, 60, 1))) * 100)  # noqa: E731
            out.append((f(a), f(b)))
        return out

    def test_caption_events_never_overlap(self):
        # overlapping / zero-length / identical word timings
        words = [(0.0, 0.4, "a"), (0.2, 0.6, "b"), (0.2, 0.2, "c"), (0.2, 0.2, "d"), (0.25, 0.9, "e"), (0.3, 0.3, "f")]
        chunks = self.c.chunk_words(words, 3)
        for style in ("karaoke", "plain"):
            self.cfg.raw["captions"] = {**self.cfg.raw["captions"], "style": style}
            ass = self.c.build_ass(chunks, self.cfg, "", "", 3.0, words=words)
            t = self._times(self._dialogues(ass, "Caption"))
            self.assertTrue(t)
            for (s1, e1), (s2, _e2) in zip(t, t[1:]):
                self.assertLessEqual(e1, s2)
            for s1, e1 in t:
                self.assertGreater(e1, s1)

    def test_end_card_suppresses_late_captions(self):
        words = _words(9)  # 0..4.5s, 0.5s per word
        chunks = self.c.chunk_words(words, 3)
        for style in ("karaoke", "plain"):
            self.cfg.raw["captions"] = {**self.cfg.raw["captions"], "style": style}
            ass = self.c.build_ass(chunks, self.cfg, "T", "@me", 5.0, words=words, beats=[0.0, 2.0, 3.25])
            evs = [ln for ln in self._dialogues(ass, "Caption") if "FOLLOW" not in ln]
            t = self._times(evs)
            self.assertTrue(t)
            self.assertTrue(all(e <= 325 and s < 325 for s, e in t), (style, t))
            self.assertEqual(max(e for _s, e in t), 325)  # the running one is trimmed, not dropped

    def test_text_braces_are_escaped(self):
        words = [(0.0, 0.5, "a{b}c")]
        ass = self.c.build_ass([(0.0, 0.5, "a{b}c")], self.cfg, "", "", 1.0, words=words)
        self.assertNotIn("{b}", ass)


if __name__ == "__main__":
    unittest.main()
