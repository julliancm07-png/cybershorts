import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("APP_DIR")
        os.environ["APP_DIR"] = self._tmp.name
        from cybershorts import config, db, pipeline

        self.db, self.pipeline = db, pipeline
        self.cfg = config.load()
        self.cfg.telegram_bot_token = ""  # _notify becomes a no-op
        self.cfg.telegram_chat_id = ""
        conn = db.connect(self.cfg)
        db.init(conn)
        self.video = self.cfg.path("videos") / "draft_1.mp4"
        self.video.write_bytes(b"fake")
        data = {"youtube_title": "T #Shorts", "description": "Desc one. Source: http://x",
                "hashtags": ["a", "b"], "on_screen_title": "T", "script": "s"}
        self.id = db.create_draft(conn, "http://x", "rendered", json.dumps(data))
        db.update_draft(conn, self.id, video_path=str(self.video))
        conn.close()

    def tearDown(self):
        if self._old is None:
            os.environ.pop("APP_DIR", None)
        else:
            os.environ["APP_DIR"] = self._old
        self._tmp.cleanup()

    def row(self, i=None):
        conn = self.db.connect(self.cfg)
        try:
            return self.db.get_draft(conn, i or self.id)
        finally:
            conn.close()

    def _publish(self, targets, yt=None, tt=None):
        yt = yt or mock.Mock(return_value="YT123")
        tt = tt or mock.Mock(return_value="TT456")
        with mock.patch("cybershorts.youtube.upload", yt), \
             mock.patch("cybershorts.tiktok.upload_draft", tt):
            out = self.pipeline.publish(self.cfg, self.id, targets)
        return out, yt, tt

    def test_both_ok(self):
        out, yt, tt = self._publish({"yt", "tt"})
        r = self.row()
        self.assertEqual(r["status"], "published")
        self.assertEqual(r["youtube_id"], "YT123")
        self.assertEqual(r["tiktok_publish_id"], "TT456")
        self.assertIn("youtube.com/shorts/YT123", out)

    def test_yt_only(self):
        _, yt, tt = self._publish({"yt"})
        r = self.row()
        self.assertEqual(r["status"], "published")
        self.assertEqual(r["youtube_id"], "YT123")
        self.assertIsNone(r["tiktok_publish_id"])
        tt.assert_not_called()

    def test_partial(self):
        _, _, _ = self._publish({"yt", "tt"}, tt=mock.Mock(side_effect=RuntimeError("boom")))
        r = self.row()
        self.assertEqual(r["status"], "partial")
        self.assertEqual(r["youtube_id"], "YT123")
        self.assertIsNone(r["tiktok_publish_id"])
        self.assertIn("boom", r["error"])

    def test_all_fail(self):
        self._publish({"yt", "tt"}, yt=mock.Mock(side_effect=RuntimeError("e1")),
                      tt=mock.Mock(side_effect=RuntimeError("e2")))
        r = self.row()
        self.assertEqual(r["status"], "failed")
        self.assertIn("e1", r["error"])
        self.assertIn("e2", r["error"])

    def test_yt_only_fail(self):
        self._publish({"yt"}, yt=mock.Mock(side_effect=RuntimeError("e1")))
        self.assertEqual(self.row()["status"], "failed")

    def test_already_published_not_repeated(self):
        self._publish({"yt"})
        out, yt, _ = self._publish({"yt"})
        yt.assert_not_called()
        self.assertIn("already", out)

    def test_missing_video_raises(self):
        self.video.unlink()
        with self.assertRaises(RuntimeError):
            self.pipeline.publish(self.cfg, self.id, {"yt"})

    def test_reject(self):
        self.pipeline.reject(self.cfg, self.id)
        self.assertEqual(self.row()["status"], "rejected")

    def test_mark_manual(self):
        yt = mock.Mock()
        with mock.patch("cybershorts.youtube.upload", yt):
            self.pipeline.mark_manual(self.cfg, self.id)
        r = self.row()
        self.assertEqual(r["status"], "published")
        self.assertIsNone(r["youtube_id"])
        yt.assert_not_called()
        with self.assertRaises(ValueError):
            self.pipeline.mark_manual(self.cfg, 999)

    def test_cleanup_old_deleted_recent_kept(self):
        conn = self.db.connect(self.cfg)
        old_v = self.cfg.path("videos") / "draft_old.mp4"
        new_v = self.cfg.path("videos") / "draft_new.mp4"
        old_v.write_bytes(b"x")
        new_v.write_bytes(b"x")
        old = self.db.create_draft(conn, "http://old", "rejected", "{}")
        new = self.db.create_draft(conn, "http://new", "rejected", "{}")
        self.db.update_draft(conn, old, video_path=str(old_v))
        self.db.update_draft(conn, new, video_path=str(new_v))
        conn.execute("UPDATE drafts SET updated_at='2000-01-01T00:00:00' WHERE id=?", (old,))
        conn.commit()
        conn.close()
        self.pipeline.cleanup(self.cfg)
        self.assertFalse(old_v.exists())
        self.assertTrue(new_v.exists())
        self.assertIsNotNone(self.row(old))  # rows never deleted

    def test_retry_skips_done_targets(self):
        self._publish({"yt", "tt"}, tt=mock.Mock(side_effect=RuntimeError("boom")))
        self.assertEqual(self.row()["status"], "partial")
        out, yt, tt = self._publish({"yt", "tt"})
        yt.assert_not_called()  # already uploaded
        tt.assert_called_once()
        r = self.row()
        self.assertEqual(r["status"], "published")
        self.assertEqual(r["youtube_id"], "YT123")
        self.assertEqual(r["tiktok_publish_id"], "TT456")

    def test_retry_single_remaining_target_counts_previous(self):
        self._publish({"yt", "tt"}, tt=mock.Mock(side_effect=RuntimeError("boom")))
        self._publish({"tt"})
        self.assertEqual(self.row()["status"], "published")

    def test_stuck_publishing_retried_recent_not(self):
        conn = self.db.connect(self.cfg)
        self.db.update_draft(conn, self.id, status="publishing")
        conn.close()
        out, yt, _ = self._publish({"yt"})
        yt.assert_not_called()
        self.assertIn("already publishing", out)
        conn = self.db.connect(self.cfg)
        conn.execute("UPDATE drafts SET status='publishing', updated_at='2000-01-01T00:00:00' WHERE id=?",
                     (self.id,))
        conn.commit()
        conn.close()
        _, yt, _ = self._publish({"yt"})
        yt.assert_called_once()
        self.assertEqual(self.row()["status"], "published")

    def test_cleanup_glob_does_not_touch_other_drafts_and_removes_voice(self):
        work = self.cfg.path("work")
        for n in ("draft_1.wav", "draft_1.ass", "draft_1_broll.mp4", "draft_12.wav",
                  "draft_12_broll.mp4", "voice_1.ogg", "voice_12.ogg"):
            (work / n).write_bytes(b"x")
        conn = self.db.connect(self.cfg)
        conn.execute("UPDATE drafts SET status='rejected', updated_at='2000-01-01T00:00:00' WHERE id=?",
                     (self.id,))
        conn.commit()
        conn.close()
        self.assertEqual(self.id, 1)
        self.pipeline.cleanup(self.cfg)
        for n in ("draft_1.wav", "draft_1.ass", "draft_1_broll.mp4", "voice_1.ogg"):
            self.assertFalse((work / n).exists(), n)
        for n in ("draft_12.wav", "draft_12_broll.mp4", "voice_12.ogg"):
            self.assertTrue((work / n).exists(), n)

    def test_make_draft_notifies_and_reraises_on_early_failure(self):
        self.cfg.telegram_bot_token = "SECRET"
        self.cfg.telegram_chat_id = "1"
        with mock.patch("cybershorts.news.fetch_candidates",
                        side_effect=RuntimeError("All feeds failed SECRET")), \
             mock.patch.object(self.pipeline, "_telegram") as tg:
            with self.assertRaises(RuntimeError):
                self.pipeline.make_draft(self.cfg)
        sent = tg.return_value.send_message.call_args[0][0]
        self.assertIn("Scheduled draft failed", sent)
        self.assertIn("All feeds failed", sent)
        self.assertNotIn("SECRET", sent)

    def _render_patches(self, size, render_side_effect=None, broll=True):
        wav = self.cfg.path("work") / "draft_1.wav"
        wav.write_bytes(b"w")
        conn = self.db.connect(self.cfg)
        self.db.update_draft(conn, self.id, audio_path=str(wav))
        conn.close()
        self.cfg.raw.setdefault("broll", {})["provider"] = "pixabay" if broll else "none"

        def fake_render(cfg, wav, ass, br, out, beats=None, **kw):
            if render_side_effect and br:
                raise RuntimeError("bad broll")
            Path(out).write_bytes(b"\0" * size)
            return out

        return fake_render

    def _run_render(self, size, **kw):
        fake = self._render_patches(size, **kw)
        tg = mock.Mock()
        tg.send_message.return_value = {"message_id": 5}
        tg.send_video.return_value = {"message_id": 6}
        render_mock = mock.Mock(side_effect=fake)
        clips = [Path("/nonexistent/b0.mp4"), Path("/nonexistent/b1.mp4")]
        with mock.patch("cybershorts.captions.transcribe_words", return_value=[(0.0, 0.5, "a")]), \
             mock.patch("cybershorts.captions.chunk_words", return_value=[]), \
             mock.patch("cybershorts.captions.beat_times", create=True, return_value=[0.0, 0.5]) as bt, \
             mock.patch("cybershorts.captions.write_ass") as wa, \
             mock.patch("cybershorts.render.probe_duration", return_value=1.0), \
             mock.patch("cybershorts.render.render", render_mock), \
             mock.patch("cybershorts.visuals.fetch_broll_set", return_value=clips) as fs, \
             mock.patch.object(self.pipeline, "_telegram", return_value=tg):
            self.pipeline.render_draft(self.cfg, self.id)
        self.mocks = {"beat_times": bt, "write_ass": wa, "fetch_broll_set": fs}
        return tg, render_mock

    def test_render_small_video_sent_as_video(self):
        tg, _ = self._run_render(10)
        tg.send_video.assert_called_once()
        tg.send_message.assert_not_called()

    def test_render_oversize_video_sent_as_message_with_path(self):
        with mock.patch.object(self.pipeline, "MAX_TG_VIDEO_BYTES", 5):
            tg, _ = self._run_render(10)
        tg.send_video.assert_not_called()
        text, kb = tg.send_message.call_args[0]
        self.assertIn("draft_1.mp4", text)
        self.assertIn("inline_keyboard", kb)
        self.assertEqual(self.row()["status"], "rendered")

    def test_render_retries_without_broll(self):
        tg, render_mock = self._run_render(10, render_side_effect=True)
        self.assertEqual(render_mock.call_count, 2)
        self.assertIsNone(render_mock.call_args_list[1][0][3])
        tg.send_video.assert_called_once()

    def test_render_uses_v2_interfaces(self):
        _, render_mock = self._run_render(10)
        self.mocks["beat_times"].assert_called_once()
        n_args, _ = self.mocks["fetch_broll_set"].call_args
        self.assertEqual(n_args[1], 2)  # one clip per beat
        self.assertTrue(str(n_args[3]).endswith("draft_1_broll"))
        kw = self.mocks["write_ass"].call_args.kwargs
        self.assertEqual(kw["beats"], [0.0, 0.5])
        self.assertEqual(kw["words"], [(0.0, 0.5, "a")])
        self.assertIn("keywords", kw)
        args, rkw = render_mock.call_args
        self.assertEqual(len(args[3]), 2)
        self.assertEqual(rkw["beats"], [0.0, 0.5])

    def test_render_no_broll_provider_none(self):
        _, render_mock = self._run_render(10, broll=False)
        self.mocks["fetch_broll_set"].assert_not_called()
        self.assertIsNone(render_mock.call_args[0][3])

    def test_caption_fact_check_lines_and_limit(self):
        P = self.pipeline
        base = {"on_screen_title": "T <x>", "script": "word " * 80, "description": "D. Source: u",
                "hashtags": ["a"]}
        ok = P._telegram_caption(dict(base, fact_check={"ok": True, "issues": []}), "http://x", 1)
        self.assertIn("✅ Fact-check: nothing unsupported found", ok)
        self.assertIn("&lt;x&gt;", ok)
        bad = P._telegram_caption(dict(base, fact_check={"ok": False, "issues": ["Says <b>9.8</b> & more"]}),
                                  "http://x", 1)
        self.assertIn("⚠️ Fact-check:", bad)
        self.assertIn("• Says &lt;b&gt;9.8&lt;/b&gt; &amp; more", bad)
        sk = P._telegram_caption(dict(base, fact_check={"ok": True, "issues": [], "skipped": "x"}), "http://x", 1)
        self.assertIn("ℹ️ Fact-check skipped", sk)
        self.assertIn("(x)", sk)
        esc = P._telegram_caption(
            dict(base, fact_check={"ok": True, "issues": [], "skipped": "<b>" + "y" * 200}), "http://x", 1)
        self.assertIn("&lt;b&gt;", esc)
        self.assertNotIn("y" * 81, esc)
        self.assertNotIn("Fact-check", P._telegram_caption(base, "http://x", 1))
        many = dict(base, script="word " * 200,
                    fact_check={"ok": False, "issues": ["issue %d " % i + "z" * 125 for i in range(5)]})
        cap = P._telegram_caption(many, "http://x", 1)
        self.assertLessEqual(len(cap), 1024)
        self.assertIn("• issue 0", cap)

    def test_cleanup_removes_numbered_broll(self):
        work = self.cfg.path("work")
        names = ["draft_1_broll_0.mp4", "draft_1_broll_1.mp4", "draft_12_broll_0.mp4"]
        for n in names:
            (work / n).write_bytes(b"x")
        conn = self.db.connect(self.cfg)
        self.db.update_draft(conn, self.id, status="rejected")
        conn.execute("UPDATE drafts SET updated_at='2000-01-01T00:00:00' WHERE id=?", (self.id,))
        conn.commit()
        conn.close()
        self.pipeline.cleanup(self.cfg)
        self.assertFalse((work / names[0]).exists() or (work / names[1]).exists())
        self.assertTrue((work / names[2]).exists())

    def test_keyboard_remaining_targets(self):
        from cybershorts.telegram import keyboard
        kb = keyboard(5, yt_enabled=False, tt_enabled=True, manual_fallback=False)
        cbs = [b["callback_data"] for row in kb["inline_keyboard"] for b in row]
        self.assertEqual(sorted(cbs), ["pub:tt:5", "rej:5"])

    def test_health_ping_strips_query(self):
        from cybershorts import health
        with mock.patch("cybershorts.health.requests.get") as get:
            health.ping("http://k/api/push/abc?status=up&msg=OK&ping=", "down", "x y")
        self.assertEqual(get.call_args[0][0], "http://k/api/push/abc?status=down&msg=x%20y&ping=")

    def test_youtube_strips_angle_brackets(self):
        from cybershorts import youtube
        self.assertEqual(youtube._clean("a <b> c"), "a b c")

    def test_atomic_token_saves_mode_0600(self):
        import stat
        from cybershorts import tiktok, youtube
        tiktok._save_token(self.cfg, {"access_token": "a", "expires_in": 5})
        p = tiktok.token_path(self.cfg)
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        self.assertFalse(p.with_name(p.name + ".tmp").exists())
        creds = mock.Mock()
        creds.to_json.return_value = "{}"
        yp = Path(self.cfg.path("yt_test.json"))
        youtube._save(creds, yp)
        self.assertEqual(stat.S_IMODE(yp.stat().st_mode), 0o600)
        self.assertFalse(yp.with_name(yp.name + ".tmp").exists())

    def test_keyboard_manual_when_disabled(self):
        from cybershorts.telegram import keyboard
        kb = keyboard(5, yt_enabled=False, tt_enabled=False)
        cbs = [b["callback_data"] for row in kb["inline_keyboard"] for b in row]
        self.assertEqual(sorted(cbs), ["man:5", "rej:5"])
        cbs = [b["callback_data"] for row in keyboard(5)["inline_keyboard"] for b in row]
        self.assertNotIn("man:5", cbs)


