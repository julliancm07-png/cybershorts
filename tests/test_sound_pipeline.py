"""Shorts integration of the sound kit: render_draft / publish / swap_hook wiring (all offline)."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_sound_regression import BASE_DATA, CASES, _fake_run, _words

KIT = ("hook", "whoosh", "breaking", "card", "outro")


def _setup(tmp, *, kit=True, sfx_on_tiktok=True, extra_data=None):
    from cybershorts import config, db

    cfg = config.load()
    cfg.telegram_bot_token, cfg.telegram_chat_id = "t", "1"
    cfg.raw.setdefault("sound", {})["enabled"] = True
    cfg.raw["sound"]["sfx_on_tiktok"] = sfx_on_tiktok
    d = cfg.path("brand", "sound")
    d.mkdir(parents=True, exist_ok=True)
    if kit:
        for n in KIT:
            (d / f"{n}.mp3").write_bytes(b"fake")
    wav = cfg.path("work") / "draft_audio.wav"
    wav.write_bytes(b"fake")
    conn = db.connect(cfg)
    db.init(conn)
    did = db.create_draft(conn, "http://x", "awaiting_review",
                          json.dumps({**BASE_DATA, **CASES["regular_beats"]["data"], **(extra_data or {})}),
                          audio_path=str(wav))
    conn.close()
    return cfg, did, wav


class SoundPipeline(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old = os.environ.get("APP_DIR")
        os.environ["APP_DIR"] = self._tmp.name
        self.addCleanup(lambda: os.environ.pop("APP_DIR", None) if self._old is None
                        else os.environ.__setitem__("APP_DIR", self._old))

    def _render(self, cfg, did, *, short_sfx=None):
        from cybershorts import pipeline, render

        tg = mock.Mock()
        tg.send_video.return_value = {"result": {"message_id": 7}}
        calls: list = []
        renders: list = []
        real_render = render.render

        def spy(cfg_, audio, *a, **k):
            renders.append(Path(audio).name)
            return real_render(cfg_, audio, *a, **k)

        patches = [mock.patch("subprocess.run", side_effect=_fake_run(calls)),
                   mock.patch("cybershorts.captions.transcribe_words", return_value=_words()),
                   mock.patch("cybershorts.visuals.fetch_broll_set", return_value=[]),
                   mock.patch("cybershorts.render.render", side_effect=spy),
                   mock.patch("cybershorts.pipeline._telegram", return_value=tg)]
        if short_sfx is not None:
            patches.append(mock.patch("cybershorts.sound.short_sfx", **short_sfx))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        out = pipeline.render_draft(cfg, did)
        return out, renders, calls, tg

    def test_render_gets_sfx_wav_and_cleans_up(self):
        cfg, did, _ = _setup(self._tmp.name)
        out, renders, calls, tg = self._render(cfg, did)
        self.assertEqual(renders, [f"draft_{did}_sfx.wav"])
        self.assertFalse((cfg.path("work") / f"draft_{did}_sfx.wav").exists())
        self.assertFalse((cfg.path("work") / f"draft_{did}_tiktok.mp4").exists())
        caption = tg.send_video.call_args[0][1]
        self.assertIn("🔊 SFX", caption)
        self.assertLessEqual(len(caption), 1024)

    def test_sfx_off_tiktok_makes_clean_version_and_publish_routes_it(self):
        from cybershorts import pipeline, sound

        cfg, did, wav = _setup(self._tmp.name, sfx_on_tiktok=False)
        out, renders, calls, tg = self._render(cfg, did)
        tv = sound.tiktok_version(cfg, did)
        self.assertTrue(tv.exists())
        remux = [c for c in calls if str(tv) == c[-1]]
        self.assertEqual(len(remux), 1)
        self.assertIn(str(wav), remux[0])
        uploads = {}
        with mock.patch("cybershorts.youtube.upload", side_effect=lambda c, v, *a, **k: uploads.update(yt=Path(v)) or "vid"), \
             mock.patch("cybershorts.tiktok.upload_draft", side_effect=lambda c, v: uploads.update(tt=Path(v)) or "pid"):
            pipeline.publish(cfg, did, {"yt", "tt"})
        self.assertEqual(uploads["yt"], out)
        self.assertEqual(uploads["tt"], tv)

    def test_stale_tiktok_version_removed_when_sfx_on_tiktok(self):
        from cybershorts import sound

        cfg, did, _ = _setup(self._tmp.name, sfx_on_tiktok=True)
        tv = sound.tiktok_version(cfg, did)
        tv.write_bytes(b"old")
        self._render(cfg, did)
        self.assertFalse(tv.exists())

    def test_sfx_failures_still_render_plain(self):
        for kw in ({"side_effect": RuntimeError("boom")}, {"return_value": None}):
            with self.subTest(kw=kw):
                cfg, did, _ = _setup(self._tmp.name, sfx_on_tiktok=False)
                out, renders, calls, tg = self._render(cfg, did, short_sfx=kw)
                self.assertEqual(renders, ["draft_audio.wav"])
                self.assertTrue(out.exists())
                from cybershorts import sound
                self.assertFalse(sound.tiktok_version(cfg, did).exists())
                self.assertNotIn("SFX", tg.send_video.call_args[0][1])
                mock.patch.stopall()

    def test_caption_note_skipped_when_full(self):
        cfg, did, _ = _setup(self._tmp.name)
        with mock.patch("cybershorts.pipeline._telegram_caption", return_value="x" * 1023):
            out, renders, calls, tg = self._render(cfg, did)
        self.assertEqual(len(tg.send_video.call_args[0][1]), 1023)

    def test_swap_hook_backs_up_tiktok_version(self):
        import inspect

        from cybershorts import pipeline
        self.assertIn("tiktok_version", inspect.getsource(pipeline.swap_hook))


if __name__ == "__main__":
    unittest.main()
