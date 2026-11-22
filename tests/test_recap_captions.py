import os
import re
import tempfile
import unittest
from pathlib import Path

CARD = {"cve_id": "CVE-2025-12345", "cvss": 9.8, "severity": "Critical", "products": ["Acme VPN"],
        "exploited": True, "patch_available": True}
DUR = 120.0
PARTS = [
    {"kind": "intro", "start": 0.0, "end": 10.0, "title": "Intro", "index": 0, "total": 3},
    {"kind": "story", "start": 10.0, "end": 50.0, "title": "Big VPN flaw exploited", "index": 1, "total": 3},
    {"kind": "story", "start": 50.0, "end": 53.0, "title": "Short one", "index": 2, "total": 3},
    {"kind": "story", "start": 53.0, "end": 100.0, "title": "Ransomware hits hospital", "index": 3, "total": 3},
    {"kind": "outro", "start": 100.0, "end": DUR, "title": "Outro", "index": 4, "total": 3},
]
DIAL = re.compile(r"Dialogue: (\d+),(\d+:\d\d:\d\d\.\d\d),(\d+:\d\d:\d\d\.\d\d),(\w+),,0,0,0,,(.*)")


def secs(t):
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def events(ass):
    return [(int(m[1]), secs(m[2]), secs(m[3]), m[4], m[5]) for m in map(DIAL.match, ass.splitlines()) if m]


class RecapCaptions(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("APP_DIR")
        os.environ["APP_DIR"] = self._tmp.name
        from cybershorts import captions, recap_captions
        from cybershorts.config import load
        self.rc, self.cfg = recap_captions, load()
        self.words = [(i * 0.5, i * 0.5 + 0.4, f"w{i}") for i in range(230)]
        self.chunks = captions.chunk_words(self.words, 6)
        self.ov = {1: {"badge": "BREAKING", "badge_color": "#FF3B30", "card": CARD}, 3: {"card": CARD}}

    def tearDown(self):
        if self._old is None:
            os.environ.pop("APP_DIR", None)
        else:
            os.environ["APP_DIR"] = self._old
        self._tmp.cleanup()

    def build(self, overlays="default", words=None, chunks=None):
        return self.rc.build_recap_ass(
            self.cfg, self.words if words is None else words, self.chunks if chunks is None else chunks,
            PARTS, DUR, title="This Week in Cyber", week_label="Week 47", handle="@cyber",
            overlays=self.ov if overlays == "default" else overlays)

    def test_header(self):
        a = self.build()
        self.assertIn("PlayResX: 1920", a)
        self.assertIn("PlayResY: 1080", a)

    def test_times_valid(self):
        evs = events(self.build())
        self.assertTrue(evs)
        for _l, s, e, _st, _t in evs:
            self.assertLess(s, e)
            self.assertLessEqual(e, DUR + 0.005)

    def test_title_cards(self):
        evs = events(self.build())
        for p in PARTS:
            if p["kind"] != "story":
                continue
            hits = [e for e in evs if f"STORY {p['index']} OF 3" in e[4]]
            self.assertEqual(len(hits), 1)
            self.assertAlmostEqual(hits[0][1], p["start"], places=1)
            self.assertLessEqual(hits[0][2], p["end"] + 0.01)

    def test_data_card_inside_part_right_half(self):
        evs = events(self.build())
        card = [e for e in evs if e[3] == "Card" and "\\move(" in e[4]]
        self.assertTrue(card)
        for _l, s, e, _st, t in card:
            part = next(p for p in PARTS if p["kind"] == "story" and p["start"] <= s < p["end"])
            self.assertGreaterEqual(s, part["start"] + 3.5 - 0.01)
            self.assertLessEqual(e, part["end"] + 0.01)
            x = int(re.search(r"\\move\((-?\d+),", t)[1])
            self.assertGreater(x, 960)
        # story 2 is too short (3 s) for a card or ends inside its part; no card for it anyway
        self.assertFalse([e for e in card if 50.0 <= e[1] < 53.0])

    def test_captions_lower_third(self):
        caps = [e for e in events(self.build()) if e[3] == "Caption"]
        self.assertTrue(caps)
        for e in caps:
            y = int(re.search(r"\\pos\(\d+,(\d+)\)", e[4])[1])
            self.assertGreaterEqual(y, 0.8 * 1080)

    def test_plain_fallback(self):
        caps = [e for e in events(self.build(words=[])) if e[3] == "Caption"]
        self.assertTrue(caps)

    def test_intro_outro(self):
        a = self.build()
        self.assertIn("THIS WEEK IN CYBER", a)
        self.assertIn("WEEK 47", a)
        self.assertIn("SUBSCRIBE", a)
        self.assertIn("@cyber", a)

    def test_overlays_none(self):
        a = self.build(overlays=None)
        self.assertIn("STORY 1 OF 3", a)
        self.assertNotIn("CVE-2025-12345", a)

    def test_write(self):
        out = Path(self._tmp.name) / "r.ass"
        r = self.rc.write_recap_ass(self.cfg, self.words, self.chunks, PARTS, DUR, out,
                                    title="T", week_label="W", handle="@h")
        self.assertEqual(r, out)
        self.assertIn("[Events]", out.read_text())

    def test_thumbnail(self):
        a = self.rc.build_thumbnail_ass(self.cfg, "Week in cyber")
        self.assertIn("PlayResX: 1280", a)
        self.assertIn("PlayResY: 720", a)
        self.assertIn("WEEK IN CYBER", a)
        self.assertIn("WEEKLY RECAP", a)
        long = self.rc.build_thumbnail_ass(self.cfg, "Massive router botnet takes down ISP")
        self.assertIn("\\N", long)
        self.assertIn("MASSIVE ROUTER", long.replace("\\N", " "))


if __name__ == "__main__":
    unittest.main()
