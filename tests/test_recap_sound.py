"""Recap step 9-10 sound wiring: recap_audio mix -> render_recap audio, add_music only without a bed."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cybershorts import config, recap


class RecapSound(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(mock.patch.stopall)
        self.cfg = config.Config(Path(self._tmp.name), copy.deepcopy(config.DEFAULTS), {})
        self.cfg.raw["recap"]["layout"] = "classic"
        self.work = self.cfg.path("work")
        self.work.mkdir(parents=True, exist_ok=True)
        self.wav = self.work / "draft_7.wav"
        self.wav.write_bytes(b"w")
        self.mix = self.work / "draft_7_recap_mix.wav"
        self.render = mock.Mock()
        self.add_music = mock.Mock(return_value=(None, None))
        self.recap_audio = mock.Mock(return_value=(None, False))

    def _produce(self):
        stories = [{"draft_id": 1, "data": {}}]
        data = {"script": "one two three four five six", "para_counts": [1, 1, 1],
                "segments": [{"chapter": "C", "narration": "n"}], "on_screen_title": "T",
                "thumbnail_text": "X"}
        words = [(i, i + 0.9, t) for i, t in enumerate(data["script"].split())]
        P = mock.patch
        with P("cybershorts.pipeline._voice", return_value=self.wav), \
                P("cybershorts.tts.load_spans", return_value=None), \
                P("cybershorts.captions.transcribe_words", return_value=words), \
                P("cybershorts.captions.align_to_script", side_effect=lambda w, s: w), \
                P("cybershorts.render.probe_duration", return_value=6.0), \
                P("cybershorts.visuals.fetch_broll_set", return_value=[]), \
                P("cybershorts.recap_captions.write_recap_ass", return_value=self.work / "a.ass"), \
                P("cybershorts.recap_captions.build_thumbnail_ass", return_value=""), \
                P("cybershorts.recap_render.render_recap", self.render), \
                P("cybershorts.recap_render.extract_still", side_effect=RuntimeError("x")), \
                P("cybershorts.recap_render.render_preview"), \
                P("cybershorts.sound.recap_audio", self.recap_audio), \
                P("cybershorts.music.add_music", self.add_music), \
                P("cybershorts.recap_publish.send_review", return_value=None), \
                P("cybershorts.recap.build_description", return_value="d"), \
                P("cybershorts.db.connect"), P("cybershorts.db.update_draft"):
            recap._produce(self.cfg, 7, "2026-W47", stories, data, [])

    def test_no_kit_unchanged(self):
        self._produce()
        self.assertEqual(self.render.call_args[0][1], self.wav)
        self.add_music.assert_called_once()
        self.assertEqual(self.recap_audio.call_args[0][1], self.wav)
        self.assertEqual(Path(self.recap_audio.call_args[0][3]), self.mix)

    def test_bed_skips_add_music(self):
        def fake(cfg, wav, parts, out):
            Path(out).write_bytes(b"m")
            return Path(out), True
        self.recap_audio.side_effect = fake
        self._produce()
        self.assertEqual(Path(self.render.call_args[0][1]), self.mix)
        self.add_music.assert_not_called()
        self.assertFalse(self.mix.exists())
        parts = self.recap_audio.call_args[0][2]
        self.assertEqual([p["kind"] for p in parts], ["intro", "story", "outro"])

    def test_no_bed_still_adds_music(self):
        self.recap_audio.return_value = (self.mix, False)
        self._produce()
        self.assertEqual(Path(self.render.call_args[0][1]), self.mix)
        self.add_music.assert_called_once()

    def test_recap_audio_raises_falls_back(self):
        self.recap_audio.side_effect = RuntimeError("boom")
        self._produce()
        self.assertEqual(self.render.call_args[0][1], self.wav)
        self.add_music.assert_called_once()


if __name__ == "__main__":
    unittest.main()
