"""Golden files for the Shorts ASS output: build_ass must stay byte-identical."""
import os
import tempfile
import unittest
from pathlib import Path

FIX = Path(__file__).parent / "fixtures" / "shorts_ass"
SCRIPT_WORDS = 9
CARD = {"cve_id": "CVE-2025-12345", "cvss": 9.8, "severity": "Critical", "products": ["Acme VPN", "Acme Edge"],
        "exploited": True, "patch_available": True}


def _words(n, step=0.5):
    return [(i * step, (i + 1) * step, f"w{i}") for i in range(n)]


class ShortsAssGolden(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("APP_DIR")
        os.environ["APP_DIR"] = self._tmp.name
        from cybershorts import captions
        from cybershorts.config import load
        self.c = captions
        self.cfg = load()
        self.words = _words(SCRIPT_WORDS)
        self.chunks = captions.chunk_words(self.words, 3)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("APP_DIR", None)
        else:
            os.environ["APP_DIR"] = self._old
        self._tmp.cleanup()

    def cases(self):
        b = self.c.build_ass
        ch, cfg, w = self.chunks, self.cfg, self.words
        return {
            "plain": lambda: b(ch, cfg, "Big VPN flaw", "@me", 5.0),
            "karaoke_keywords": lambda: b(ch, cfg, "Big VPN flaw", "@me", 5.0, words=w, keywords=["VPN", "flaw"]),
            "badge_card": lambda: b(ch, cfg, "Big VPN flaw", "@me", 12.0, words=w, keywords=["VPN"],
                                    beats=[0.0, 1.5, 3.0], badge="BREAKING", badge_color="#FF3B30", card=CARD),
            "end_card_beats": lambda: b(ch, cfg, "Big VPN flaw", "@me", 5.0, words=w, keywords=["VPN"],
                                        beats=[0.0, 1.5, 3.0]),
        }

    def test_golden(self):
        regen = os.environ.get("REGEN_GOLDEN") == "1"
        for name, fn in self.cases().items():
            with self.subTest(case=name):
                got = fn()
                path = FIX / f"{name}.ass"
                if regen:
                    path.write_text(got, encoding="utf-8", newline="")
                self.assertEqual(got, path.read_bytes().decode("utf-8"))

    def test_write_ass_matches(self):
        out = Path(self._tmp.name) / "x.ass"
        self.c.write_ass(self.chunks, self.cfg, "Big VPN flaw", "@me", 5.0, out, words=self.words,
                         keywords=["VPN"], beats=[0.0, 1.5, 3.0])
        self.assertEqual(out.read_bytes(), (FIX / "end_card_beats.ass").read_bytes())


if __name__ == "__main__":
    unittest.main()