if __name__ == "__main__":
    unittest.main()


class PipelineCrashSafetyTests(PipelineTests):
    """Round-2 review fixes: ids persisted immediately; reject can't undo a publish; stuck detection."""

    def test_youtube_id_saved_before_tiktok_step(self):
        crash = mock.Mock(side_effect=KeyboardInterrupt)  # simulates the process dying mid-TikTok
        with self.assertRaises(KeyboardInterrupt):
            self._publish({"yt", "tt"}, tt=crash)
        self.assertEqual(self.row()["youtube_id"], "YT123")
        # retry must not upload to YouTube again
        _, yt, _ = self._publish({"yt", "tt"})
        yt.assert_not_called()

    def test_reject_refuses_published(self):
        self._publish({"yt", "tt"})
        with self.assertRaises(ValueError):
            self.pipeline.reject(self.cfg, self.id)
        self.assertEqual(self.row()["status"], "published")

    def test_stuck_publishing_detected(self):
        conn = self.db.connect(self.cfg)
        try:
            conn.execute("UPDATE drafts SET status='publishing', updated_at='2000-01-01T00:00:00' WHERE id=?",
                         (self.id,))
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self.pipeline.stuck_publishing(self.cfg), [self.id])


class TikTokCaptionTests(PipelineTests):
    def test_caption_available_after_tiktok_upload(self):
        self.assertIsNone(self.pipeline.tiktok_caption_for(self.cfg, self.id))
        self._publish({"tt"})
        cap = self.pipeline.tiktok_caption_for(self.cfg, self.id)
        self.assertEqual(cap, "Desc one. #a #b")

    def test_draft_caption_has_tap_to_copy(self):
        data = {"on_screen_title": "T", "script": "s", "description": "Desc one. Source: http://x",
                "hashtags": ["a"]}
        cap = self.pipeline._telegram_caption(data, "http://x", 1)
        self.assertIn("<code>Desc one. #a</code>", cap)
