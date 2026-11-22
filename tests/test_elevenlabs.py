import os
import tempfile
import unittest
from array import array
from pathlib import Path
from unittest import mock

SCRIPT = "Big bug found today.\n\nPatch your servers now.\n\nFollow for daily cyber news."


def pcm_bytes(seconds=1.0):
    a = array("h", [6000 if (i // 40) % 2 else -6000 for i in range(int(24000 * seconds))])
    return a.tobytes()


class ElevenBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("APP_DIR")
        os.environ["APP_DIR"] = self._tmp.name
        from cybershorts import config, tts
        self.tts = tts
        self.cfg = config.load()
        self.cfg.elevenlabs_api_key = "KEY"
        self.cfg.raw["voice"]["engine"] = "elevenlabs"
        self.cfg.raw["voice"]["polish"] = False
        self.out = Path(self._tmp.name) / "draft_1.wav"

    def tearDown(self):
        if self._old is None:
            os.environ.pop("APP_DIR", None)
        else:
            os.environ["APP_DIR"] = self._old
        self._tmp.cleanup()


class ElevenLabsTests(ElevenBase):
    def test_success_with_context_and_spans(self):
        calls = []

        def fake_post(url, params=None, headers=None, json=None, timeout=None):
            calls.append(json)
            r = mock.Mock(status_code=200, content=pcm_bytes(1.0))
            return r

        with mock.patch.object(self.tts.requests, "post", side_effect=fake_post):
            self.tts.synthesize(self.cfg, SCRIPT, self.out)
        self.assertEqual(len(calls), 3)
        self.assertNotIn("previous_text", calls[0])
        self.assertIn("Big bug", calls[1]["previous_text"])
        self.assertIn("Follow", calls[1]["next_text"])
        self.assertEqual(calls[0]["model_id"], "eleven_multilingual_v2")
        self.assertEqual(len(self.tts.load_spans(self.out)), 3)

    def test_out_of_credits_falls_back_to_kokoro(self):
        r = mock.Mock(status_code=401, content=b"")
        r.json.return_value = {"detail": {"status": "quota_exceeded"}}

        class FakeKokoro:
            def __init__(self, cfg):
                pass

            def say(self, text):
                a = array("h")
                a.frombytes(pcm_bytes(0.8))
                return a, 24000

        with mock.patch.object(self.tts.requests, "post", return_value=r), \
             mock.patch.object(self.tts, "_Kokoro", FakeKokoro), \
             mock.patch.object(self.tts, "ensure_voice"):
            out = self.tts.synthesize(self.cfg, SCRIPT, self.out)
        self.assertTrue(out.exists())

    def test_no_key_without_fallback_raises(self):
        self.cfg.elevenlabs_api_key = ""
        self.cfg.raw["voice"]["fallback_engine"] = ""
        with self.assertRaises(RuntimeError):
            self.tts.synthesize(self.cfg, SCRIPT, self.out)

    def test_retries_server_errors(self):
        bad = mock.Mock(status_code=503, content=b"")
        bad.json.return_value = {"detail": "busy"}
        good = mock.Mock(status_code=200, content=pcm_bytes(0.5))
        with mock.patch.object(self.tts.requests, "post", side_effect=[bad, good, good, good]), \
             mock.patch.object(self.tts.time, "sleep"):
            self.tts.synthesize(self.cfg, SCRIPT, self.out)
        self.assertTrue(self.out.exists())
