"""Regression tests for the review fixes: recap week check / kill recovery, republish, sound, limits."""
import copy
import importlib.util
import json
import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cybershorts import config, db, pipeline, recap, recap_publish, sound, youtube

ROOT = Path(__file__).resolve().parents[1]
WEEK = "2026-W47"


def _cfg(tmp):
    cfg = config.Config(Path(tmp), copy.deepcopy(config.DEFAULTS), {})
    cfg.telegram_bot_token = ""
    cfg.telegram_chat_id = ""
    return cfg


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(mock.patch.stopall)
        self.cfg = _cfg(self._tmp.name)

    def add_recap(self, status="rendered", video=True, tg=4242, youtube_id=None, comment=None, data=None):
        conn = db.connect(self.cfg)
        db.init(conn)
        d = {"youtube_title": "T", "description": "D", "hashtags": [], "first_comment": "Q?", "week": WEEK}
        d.update(data or {})
        i = db.create_draft(conn, f"recap:{WEEK}", status, json.dumps(d), kind="recap")
        fields = {}
        if video:
            v = self.cfg.path("videos") / f"draft_{i}.mp4"
            v.parent.mkdir(parents=True, exist_ok=True)
            v.write_bytes(b"fake")
            fields["video_path"] = str(v)
        if tg:
            fields["tg_message_id"] = tg
        if youtube_id:
            fields["youtube_id"] = youtube_id
        if comment:
            fields["yt_comment_id"] = comment
        if fields:
            db.update_draft(conn, i, **fields)
        conn.close()
        self.id = i
        return i

    def row(self, i=None):
        conn = db.connect(self.cfg)
        try:
            return db.get_draft(conn, i or self.id)
        finally:
            conn.close()


class WeekCheckTests(Base):
    def _run(self, stories=(), **kw):
        sel = mock.Mock(return_value=list(stories))
        with mock.patch("cybershorts.recap.week_key", return_value=WEEK), \
                mock.patch("cybershorts.recap.select_stories", sel), \
                mock.patch("cybershorts.pipeline._notify") as nt:
            out = recap.run(self.cfg, **kw)
        return out, sel, nt

    def test_stale_rendered_without_video_is_failed_and_continues(self):
        i = self.add_recap("rendered", video=False, tg=None)
        _, sel, _ = self._run()
        sel.assert_called_once()  # the week was not blocked
        r = self.row(i)
        self.assertEqual(r["status"], "failed")
        self.assertIn("stale", r["error"])

    def test_stale_rendered_without_tg_message_is_failed(self):
        i = self.add_recap("rendered", video=True, tg=None)
        _, sel, _ = self._run()
        sel.assert_called_once()
        self.assertEqual(self.row(i)["status"], "failed")

    def test_complete_rendered_row_still_blocks(self):
        i = self.add_recap("rendered")
        out, sel, nt = self._run(on_demand=True)
        self.assertIsNone(out)
        sel.assert_not_called()
        self.assertEqual(self.row(i)["status"], "rendered")
        self.assertIn("already made", nt.call_args.args[1])

    def test_failed_with_video_blocks_and_notes(self):
        self.add_recap("failed")
        out, sel, nt = self._run(on_demand=True)
        self.assertIsNone(out)
        sel.assert_not_called()
        msg = nt.call_args.args[1]
        self.assertIn(f"Recap #{self.id} is rendered but not published", msg)
        self.assertIn("tap Publish", msg)
        self.assertIn("/recap force", msg)

    def test_failed_with_video_silent_when_not_on_demand(self):
        self.add_recap("failed")
        _, sel, nt = self._run()
        sel.assert_not_called()
        nt.assert_not_called()

    def test_failed_without_video_unblocks(self):
        self.add_recap("failed", video=False, tg=None)
        _, sel, _ = self._run()
        sel.assert_called_once()

    def test_force_overrides_failed_with_video(self):
        self.add_recap("failed")
        _, sel, _ = self._run(force=True)
        sel.assert_called_once()


