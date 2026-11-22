"""Golden files for the classic recap output: build_recap_ass and render_recap argv must stay unchanged.

Regenerate deliberately with REGEN_GOLDEN=1 (the test then rewrites the fixtures and passes).
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

FIX = Path(__file__).parent / "fixtures" / "recap_classic"
CARD = {"cve_id": "CVE-2025-12345", "cvss": 9.8, "severity": "Critical", "products": ["Acme VPN"],
        "exploited": True, "patch_available": True}
DUR = 120.0
# intro + 3 stories + outro
PARTS = [
    {"kind": "intro", "start": 0.0, "end": 10.0, "title": "Intro", "index": 0, "total": 3},
    {"kind": "story", "start": 10.0, "end": 50.0, "title": "Big VPN flaw exploited", "index": 1, "total": 3},
    {"kind": "story", "start": 50.0, "end": 53.0, "title": "Short one", "index": 2, "total": 3},
    {"kind": "story", "start": 53.0, "end": 100.0, "title": "Ransomware hits hospital", "index": 3, "total": 3},
    {"kind": "outro", "start": 100.0, "end": DUR, "title": "Outro", "index": 4, "total": 3},
]


class FakeRun:
    """subprocess.run stand-in: records argv, creates the output file (last arg) for ffmpeg."""

    def __init__(self):
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append((list(cmd), kw))
        r = mock.Mock(returncode=0, stdout="", stderr="")
        if cmd[0] == "ffmpeg":
            Path(cmd[-1]).write_bytes(b"0" * 10)
        return r


def _normalize(text, roots):
    for root in roots:
        text = text.replace(root, "<APP>")
    return text


class RecapClassicGolden(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("APP_DIR")
        os.environ["APP_DIR"] = self._tmp.name
        from cybershorts import recap_captions, recap_render
        from cybershorts.config import load
        self.rc, self.rr, self.cfg = recap_captions, recap_render, load()
        self.root = Path(self._tmp.name)
        self.roots = sorted({str(self.root), str(self.root.resolve())}, key=len, reverse=True)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("APP_DIR", None)
        else:
            os.environ["APP_DIR"] = self._old
        self._tmp.cleanup()

    def _golden_ass(self):
        from cybershorts import captions
        words = [(i * 0.5, i * 0.5 + 0.4, f"w{i}") for i in range(230)]
        chunks = captions.chunk_words(words, 6)
        overlays = {1: {"badge": "BREAKING", "badge_color": "#FF3B30", "card": CARD}, 3: {"card": CARD}}
        return self.rc.build_recap_ass(self.cfg, words, chunks, PARTS, DUR, title="This Week in Cyber",
                                       week_label="Week 47", handle="@cyber", overlays=overlays)

    def _golden_argv(self):
        wav = self.root / "a.wav"
        wav.write_bytes(b"x")
        ass = self.root / "w" / "a.ass"
        ass.parent.mkdir()
        ass.write_text("x")
        clips = [self.root / f"c{i}.mp4" for i in range(3)]
        for c in clips:
            c.write_bytes(b"x")
        part_clips = [[clips[0], clips[1]], [], [clips[2]], [], [clips[0]]]
        durs = [p["end"] - p["start"] for p in PARTS]
        fake = FakeRun()
        with mock.patch("subprocess.run", fake):
            self.rr.render_recap(self.cfg, wav, ass, part_clips, durs, self.root / "out.mp4",
                                 prefix=self.root / "draft_1_recap")
        calls = [{"argv": [_normalize(a, self.roots) for a in c],
                  "cwd": None if kw.get("cwd") is None else _normalize(kw["cwd"], self.roots)}
                 for c, kw in fake.calls if c[0] == "ffmpeg"]
        return json.dumps(calls, indent=2, ensure_ascii=False) + "\n"

    def test_build_recap_ass_golden(self):
        got = self._golden_ass()
        path = FIX / "build_recap_ass.ass"
        if os.environ.get("REGEN_GOLDEN") == "1":
            FIX.mkdir(parents=True, exist_ok=True)
            path.write_text(got, encoding="utf-8", newline="")
        self.assertEqual(got, path.read_bytes().decode("utf-8"))

    def test_render_recap_argv_golden(self):
        got = self._golden_argv()
        path = FIX / "render_recap_argv.json"
        if os.environ.get("REGEN_GOLDEN") == "1":
            FIX.mkdir(parents=True, exist_ok=True)
            path.write_text(got, encoding="utf-8", newline="")
        self.assertEqual(got, path.read_bytes().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
