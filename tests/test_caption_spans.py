import os
import shutil
import subprocess
import tempfile
import unittest
from array import array
from pathlib import Path
from unittest import mock

HAVE_FFMPEG = bool(shutil.which("ffmpeg"))
SCRIPT = "Big bug found today.\n\nPatch your servers now.\n\nFollow for daily cyber news."


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("APP_DIR")
        os.environ["APP_DIR"] = self._tmp.name
        from cybershorts import captions, config, tts
        self.captions, self.tts = captions, tts
        self.cfg = config.load()

    def tearDown(self):
        if self._old is None:
            os.environ.pop("APP_DIR", None)
        else:
            os.environ["APP_DIR"] = self._old
        self._tmp.cleanup()


class SpreadTests(Base):
    def test_spread_inside_span(self):
        w = self.captions.spread_words("Patch your servers now.", 2.0, 4.0)
        self.assertEqual([x[2] for x in w], ["Patch", "your", "servers", "now."])
        self.assertGreaterEqual(w[0][0], 2.0)
        self.assertLessEqual(w[-1][1], 4.0)
        self.assertEqual([x[0] for x in w], sorted(x[0] for x in w))

    def test_mismatched_spans_returns_none(self):
        self.assertIsNone(self.captions.transcribe_by_spans(self.cfg, "x.wav", [[0, 1]], SCRIPT))


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class SpanTranscribeTests(Base):
    def _wav(self):
        wav = Path(self._tmp.name) / "v.wav"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=220:d=9",
                        "-ar", "24000", str(wav)], check=True)
        return wav

    def test_per_beat_offsets_and_fallback(self):
        wav = self._wav()
        spans = [[0.2, 2.0], [3.0, 5.0], [6.0, 8.0]]

        def fake(cfg, clip, prompt=""):
            if prompt.startswith("Big"):   # beat 1: good transcription, clip-relative times
                return [(0.15, 0.4, "Big"), (0.45, 0.7, "bug"), (0.75, 1.0, "found"), (1.05, 1.5, "today.")]
            if prompt.startswith("Patch"):  # beat 2: Whisper garbage -> even spread inside [3,5]
                return [(0.1, 0.3, "uh")]
            return [(0.15, 0.4, "Follow"), (0.4, 0.6, "for"), (0.6, 0.9, "daily"),
                    (0.9, 1.2, "cyber"), (1.2, 1.6, "news.")]

        with mock.patch.object(self.captions, "transcribe_words", side_effect=fake):
            words = self.captions.transcribe_by_spans(self.cfg, wav, spans, SCRIPT)
        self.assertEqual(" ".join(w[2] for w in words), " ".join(SCRIPT.split()))
        self.assertAlmostEqual(words[0][0], 0.2, delta=0.06)       # 0.15 + clip offset 0.05
        beat2 = words[4:8]
        self.assertTrue(all(3.0 <= w[0] <= 5.0 for w in beat2))
        self.assertGreaterEqual(words[8][0], 6.0)
        starts = [w[0] for w in words]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(self.captions.beat_times(words, SCRIPT, 9.0)[1:], [words[4][0], words[8][0]])


class TtsSpansTests(Base):
    def test_synthesize_writes_spans(self):
        class FakeEngine:
            def __init__(self, cfg):
                pass

            def say(self, text):
                n = 24000 * len(text.split()) // 4
                return array("h", [8000 if (i // 50) % 2 else -8000 for i in range(n)]), 24000

        self.cfg.raw["voice"]["polish"] = False
        out = Path(self._tmp.name) / "draft_1.wav"
        with mock.patch.object(self.tts, "_Kokoro", FakeEngine), mock.patch.object(self.tts, "ensure_voice"):
            self.tts.synthesize(self.cfg, SCRIPT, out)
        spans = self.tts.load_spans(out)
        self.assertEqual(len(spans), 3)
        for a, b in spans:
            self.assertLess(a, b)
        self.assertTrue(all(spans[i][1] < spans[i + 1][0] for i in range(2)))
        self.assertAlmostEqual(spans[0][0], 0.0, delta=0.05)