class KilledRunTests(Base):
    def _data(self):
        return {"title": "T", "on_screen_title": "T", "script": "a b c", "description": "d"}

    def _run(self, exc):
        self.cfg.raw["llm"]["fact_check"] = False
        stories = [{"draft_id": i, "youtube_id": f"y{i}", "url": "u", "title": "t"} for i in range(3)]
        with mock.patch("cybershorts.recap.week_key", return_value=WEEK), \
                mock.patch("cybershorts.recap.select_stories", return_value=stories), \
                mock.patch("cybershorts.recap.write_recap_script", return_value=self._data()), \
                mock.patch("cybershorts.recap._produce", side_effect=exc), \
                mock.patch("cybershorts.pipeline._notify") as nt, \
                mock.patch("cybershorts.pipeline._cleanup_broll"):
            with self.assertRaises(type(exc)):
                recap.run(self.cfg)
        return nt

    def _only_row(self):
        conn = db.connect(self.cfg)
        try:
            return conn.execute("SELECT * FROM drafts WHERE kind='recap'").fetchone()
        finally:
            conn.close()

    def test_systemexit_marks_failed_and_reraises(self):
        nt = self._run(SystemExit(143))
        r = self._only_row()
        self.assertEqual(r["status"], "failed")
        self.assertTrue(r["error"])
        nt.assert_called()

    def test_keyboardinterrupt_marks_failed(self):
        self._run(KeyboardInterrupt())
        self.assertEqual(self._only_row()["status"], "failed")

    def test_exception_still_marks_failed(self):
        self._run(RuntimeError("x"))
        self.assertEqual(self._only_row()["status"], "failed")

    def test_script_installs_sigterm_handler(self):
        spec = importlib.util.spec_from_file_location("script_recap_sig", ROOT / "scripts" / "recap.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        old = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, old)
        lock = mock.MagicMock()
        with mock.patch("cybershorts.pipeline.draft_lock", return_value=lock), \
                mock.patch("cybershorts.recap.run", return_value=1), \
                mock.patch("cybershorts.config.load", return_value=self.cfg):
            mod.main(["--on-demand"])
        handler = signal.getsignal(signal.SIGTERM)
        self.assertEqual(handler, mod._on_sigterm)
        with self.assertRaises(SystemExit):
            handler(signal.SIGTERM, None)


class PublishFixTests(Base):
    def _pub(self, **patches):
        up = patches.pop("upload", mock.Mock(return_value="NEW"))
        st = patches.pop("thumb", mock.Mock(return_value=(True, "ok")))
        pc = patches.pop("comment", mock.Mock(return_value=("C1", "ok")))
        with mock.patch("cybershorts.youtube.upload", up), \
                mock.patch("cybershorts.youtube.set_thumbnail", st), \
                mock.patch("cybershorts.youtube.post_comment", pc):
            out = recap_publish.publish(self.cfg, self.id)
        return out, up, st, pc

    def _thumb(self):
        self.cfg.path("work", f"draft_{self.id}_thumb.jpg").write_bytes(b"jpg")

    def test_retry_after_crash_sets_thumbnail_and_comment(self):
        self.add_recap("publishing", youtube_id="VID")
        self._thumb()
        conn = db.connect(self.cfg)
        conn.execute("UPDATE drafts SET updated_at='2000-01-01T00:00:00' WHERE id=?", (self.id,))
        conn.commit()
        conn.close()
        out, up, st, pc = self._pub()
        up.assert_not_called()
        st.assert_called_once()
        self.assertEqual(st.call_args.args[1], "VID")
        pc.assert_called_once()
        r = self.row()
        self.assertEqual((r["status"], r["yt_comment_id"]), ("published", "C1"))
        self.assertIn("Thumbnail set", out)

    def test_existing_comment_not_reposted(self):
        self.add_recap("failed", youtube_id="VID", comment="OLD")
        self._thumb()
        _, up, st, pc = self._pub()
        st.assert_called_once()
        pc.assert_not_called()
        self.assertEqual(self.row()["yt_comment_id"], "OLD")

    def test_failed_recap_can_be_republished_via_pipeline(self):
        self.add_recap("failed")
        self._thumb()
        up = mock.Mock(return_value="V9")
        with mock.patch("cybershorts.youtube.upload", up), \
                mock.patch("cybershorts.youtube.set_thumbnail", return_value=(True, "ok")), \
                mock.patch("cybershorts.youtube.post_comment", return_value=("C", "ok")):
            out = pipeline.publish(self.cfg, self.id, {"yt"})
        up.assert_called_once()
        self.assertEqual(self.row()["status"], "published")
        self.assertIn("published", out)

    def test_upload_failure_leaves_video_so_week_stays_blocked(self):
        self.add_recap("rendered")
        self._pub(upload=mock.Mock(side_effect=RuntimeError("quota")))
        self.assertEqual(self.row()["status"], "failed")
        with mock.patch("cybershorts.recap.week_key", return_value=WEEK), \
                mock.patch("cybershorts.recap.select_stories") as sel, \
                mock.patch("cybershorts.pipeline._notify") as nt:
            self.assertIsNone(recap.run(self.cfg, on_demand=True))
        sel.assert_not_called()
        self.assertIn("rendered but not published", nt.call_args.args[1])

    def test_verify_hint_only_for_403(self):
        for reason, expect in (("channel not allowed custom thumbnails (verify phone)", True),
                               ("HTTP 403", True), ("HTTP 500", False), ("thumbnail file missing", False)):
            with self.subTest(reason=reason):
                self.add_recap("rendered")
                self._thumb()
                out, *_ = self._pub(thumb=mock.Mock(return_value=(False, reason)))
                self.assertEqual("youtube.com/verify" in out, expect, out)
                self.assertEqual(out.count("verify phone"), 1 if "verify phone" in reason else 0)

    def test_youtube_disabled_no_upload(self):
        self.add_recap("rendered")
        self.cfg.raw["youtube"]["enabled"] = False
        out, up, st, pc = self._pub()
        up.assert_not_called()
        st.assert_not_called()
        self.assertIn("youtube.enabled", out)
        self.assertEqual(self.row()["status"], "rendered")


class TikTokRoutingTests(unittest.TestCase):
    def test_clean_tiktok_version_wins_over_music(self):
        from cybershorts import music
        from tests.test_pipeline import PipelineTests

        t = PipelineTests("test_both_ok")
        t.setUp()
        self.addCleanup(t.tearDown)
        t.cfg.raw["music"]["on_tiktok"] = True
        mv = music.music_version(t.cfg, t.id)
        mv.write_bytes(b"music")
        tv = sound.tiktok_version(t.cfg, t.id)
        tv.write_bytes(b"clean")
        with self.assertLogs("cybershorts", level="INFO") as lg:
            _, yt, tt = t._publish({"yt", "tt"})
        self.assertEqual(Path(yt.call_args.args[1]), mv)
        self.assertEqual(Path(tt.call_args.args[1]), tv)
        self.assertTrue(any("music isn't added to the clean TikTok" in m for m in lg.output))


class DescriptionByteLimitTests(Base):
    def test_recap_description_capped_in_bytes_chapters_kept(self):
        ch = [(0, "Intro"), (30, "One"), (70, "Two"), (110, "Three")]
        data = {"description": "é" * 4000, "duration": 300, "hashtags": ["a", "b"]}
        stories = [{"title": "Ünïcode " * 10, "youtube_id": "x", "url": "https://e.com/1"}] * 3
        text = recap.build_description(self.cfg, data, ch, stories)
        self.assertLessEqual(len(text.encode("utf-8")), 4900)
        self.assertIn("Chapters\n00:00 Intro", text)
        self.assertIn("01:50 Three", text)

    def test_ascii_unchanged(self):
        data = {"description": "hello", "duration": 300, "hashtags": []}
        self.assertEqual(recap.build_description(self.cfg, data, [], []), "hello")

    def test_youtube_cap_bytes(self):
        s = "€" * 3000  # 3 bytes each
        out = youtube._cap_bytes(s, 4900)
        self.assertLessEqual(len(out.encode("utf-8")), 4900)
        self.assertEqual(out, "€" * 1633)
        self.assertEqual(youtube._cap_bytes("abc", 4900), "abc")


class PromptTests(Base):
    def test_tag_budget_not_contradictory(self):
        self.cfg.raw["recap"]["max_audio_tags"] = 5
        text = recap.tagmod_instructions(self.cfg, 5)
        self.assertIn("at most 5 voice tags in total", text)
        self.assertNotIn("at most 3", text)
        self.assertIn("[curious]", text)


class RecapAudioTests(Base):
    def setUp(self):
        super().setUp()
        self.kit = Path(self._tmp.name) / "kit"
        self.kit.mkdir()
        self.cfg.raw["sound"]["folder"] = str(self.kit)
        self.parts = [{"kind": "intro", "start": 0.0, "end": 8.0}, {"kind": "story", "start": 8.0, "end": 30.0},
                      {"kind": "outro", "start": 30.0, "end": 40.0}]

    def _mix(self, names):
        for n in names:
            (self.kit / n).write_bytes(b"x" * 10)
        cmds = []
        with mock.patch("cybershorts.sound._probe", return_value=40.0), \
                mock.patch("cybershorts.sound._run", side_effect=lambda cfg, cmd, *a: cmds.append(cmd) or True):
            res = sound.recap_audio(self.cfg, "v.wav", self.parts, Path(self._tmp.name) / "o.wav")
        return res, cmds[0] if cmds else None

    def _bed_used(self, cmd):
        i = cmd.index("-stream_loop")
        return Path(cmd[i + 3]).name

    def test_beds_rotate_and_ignore_variants(self):
        (_, has), cmd = self._mix(["bed1.mp3", "bed2.mp3", "bed2.tmp.mp3", "theme.mp3"])
        self.assertTrue(has)
        seen = {self._bed_used(cmd)}
        for _ in range(6):
            with mock.patch("cybershorts.sound._probe", return_value=40.0), \
                    mock.patch("cybershorts.sound._run", side_effect=lambda cfg, c, *a: seen.add(self._bed_used(c)) or True):
                sound.recap_audio(self.cfg, "v.wav", self.parts, Path(self._tmp.name) / "o.wav")
        self.assertEqual(seen, {"bed1.mp3", "bed2.mp3"})

    def test_music_disabled_skips_bed_keeps_theme(self):
        self.cfg.raw["music"]["enabled"] = False
        (out, has), cmd = self._mix(["bed1.mp3", "theme.mp3"])
        self.assertFalse(has)
        self.assertIsNotNone(out)
        self.assertNotIn("-stream_loop", cmd)
        self.assertTrue(any(str(c).endswith("theme.mp3") for c in cmd))

    def test_music_disabled_and_only_bed_returns_none(self):
        self.cfg.raw["music"]["enabled"] = False
        res, cmd = self._mix(["bed1.mp3"])
        self.assertEqual(res, (None, False))
        self.assertIsNone(cmd)


class BotRecapTests(Base):
    def _drive(self, text, which):
        spec = importlib.util.spec_from_file_location("bot_under_test2", ROOT / "scripts" / "bot.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.cfg.telegram_bot_token, self.cfg.telegram_chat_id = "tok", "1"
        tg = mock.Mock()
        calls = []

        def updates(*a, **k):
            if calls:
                raise KeyboardInterrupt
            calls.append(1)
            return [{"update_id": 1, "message": {"chat": {"id": 1}, "text": text}}]

        tg.get_updates.side_effect = updates
        old = os.environ.get("APP_DIR")
        with mock.patch("cybershorts.config.load", return_value=self.cfg), \
                mock.patch("cybershorts.telegram.Telegram", return_value=tg), \
                mock.patch("cybershorts.pipeline.stuck_publishing", return_value=[]), \
                mock.patch("shutil.which", return_value=which), \
                mock.patch("subprocess.Popen") as popen:
            with self.assertRaises(KeyboardInterrupt):
                mod.main()
        if old is None:
            os.environ.pop("APP_DIR", None)
        return popen

    def test_recap_child_runs_niced(self):
        popen = self._drive("/recap force", "/usr/bin/nice")
        argv = popen.call_args.args[0]
        self.assertEqual(argv[:3], ["nice", "-n", "10"])
        self.assertTrue(argv[4].endswith("recap.py"))
        self.assertEqual(argv[-2:], ["--on-demand", "--force"])

    def test_recap_child_plain_without_nice(self):
        popen = self._drive("/recap", None)
        argv = popen.call_args.args[0]
        self.assertTrue(argv[1].endswith("recap.py"))
        self.assertNotIn("nice", argv)


if __name__ == "__main__":
    unittest.main()
