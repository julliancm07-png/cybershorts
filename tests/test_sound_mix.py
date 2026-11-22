"""Sound mixing: overlay timing parity with build_ass, event rules, argv, failure handling."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
CARD = {"cve_id": "CVE-2025-12345", "cvss": 9.8, "severity": "Critical", "products": ["Acme VPN"],
        "exploited": True}
ALL = ["hook", "whoosh", "breaking", "card", "outro"]


def _t(s):
    h, m, rest = s.split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("APP_DIR")
        os.environ["APP_DIR"] = self._tmp.name
        from cybershorts import captions, sound
        from cybershorts.config import load
        self.captions, self.sound = captions, sound
        self.cfg = load()
        self.kit = Path(self._tmp.name) / "kit"
        self.kit.mkdir()
        self.cfg.raw.setdefault("sound", {})["folder"] = str(self.kit)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("APP_DIR", None)
        else:
            os.environ["APP_DIR"] = self._old
        self._tmp.cleanup()

    def make_kit(self, names=ALL):
        for n in names:
            (self.kit / f"{n}.mp3").write_bytes(b"x" * 10)


class OverlayTimes(Base):
    CASES = [
        dict(title="Big flaw", beats=[0, 2, 4, 6], badge=None, card=None, dur=7.0),
        dict(title="Big flaw", beats=[0, 2, 4, 6], badge="BREAKING", card=CARD, dur=7.0),
        dict(title="", beats=None, badge=None, card=CARD, dur=7.0),
        dict(title="T", beats=[0, 1.5], badge=None, card=CARD, dur=3.0),
        dict(title="T", beats=[0, 2, 3, 6.5], badge="NEW", card=CARD, dur=7.0),
        dict(title="T", beats=[0, 2, 6.9], badge=None, card=None, dur=7.0),
    ]

    def test_matches_build_ass(self):
        for c in self.CASES:
            with self.subTest(c=c):
                ot = self.captions.overlay_times(self.cfg, c["dur"], title=c["title"], beats=c["beats"],
                                                 badge=c["badge"], card=c["card"])
                ass = self.captions.build_ass([(0, 1, "hi")], self.cfg, c["title"], "@me", c["dur"],
                                              beats=c["beats"], badge=c["badge"], card=c["card"])
                hook = [l for l in ass.splitlines() if ",Hook," in l]
                self.assertEqual(bool(hook), ot["hook_end"] > 0)
                if hook:
                    self.assertAlmostEqual(_t(hook[0].split(",")[2]), ot["hook_end"], places=2)
                badge = [l for l in ass.splitlines() if l.startswith("Dialogue: 4,0:00:00.00") and "\\p1" in l
                         and "\\move" not in l]
                self.assertEqual(bool(badge), ot["badge_end"] is not None)
                if badge:
                    self.assertAlmostEqual(_t(badge[0].split(",")[2]), ot["badge_end"], places=2)
                moves = [l for l in ass.splitlines() if "\\move" in l]
                self.assertEqual(bool(moves), ot["card"] is not None)
                if moves:
                    self.assertAlmostEqual(_t(moves[0].split(",")[1]), ot["card"][0], places=2)
                    self.assertAlmostEqual(_t(moves[0].split(",")[2]), ot["card"][1], places=2)
                fol = [l for l in ass.splitlines() if "FOLLOW" in l]
                self.assertEqual(bool(fol), ot["end_card"] is not None)
                if fol:
                    self.assertAlmostEqual(_t(fol[0].split(",")[1]), ot["end_card"], places=2)

    def test_values(self):
        ot = self.captions.overlay_times(self.cfg, 7.0, title="T", beats=[0, 2, 4, 6], badge="B", card=CARD)
        self.assertEqual(ot["hook_end"], 1.6)
        self.assertEqual(ot["badge_end"], 1.6)
        self.assertEqual(ot["card"], (2.0, 4.0))
        self.assertEqual(ot["end_card"], 6.0)


class ShortEvents(Base):
    def ev(self, beats=(0, 2, 4, 6), dur=7.0, **kw):
        kw.setdefault("title", "T")
        return self.sound.short_events(self.cfg, list(beats) if beats else beats, dur, **kw)

    def test_hook_whoosh_outro(self):
        self.make_kit()
        self.assertEqual(self.ev(), [(0.0, "hook"), (1.88, "whoosh"), (3.88, "whoosh"), (6.0, "outro")])

    def test_breaking_replaces_hook(self):
        self.make_kit()
        names = [n for _t, n in self.ev(badge="BREAKING")]
        self.assertIn("breaking", names)
        self.assertNotIn("hook", names)

    def test_card_beats_whoosh_within_gap(self):
        self.make_kit()
        e = self.ev(card=CARD)
        self.assertIn((2.0, "card"), e)
        self.assertNotIn((1.88, "whoosh"), e)

    def test_outro_beats_whoosh(self):
        self.make_kit()
        e = self.ev(beats=(0, 2, 6.0), dur=7.0)
        self.assertEqual([n for _t, n in e if _t > 5], ["outro"])

    def test_min_gap_config(self):
        self.make_kit()
        self.cfg.raw["sound"]["min_gap"] = 0.05
        e = self.ev(card=CARD)
        self.assertIn((1.88, "whoosh"), e)
        self.assertIn((2.0, "card"), e)

    def test_whoosh_lead(self):
        self.make_kit(["whoosh"])
        self.cfg.raw["sound"]["whoosh_lead"] = 0.2
        self.assertEqual(self.ev(), [(1.8, "whoosh"), (3.8, "whoosh"), (5.8, "whoosh")])

    def test_missing_files_dropped(self):
        self.make_kit(["whoosh", "outro"])
        self.assertEqual([n for _t, n in self.ev()], ["whoosh", "whoosh", "outro"])
        (self.kit / "whoosh.mp3").write_bytes(b"")  # empty file == missing
        self.assertEqual([n for _t, n in self.ev()], ["outro"])

    def test_missing_high_priority_does_not_block_lower(self):
        self.make_kit(["whoosh"])
        self.assertIn((1.88, "whoosh"), self.ev(card=CARD))

    def test_no_beats(self):
        self.make_kit()
        self.assertEqual(self.ev(beats=None), [(0.0, "hook")])
        self.assertEqual([n for _t, n in self.ev(beats=None, card=CARD)], ["hook", "card"])

    def test_no_title_no_hook(self):
        self.make_kit()
        self.assertEqual(self.ev(beats=None, title=""), [])


class SfxCmd(Base):
    def test_cmd(self):
        a, b = self.kit / "a.mp3", self.kit / "b.mp3"
        evs = [(0.0, a, -10.0), (1.8774, b, -16.0), (3.0, b, -16.0)]
        cmd = self.sound.build_sfx_cmd(self.cfg, "v.wav", evs, "o.wav")
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("normalize=0", graph)
        self.assertIn("level=disabled", graph)
        self.assertIn("amix=inputs=4:duration=first", graph)
        self.assertEqual(re.findall(r"adelay=(\S+?):all=1", graph), ["0", "1877", "3000"])
        self.assertIn("asplit=2", graph)
        self.assertEqual(cmd.count("-i"), 3)  # voice + 2 distinct files
        self.assertIn("volume=-10dB", graph)
        self.assertEqual(cmd[-1], "o.wav")
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "pcm_s16le")
        self.assertEqual(cmd[cmd.index("-ac") + 1], "1")
        self.assertEqual(cmd[cmd.index("-ar") + 1], "44100")


class ShortSfx(Base):
    def call(self, **kw):
        kw.setdefault("beats", [0, 2, 4, 6])
        return self.sound.short_sfx(self.cfg, 7, "v.wav", duration=7.0, title="T", **kw)

    def test_disabled_no_subprocess(self):
        self.make_kit()
        self.cfg.raw["sound"]["enabled"] = False
        with mock.patch("subprocess.run") as run:
            self.assertIsNone(self.call())
        run.assert_not_called()

    def test_empty_kit_no_subprocess(self):
        with mock.patch("subprocess.run") as run:
            self.assertIsNone(self.call())
        run.assert_not_called()

    def test_failure_returns_none(self):
        self.make_kit()
        bad = subprocess.CompletedProcess([], 1, "", "boom")
        with mock.patch("subprocess.run", return_value=bad):
            self.assertIsNone(self.call())
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("ffmpeg")):
            self.assertIsNone(self.call())
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ffmpeg", 1)):
            self.assertIsNone(self.call())

    def test_success_path(self):
        self.make_kit()
        ok = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch("subprocess.run", return_value=ok) as run:
            out = self.call(overlay={"badge": "BREAKING"})
        self.assertEqual(out.name, "draft_7_sfx.wav")
        cmd = run.call_args[0][0]
        self.assertIn("breaking.mp3", " ".join(cmd))
        self.assertNotIn("hook.mp3", " ".join(cmd))

    def test_paths(self):
        self.assertEqual(self.sound.tiktok_version(self.cfg, 3).name, "draft_3_tiktok.mp4")


PARTS = [{"kind": "intro", "start": 0.0, "end": 8.0}, {"kind": "story", "start": 8.0, "end": 30.0},
         {"kind": "story", "start": 30.0, "end": 52.0}, {"kind": "outro", "start": 52.0, "end": 60.0}]


class RecapEvents(Base):
    def test_order_and_times(self):
        self.make_kit()
        e = self.sound.recap_events(self.cfg, PARTS, 60.0)
        self.assertEqual(e, [(7.88, "whoosh"), (29.88, "whoosh"), (52.0, "outro")])

    def test_sting_len_pulls_outro_later(self):
        self.make_kit()
        e = self.sound.recap_events(self.cfg, PARTS, 60.0, sting_len=2.0)
        self.assertEqual(e[-1], (58.0, "outro"))
        e = self.sound.recap_events(self.cfg, PARTS, 60.0, sting_len=20.0)
        self.assertEqual(e[-1], (52.0, "outro"))

    def test_missing_files(self):
        self.make_kit(["outro"])
        self.assertEqual(self.sound.recap_events(self.cfg, PARTS, 60.0), [(52.0, "outro")])

    def test_recap_audio_disabled_or_empty(self):
        with mock.patch("subprocess.run") as run:
            self.assertEqual(self.sound.recap_audio(self.cfg, "v.wav", PARTS, "o.wav"), (None, False))
            self.cfg.raw["sound"]["enabled"] = False
            self.assertEqual(self.sound.recap_audio(self.cfg, "v.wav", PARTS, "o.wav"), (None, False))
        run.assert_not_called()


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class RealFfmpeg(Base):
    def ff(self, *args):
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True, timeout=60)

    def dur(self, p):
        return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                                     "csv=p=0", str(p)], capture_output=True, text=True).stdout)

    def test_short_sfx_duration(self):
        voice = Path(self._tmp.name) / "v.wav"
        self.ff("-f", "lavfi", "-i", "sine=f=220:d=4", str(voice))
        self.ff("-f", "lavfi", "-i", "sine=f=1000:d=0.4", str(self.kit / "hook.mp3"))
        out = self.sound.short_sfx(self.cfg, 1, voice, beats=[0.0], duration=4.0, title="T")
        self.assertIsNotNone(out)
        self.assertAlmostEqual(self.dur(out), 4.0, delta=0.02)


if __name__ == "__main__":
    unittest.main()
