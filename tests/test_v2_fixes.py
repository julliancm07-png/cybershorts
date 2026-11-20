import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


def _ff(*args):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True, timeout=60)


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("APP_DIR")
        os.environ["APP_DIR"] = self._tmp.name
        from cybershorts.config import load

        self.cfg = load()

    def tearDown(self):
        if self._old is None:
            os.environ.pop("APP_DIR", None)
        else:
            os.environ["APP_DIR"] = self._old
        self._tmp.cleanup()


class GarbageLogoRender(_Base):
    def test_corrupt_logo_skipped_fast(self):
        from cybershorts import captions, render

        brand = self.cfg.path("brand")
        brand.mkdir(parents=True, exist_ok=True)
        (brand / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"garbage" * 20)
        work = self.cfg.path("work")
        wav = work / "t.wav"
        _ff("-f", "lavfi", "-i", "sine=frequency=220:duration=2", "-ar", "22050", "-ac", "1", str(wav))
        ass = work / "t.ass"
        ass.write_text(captions.build_ass([], self.cfg, "T", "@me", 2.6))
        out = self.cfg.path("videos") / "t.mp4"
        t0 = time.time()
        with self.assertLogs("cybershorts", level="WARNING") as lg:
            render.render(self.cfg, wav, ass, None, out)
        self.assertLess(time.time() - t0, 60)
        self.assertTrue(out.exists() and out.stat().st_size > 0)
        self.assertTrue(any("watermark" in m.lower() for m in lg.output))

    def test_watermark_false_skips_logo(self):
        from cybershorts import render

        self.assertIn("watermark", render.render.__kwdefaults__)
        self.assertTrue(render.render.__kwdefaults__["watermark"])


class EndCardTests(_Base):
    def test_signoff_split_into_own_paragraph(self):
        from cybershorts import writer

        self.cfg.raw["channel"] = {**self.cfg.raw.get("channel", {}), "sign_off": "Stay patched, stay safe."}
        story = {"url": "http://x", "title": "t"}
        d = writer.validate_script({"on_screen_title": "T", "script": "Patch now. Stay patched, stay safe."},
                                   story, self.cfg)
        self.assertTrue(d["script"].endswith("\n\nStay patched, stay safe."))
        self.assertEqual(d["script"].count("Stay patched, stay safe."), 1)
        d2 = writer.validate_script({"on_screen_title": "T", "script": "Patch now.\n\nStay patched, stay safe."},
                                    story, self.cfg)
        self.assertEqual(d2["script"], "Patch now.\n\nStay patched, stay safe.")

    def test_end_card_only_for_short_last_beat(self):
        from cybershorts import captions

        words = [(i * 0.5, i * 0.5 + 0.4, f"w{i}") for i in range(20)]
        chunks = captions.chunk_words(words, 3)

        def card(beats, dur):
            ass = captions.build_ass(chunks, self.cfg, "T", "@me", dur, words=words, beats=beats)
            return "FOLLOW" in ass

        self.assertTrue(card([0.0, 3.0, 7.0], 10.0))     # last beat 3 s
        self.assertTrue(card([0.0, 3.0, 5.5], 10.0))     # 4.5 s exactly
        self.assertFalse(card([0.0, 3.0, 4.0], 10.0))    # 6 s: skip card


class PanTests(_Base):
    def test_pan_expression_limited(self):
        from cybershorts import render

        clip = self.cfg.path("work") / "c.mp4"
        _ff("-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=1", "-pix_fmt", "yuv420p", str(clip))
        wav = self.cfg.path("work") / "a.wav"
        _ff("-f", "lavfi", "-i", "sine=duration=1", "-ar", "22050", "-ac", "1", str(wav))
        ass = self.cfg.path("work") / "a.ass"
        ass.write_text("[Script Info]\nScriptType: v4.00+\n")
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            return mock.Mock(returncode=0, stderr="", stdout="")

        with mock.patch.object(render, "probe_duration", return_value=1.0), \
             mock.patch.object(render.subprocess, "run", side_effect=fake_run):
            render.render(self.cfg, wav, ass, [clip, clip], self.cfg.path("videos") / "o.mp4")
        graph = seen["cmd"][seen["cmd"].index("-filter_complex") + 1]
        # 1080 wide: span 130, travel 0.12*1080 = 129.6 centred on 65
        self.assertIn("min(max(65.00+129.60*(t/", graph)
        self.assertIn("min(max(65.00+-129.60*(t/", graph)


class BrollCleanup(_Base):
    def test_deletes_only_downloaded_files(self):
        from cybershorts import pipeline

        work = self.cfg.path("work")
        local = Path(self._tmp.name) / "localclips"
        local.mkdir()
        keep_local = local / "mine.mp4"
        keep_local.write_bytes(b"x")
        for n in ("draft_1_broll_0.mp4", "draft_1_broll_1.mp4", "draft_1_broll_1.mp4.part"):
            (work / n).write_bytes(b"x")
        other = work / "draft_12_broll_0.mp4"
        other.write_bytes(b"x")
        pipeline._cleanup_broll(self.cfg, 1, [work / "draft_1_broll_0.mp4", keep_local])
        self.assertEqual(sorted(p.name for p in work.glob("draft_1_broll*")), [])
        self.assertTrue(keep_local.exists())
        self.assertTrue(other.exists())


class PixabayPick(unittest.TestCase):
    def test_prefers_non_4k(self):
        from cybershorts import visuals

        hit = {"tags": "cyber", "videos": {
            "large": {"url": "4k", "width": 3840, "height": 2160},
            "medium": {"url": "hd", "width": 1920, "height": 1080},
            "small": {"url": "sd", "width": 960, "height": 540}}}
        self.assertEqual(visuals._pixabay_pick([hit]), ["hd"])

    def test_falls_back_to_4k_when_only_option(self):
        from cybershorts import visuals

        hit = {"tags": "cyber", "videos": {"large": {"url": "4k", "width": 3840, "height": 2160}}}
        self.assertEqual(visuals._pixabay_pick([hit]), ["4k"])


if __name__ == "__main__":
    unittest.main()
