import json
import os
import tempfile
import unittest
from array import array
from pathlib import Path
from unittest import mock

VOICE = "[sighs] Big bug found today.\n\n[whispers] Patch your servers now.\n\nFollow for daily cyber news."


def pcm_bytes(seconds=1.0):
    return array("h", [6000 if (i // 40) % 2 else -6000 for i in range(int(24000 * seconds))]).tobytes()


class V3Base(unittest.TestCase):
    model = "eleven_v3"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("APP_DIR")
        os.environ["APP_DIR"] = self._tmp.name
        from cybershorts import config, tts
        self.tts = tts
        self.cfg = config.load()
        self.cfg.elevenlabs_api_key = "KEY"
        v = self.cfg.raw["voice"]
        v["engine"] = "elevenlabs"
        v["polish"] = False
        v["elevenlabs"] = {**v.get("elevenlabs", {}), "model": self.model,
                           "tag_models": ["eleven_v3"], "audio_tags": True}
        self.out = Path(self._tmp.name) / "draft_1.wav"

    def tearDown(self):
        if self._old is None:
            os.environ.pop("APP_DIR", None)
        else:
            os.environ["APP_DIR"] = self._old
        self._tmp.cleanup()

    def run_eleven(self, text=VOICE):
        calls = []

        def fake_post(url, params=None, headers=None, json=None, timeout=None):
            calls.append(json)
            return mock.Mock(status_code=200, content=pcm_bytes(0.8))

        with mock.patch.object(self.tts.requests, "post", side_effect=fake_post):
            self.tts.synthesize(self.cfg, text, self.out)
        return calls


class TagModelTests(V3Base):
    def test_request_has_tags_and_no_stitching(self):
        calls = self.run_eleven()
        self.assertEqual(len(calls), 3)
        self.assertTrue(calls[0]["text"].startswith("[sighs] "))
        self.assertIn("[whispers]", calls[1]["text"])
        self.assertEqual(calls[0]["model_id"], "eleven_v3")
        for c in calls:
            self.assertNotIn("previous_text", c)
            self.assertNotIn("next_text", c)

    def test_span_count_equals_clean_paragraphs(self):
        from cybershorts import tags
        self.run_eleven()
        spans = self.tts.load_spans(self.out)
        clean = [p for p in tags.strip_tags(VOICE).split("\n") if p.strip()]
        self.assertEqual(len(spans), len(clean))

    def test_tag_only_paragraph_does_not_add_span(self):
        calls = self.run_eleven("One two.\n\n[serious]\n\nThree four.")
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[1]["text"].startswith("[serious] "))
        self.assertEqual(len(self.tts.load_spans(self.out)), 2)

    def test_tags_survive_cleaning_and_pronunciations(self):
        self.cfg.raw["voice"]["pronunciations"] = {"sighs": "BAD", "CISA": "Sisa"}
        calls = self.run_eleven("[Sighs] CISA says 50% of (the) apps/sites are bad.\n\nTwo.")
        txt = calls[0]["text"]
        self.assertTrue(txt.startswith("[sighs] Sisa"), txt)
        self.assertIn("50 percent", txt)
        self.assertNotIn("BAD", txt)

    def test_stability_snaps(self):
        for given, want in ((0.45, 0.5), (0.1, 0.0), (0.9, 1.0), (0.3, 0.5), (0.2, 0.0)):
            self.cfg.raw["voice"]["elevenlabs"]["stability"] = given
            calls = self.run_eleven()
            self.assertEqual(calls[0]["voice_settings"]["stability"], want, given)

    def test_audio_tags_off_strips(self):
        self.cfg.raw["voice"]["elevenlabs"]["audio_tags"] = False
        calls = self.run_eleven()
        for c in calls:
            self.assertNotIn("[", c["text"])
            self.assertNotIn("previous_text", c)  # v3 still has no stitching


class NonTagModelTests(V3Base):
    model = "eleven_multilingual_v2"

    def test_tags_stripped_and_stitching_kept(self):
        calls = self.run_eleven()
        self.assertEqual(len(calls), 3)
        for c in calls:
            self.assertNotIn("[", c["text"])
            self.assertNotIn("sighs", c["text"])
        self.assertIn("previous_text", calls[1])
        self.assertEqual(calls[0]["voice_settings"]["stability"], 0.45)


class FallbackEngineTests(V3Base):
    def test_fallback_engine_never_speaks_tags(self):
        spoken = []

        class FakeKokoro:
            def __init__(self, cfg):
                pass

            def say(self, text):
                spoken.append(text)
                a = array("h")
                a.frombytes(pcm_bytes(0.8))
                return a, 24000

        r = mock.Mock(status_code=401, content=b"")
        r.json.return_value = {"detail": {"status": "quota_exceeded"}}
        with mock.patch.object(self.tts.requests, "post", return_value=r), \
             mock.patch.object(self.tts, "_Kokoro", FakeKokoro), \
             mock.patch.object(self.tts, "ensure_voice"):
            self.tts.synthesize(self.cfg, VOICE, self.out)
        self.assertEqual(len(spoken), 3)
        for s in spoken:
            self.assertNotIn("[", s)
            self.assertNotIn("sighs", s)
            self.assertNotIn("whispers", s)
        self.assertEqual(len(json.loads(self.tts.spans_path(self.out).read_text())), 3)

    def test_kokoro_primary_strips(self):
        spoken = []

        class FakeKokoro:
            def __init__(self, cfg):
                pass

            def say(self, text):
                spoken.append(text)
                a = array("h")
                a.frombytes(pcm_bytes(0.5))
                return a, 24000

        self.cfg.raw["voice"]["engine"] = "kokoro"
        with mock.patch.object(self.tts, "_Kokoro", FakeKokoro), mock.patch.object(self.tts, "ensure_voice"):
            self.tts.synthesize(self.cfg, VOICE, self.out)
        self.assertTrue(all("sighs" not in s and "[" not in s for s in spoken))


if __name__ == "__main__":
    unittest.main()


class ElevenV4SettingsTests(unittest.TestCase):
    def _engine(self, model, **voice):
        import copy
        import tempfile
        from cybershorts import config, tts
        raw = copy.deepcopy(config.DEFAULTS)
        raw["voice"]["engine"] = "elevenlabs"
        raw["voice"]["elevenlabs"]["model"] = model
        raw["voice"]["elevenlabs"].update(voice)
        cfg = config.Config(tempfile.mkdtemp(), raw, {"ELEVENLABS_API_KEY": "k" * 20})
        return cfg, tts._ElevenLabs(cfg)

    def test_v4_is_tag_model_no_style_no_speed_continuous_stability(self):
        from cybershorts import tags
        cfg, e = self._engine("eleven_v4", stability=0.45, speed=1.1)
        self.assertTrue(tags.tags_supported(cfg))
        self.assertFalse(e.context)
        self.assertNotIn("style", e.settings)
        self.assertNotIn("speed", e.settings)
        self.assertAlmostEqual(e.settings["stability"], 0.45)

    def test_v3_still_snaps(self):
        _, e = self._engine("eleven_v3", stability=0.45)
        self.assertEqual(e.settings["stability"], 0.5)
        self.assertIn("style", e.settings)
