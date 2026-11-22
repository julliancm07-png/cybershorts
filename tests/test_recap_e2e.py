"""End-to-end recap: real recap_captions + recap_render (ffmpeg); only external boundaries mocked."""
import copy
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cybershorts import config, db, recap, recap_publish

FIX = Path(__file__).parent / "fixtures" / "recap_llm.json"
WEEK = "2026-W47"
AUDIO_SECONDS = 45


def _probe(path, entries):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", entries,
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True).stdout
    return out.strip()


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg not installed")
class RecapE2E(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = config.Config(Path(self._tmp.name), copy.deepcopy(config.DEFAULTS), {})
        self.cfg.raw["recap"]["target_words"] = [150, 220]
        self.cfg.raw["recap"]["fetch_article"] = False
        self.cfg.raw["llm"]["fact_check"] = False
        self.cfg.raw["music"] = {**(self.cfg.raw.get("music") or {}), "enabled": False}
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self._tmp.cleanup)
        self.work = self.cfg.path("work")
        self.sent = {}

    def _stories(self, cfg, conn, now=None):
        return [{"draft_id": i, "youtube_id": f"yt{i}", "url": f"https://ex.com/{i}", "title": f"Story {i}",
                 "summary": "S", "script": "x", "description": "d", "breaking": False, "series": "",
                 "data": {}, "views": 0} for i in range(1, 4)]

    def _voice(self, cfg, draft_id, data, wav=None):
        out = self.work / f"draft_{draft_id}.wav"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                        f"sine=frequency=220:duration={AUDIO_SECONDS}", "-ar", "22050", "-ac", "1", str(out)],
                       check=True)
        self.voice_text = data.get("voice_script") or data["script"]
        return out

    def _words(self, cfg, wav, prompt=""):
        toks = self.script_words
        step = AUDIO_SECONDS / len(toks)
        return [(i * step, i * step + step * 0.9, t) for i, t in enumerate(toks)]

    def test_run_then_publish(self):
        tg = mock.Mock()
        tg.send_photo.return_value = {"message_id": 1}
        tg.send_video.return_value = {"message_id": 4242}
        tg.send_message.return_value = {"message_id": 4243}
        raw = json.loads(FIX.read_text())

        real_chat = mock.Mock(return_value=json.dumps(raw))

        def capture_words(cfg, wav, script):
            self.script_words = script.split()
            return self._words(cfg, wav, script)

        with mock.patch("cybershorts.recap.week_key", return_value=WEEK), \
                mock.patch("cybershorts.recap.select_stories", side_effect=self._stories), \
                mock.patch("cybershorts.writer.chat", real_chat), \
                mock.patch("cybershorts.pipeline._voice", side_effect=self._voice), \
                mock.patch("cybershorts.tts.load_spans", return_value=None), \
                mock.patch("cybershorts.captions.transcribe_words", side_effect=capture_words), \
                mock.patch("cybershorts.captions.align_to_script", side_effect=lambda w, s: w), \
                mock.patch("cybershorts.visuals.fetch_broll_set", return_value=[]), \
                mock.patch("cybershorts.pipeline._telegram", return_value=tg), \
                mock.patch("cybershorts.pipeline._notify"):
            draft_id = recap.run(self.cfg)

        self.assertIsNotNone(draft_id)
        conn = db.connect(self.cfg)
        try:
            row = db.get_draft(conn, draft_id)
        finally:
            conn.close()
        self.assertEqual(row["kind"], "recap")
        self.assertEqual(row["status"], "rendered", row["error"])
        self.assertEqual(row["tg_message_id"], 4242)
        data = json.loads(row["data_json"])

        video = Path(row["video_path"])
        self.assertEqual(video, self.cfg.path("videos") / f"draft_{draft_id}.mp4")
        self.assertEqual(_probe(video, "stream=width,height"), "1920,1080")
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                    str(video)], capture_output=True, text=True).stdout.strip())
        self.assertAlmostEqual(dur, data["duration"], delta=1.0)

        preview = self.work / f"draft_{draft_id}_preview.mp4"
        thumb = self.work / f"draft_{draft_id}_thumb.jpg"
        self.assertTrue(preview.exists() and preview.stat().st_size > 0)
        self.assertEqual(_probe(preview, "stream=width,height"), "854,480")
        self.assertTrue(thumb.exists())
        self.assertEqual(_probe(thumb, "stream=width,height"), "1280,720")

        # review message used the real preview + thumbnail
        tg.send_photo.assert_called_once()
        self.assertEqual(Path(tg.send_video.call_args[0][0]), preview)

        # description has valid chapters, or a warning explains why not
        desc = data["description"]
        if data.get("chapters"):
            self.assertIn("00:00", desc)
            self.assertGreaterEqual(len(data["chapters"]), 3)
            self.assertNotIn("chapters_warning", data)
        else:
            self.assertTrue(data.get("chapters_warning"))
            self.assertNotIn("00:00 ", desc)
        self.assertNotIn("#Shorts", data["youtube_title"])
        self.assertTrue(data["youtube_title"])

        # publish: YouTube mocked, TikTok must never be touched
        with mock.patch("cybershorts.youtube.upload", return_value="RECAPYT") as up, \
                mock.patch("cybershorts.youtube.set_thumbnail", return_value=(True, "ok")) as st, \
                mock.patch("cybershorts.youtube.post_comment", return_value=("cid", "ok")), \
                mock.patch("cybershorts.tiktok.upload_draft") as tt, \
                mock.patch("cybershorts.pipeline._telegram", return_value=tg):
            out = recap_publish.publish(self.cfg, draft_id)
        self.assertIn("published", out)
        up.assert_called_once()
        self.assertEqual(Path(up.call_args[0][1]), video)
        self.assertEqual(Path(st.call_args[0][2]), thumb)
        tt.assert_not_called()
        conn = db.connect(self.cfg)
        try:
            row = db.get_draft(conn, draft_id)
        finally:
            conn.close()
        self.assertEqual(row["status"], "published")
        self.assertEqual(row["youtube_id"], "RECAPYT")


if __name__ == "__main__":
    unittest.main()
