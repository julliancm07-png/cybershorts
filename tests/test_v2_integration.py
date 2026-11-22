import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = "Researchers found a flaw today.\n\nAttackers exploit it now.\n\nPatch and stay safe."


def _ff(*args):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True, timeout=120)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg not installed")
class V2IntegrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("APP_DIR")
        os.environ["APP_DIR"] = self._tmp.name

    def tearDown(self):
        if self._old is None:
            os.environ.pop("APP_DIR", None)
        else:
            os.environ["APP_DIR"] = self._old
        self._tmp.cleanup()

    def test_render_draft_end_to_end(self):
        from cybershorts import captions, config, db, pipeline, render

        cfg = config.load()
        cfg.telegram_bot_token, cfg.telegram_chat_id = "t", "1"
        work = cfg.path("work")
        wav = work / "draft_audio.wav"
        _ff("-f", "lavfi", "-i", "sine=frequency=220:duration=6", "-ar", "22050", "-ac", "1", str(wav))
        tokens = [t for t in SCRIPT.split() if captions._norm(t)]
        per = 6.0 / len(tokens)
        fake_words = [(i * per, (i + 1) * per, t) for i, t in enumerate(tokens)]

        def fake_broll(cfg_, n, query, prefix, **kw):
            self.assertEqual(n, 3)
            out = []
            for i, pat in enumerate(("testsrc2", "smptebars", "rgbtestsrc")[:n]):
                p = Path(f"{prefix}_{i}.mp4")
                p.parent.mkdir(parents=True, exist_ok=True)
                _ff("-f", "lavfi", "-i", f"{pat}=size=640x360:rate=30:duration={1 + i}", "-pix_fmt", "yuv420p", str(p))
                out.append(p)
            return out

        data = {"script": SCRIPT, "on_screen_title": "Flaw exploited", "keywords": ["flaw"],
                "youtube_title": "T #Shorts", "description": "d", "hashtags": ["a"],
                "fact_check": {"ok": True, "issues": []}}
        conn = db.connect(cfg)
        db.init(conn)
        did = db.create_draft(conn, "http://x", "awaiting_review", json.dumps(data), audio_path=str(wav))
        conn.close()

        tg = mock.Mock()
        tg.send_video.return_value = {"result": {"message_id": 7}}
        with mock.patch("cybershorts.captions.transcribe_words", return_value=fake_words), \
             mock.patch("cybershorts.visuals.fetch_broll_set", side_effect=fake_broll), \
             mock.patch("cybershorts.pipeline._telegram", return_value=tg):
            out = pipeline.render_draft(cfg, did)

        self.assertTrue(out.exists() and out.stat().st_size > 0)
        info = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
             "-show_entries", "format=duration", "-of", "default=nw=1", str(out)],
            capture_output=True, text=True, check=True, timeout=30).stdout
        kv = dict(line.split("=", 1) for line in info.split())
        self.assertEqual((int(kv["width"]), int(kv["height"])), (1080, 1920))
        self.assertAlmostEqual(float(kv["duration"]), 6.0 + render.PAD_SECONDS, delta=0.4)
        tg.send_video.assert_called_once()
        self.assertIn("Fact-check", tg.send_video.call_args[0][1])
        conn = db.connect(cfg)
        try:
            self.assertEqual(db.get_draft(conn, did)["status"], "rendered")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
