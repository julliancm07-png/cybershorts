import os
import re
import tempfile
import unittest

SCRIPT = "One two three.\n\nFour five six seven.\n\nEight nine."
CARD = {"cve_id": "CVE-2025-12345", "cvss": 9.8, "severity": "Critical", "products": ["Acme VPN", "Acme Edge"],
        "exploited": True, "patch_available": True}


def _words(n, step=0.5):
    return [(i * step, (i + 1) * step, f"w{i}") for i in range(n)]


def secs(t):
    h, m, rest = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


class CaptionsV3Tests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("APP_DIR")
        os.environ["APP_DIR"] = self._tmp.name
        from cybershorts import captions
        from cybershorts.config import load
        self.c = captions
        self.cfg = load()
        self.words = _words(9)
        self.chunks = captions.chunk_words(self.words, 3)
        self.beats = [0.0, 1.5, 3.0]  # beat 2 window = 1.5 .. 3.0

    def tearDown(self):
        if self._old is None:
            os.environ.pop("APP_DIR", None)
        else:
            os.environ["APP_DIR"] = self._old
        self._tmp.cleanup()

    def ass(self, dur=5.0, beats=None, **kw):
        return self.c.build_ass(self.chunks, self.cfg, "Big VPN flaw", "@me", dur, words=self.words,
                                keywords=["VPN"], beats=self.beats if beats is None else beats, **kw)

    def card_lines(self, ass):
        return [ln for ln in ass.splitlines() if ln.startswith("Dialogue: ") and ",Card," in ln]

    def test_old_calls_identical(self):
        base = self.ass()
        self.assertEqual(base, self.ass(badge=None, badge_color=None, card=None))
        self.assertEqual(base, self.ass(card={}))
        self.assertEqual(base, self.ass(card={"cve_id": "", "cvss": None, "products": [], "exploited": False}))
        self.assertNotIn("Style: Card", base)
        self.assertEqual(self.card_lines(base), [])

    def test_write_ass_kwargs(self):
        out = self.c.write_ass(self.chunks, self.cfg, "T", "@me", 5.0, os.path.join(self._tmp.name, "a.ass"),
                               words=self.words, beats=self.beats, badge="BREAKING", card=CARD)
        txt = out.read_text(encoding="utf-8")
        self.assertIn("BREAKING", txt)
        self.assertIn("CVE-2025-12345", txt)

    def test_badge_timing_color_and_shape(self):
        a = self.ass(badge="Breaking", badge_color="#00FF00")
        lines = self.card_lines(a)
        self.assertEqual(len(lines), 2)
        self.assertIn("\\p1", lines[0])
        self.assertIn("&H0000FF00&", lines[0])
        self.assertIn("\\fad(", lines[0])
        self.assertTrue(lines[1].endswith("BREAKING"))
        self.assertIn("&H00000000&", lines[1])  # black text
        for ln in lines:
            f = ln.split(",")
            self.assertEqual(secs(f[1]), 0.0)
            self.assertAlmostEqual(secs(f[2]), 1.6, places=2)

    def test_badge_lasts_for_longer_hook(self):
        self.cfg.raw["video"] = {**self.cfg.raw.get("video", {}), "hook_seconds": 2.4}
        f = self.card_lines(self.ass(badge="X"))[0].split(",")
        self.assertAlmostEqual(secs(f[2]), 2.4, places=2)

    def test_badge_default_color_from_config(self):
        a = self.ass(badge="X")
        self.assertIn("&H00303BFF&", a)  # #FF3B30 -> BBGGRR

    def test_card_rows_only_truthy(self):
        txt = "\n".join(self.card_lines(self.ass(card={"cve_id": "CVE-2025-1", "exploited": True})))
        self.assertIn("CVE-2025-1", txt)
        self.assertIn("ACTIVELY EXPLOITED", txt)
        for absent in ("CVSS", "Affects", "PATCH"):
            self.assertNotIn(absent, txt)
        full = "\n".join(self.card_lines(self.ass(card=CARD)))
        for present in ("CVE-2025-12345", "CVSS 9.8 CRITICAL", "Affects: Acme VPN, Acme Edge",
                        "ACTIVELY EXPLOITED", "PATCH AVAILABLE \u2713"):
            self.assertIn(present, full)
        self.assertNotIn("ACTIVELY", "\n".join(self.card_lines(self.ass(card={**CARD, "exploited": False}))))
        self.assertNotIn("PATCH", "\n".join(self.card_lines(self.ass(card={**CARD, "patch_available": None}))))

    def test_card_window_inside_beat_two(self):
        for ln in self.card_lines(self.ass(card=CARD)):
            f = ln.split(",")
            self.assertAlmostEqual(secs(f[1]), 1.5, places=2)
            self.assertLessEqual(secs(f[2]), 3.0 + 1e-6)
            self.assertIn(f[3], ("Card",))
            self.assertIn(f[0], ("Dialogue: 4", "Dialogue: 5"))

    def test_card_capped_by_seconds_and_endcard(self):
        self.cfg.raw["data_card"] = {"seconds": 1.2}
        self.assertAlmostEqual(secs(self.card_lines(self.ass(card=CARD, beats=[0.0, 1.0, 6.0], dur=9.0))[0].split(",")[2]),
                               2.2, places=2)
        self.cfg.raw["data_card"] = {"seconds": 4.5}
        # end card starts at beats[-1]=3.0 (dur - 3.0 <= 4.5): card may not run past it
        ends = [secs(ln.split(",")[2]) for ln in self.card_lines(self.ass(card=CARD, beats=[0.0, 1.0, 3.0], dur=5.0))]
        self.assertTrue(ends and max(ends) <= 3.0 + 1e-6)

    def test_card_skipped_when_window_under_one_second(self):
        self.assertEqual(self.card_lines(self.ass(card=CARD, beats=[0.0, 1.5, 2.2], dur=5.0)), [])

    def test_card_fallback_start_without_beats(self):
        a = self.c.build_ass(self.chunks, self.cfg, "T", "@me", 8.0, words=self.words, beats=[0.0], card=CARD)
        self.assertAlmostEqual(secs(self.card_lines(a)[0].split(",")[1]), 2.0, places=2)

    def test_card_animation_and_stagger(self):
        lines = self.card_lines(self.ass(card=CARD))
        self.assertIn("\\move(", lines[0])
        self.assertIn("\\fad(", lines[0])
        starts = [int(m.group(1)) for ln in lines if (m := re.search(r"\\t\((\d+),\d+,\\alpha", ln))]
        self.assertEqual(starts, sorted(starts))
        self.assertTrue(all(b - a >= 120 for a, b in zip(sorted(set(starts)), sorted(set(starts))[1:])))

    def test_cvss_color_bands(self):
        for score, col in ((9.8, "#FF3B30"), (7.5, "#FF9500"), (5.0, "#FFD60A"), (2.0, "#34C759")):
            a = self.ass(card={"cvss": score})
            self.assertIn(self.c.ass_color(col), a, score)

    def test_long_products_truncated(self):
        a = "\n".join(self.card_lines(self.ass(card={"products": ["A" * 80]})))
        row = re.search(r"Affects: [A\u2026]+", a).group(0)
        self.assertLessEqual(len(row), 40)

    def test_text_is_escaped(self):
        a = self.ass(card={"cve_id": "CVE-1{x}", "products": "Foo {bar}"}, badge="a{b}")
        self.assertNotIn("{x}", a)
        self.assertNotIn("{bar}", a)


if __name__ == "__main__":
    unittest.main()
