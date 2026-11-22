import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def _load_script(name):
    spec = importlib.util.spec_from_file_location("script_" + name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("APP_DIR")
        os.environ["APP_DIR"] = self._tmp.name
        from cybershorts import config, db, pipeline, recap_publish

        self.db, self.pipeline, self.rp = db, pipeline, recap_publish
        self.cfg = config.load()
        self.cfg.telegram_bot_token = ""
        self.cfg.telegram_chat_id = ""

    def tearDown(self):
        if self._old is None:
            os.environ.pop("APP_DIR", None)
        else:
            os.environ["APP_DIR"] = self._old
        self._tmp.cleanup()

    def make_recap(self, **extra):
        data = {"youtube_title": "This Week in Cyber: A & B", "description": "Chapters\n00:00 Intro",
                "hashtags": ["cyber"], "first_comment": "Which story mattered?", "week": "2026-W47",
                "duration": 300, "chapters": [[0, "Intro"], [30, "One"], [70, "Two"]],
                "thumbnail_text": "WEEK <IN> CYBER", "script": "word " * 700}
        data.update(extra)
        conn = self.db.connect(self.cfg)
        self.db.init(conn)
        video = self.cfg.path("videos") / "draft_1.mp4"
        video.write_bytes(b"fake")
        i = self.db.create_draft(conn, "recap:2026-W47", "rendered", json.dumps(data), kind="recap")
        self.db.update_draft(conn, i, video_path=str(video))
        conn.close()
        self.id = i
        self.video = video
        return i

    def row(self):
        conn = self.db.connect(self.cfg)
        try:
            return self.db.get_draft(conn, self.id)
        finally:
            conn.close()


class PublishTests(Base):
    def _pub(self, upload=None, thumb=(True, "ok"), comment=("C1", "ok"), thumb_file=True):
        self.make_recap()
        if thumb_file:
            self.cfg.path("work", f"draft_{self.id}_thumb.jpg").write_bytes(b"jpg")
        up = upload or mock.Mock(return_value="RV1")
        tt = mock.Mock()
        with mock.patch("cybershorts.youtube.upload", up), \
             mock.patch("cybershorts.youtube.set_thumbnail", mock.Mock(side_effect=thumb) if isinstance(thumb, Exception) else mock.Mock(return_value=thumb)) as st, \
             mock.patch("cybershorts.youtube.post_comment", mock.Mock(return_value=comment)), \
             mock.patch("cybershorts.tiktok.upload_draft", tt):
            out = self.pipeline.publish(self.cfg, self.id, {"yt", "tt"})
        return out, up, tt, st

    def test_dispatch_category_no_shorts_no_tiktok(self):
        with mock.patch("cybershorts.recap_publish.publish", return_value="x") as rp:
            self.make_recap()
            self.assertEqual(self.pipeline.publish(self.cfg, self.id, {"yt"}), "x")
            rp.assert_called_once_with(self.cfg, self.id)
        out, up, tt, st = self._pub()
        kw = up.call_args.kwargs
        self.assertEqual(kw["category_id"], "28")
        args = up.call_args.args
        self.assertNotIn("#Shorts", args[2])
        self.assertNotIn("#Shorts", args[3])
        tt.assert_not_called()
        self.assertEqual(self.row()["status"], "published")
        self.assertEqual(self.row()["youtube_id"], "RV1")
        self.assertIn("https://youtu.be/RV1", out)
        self.assertNotIn("/shorts/", out)
        self.assertIn("Thumbnail set", out)

    def test_music_version_preferred(self):
        self.make_recap()
        mv = self.cfg.path("work") / f"draft_{self.id}_music.mp4"
        mv.write_bytes(b"m")
        up = mock.Mock(return_value="V")
        with mock.patch("cybershorts.youtube.upload", up), \
             mock.patch("cybershorts.youtube.post_comment", return_value=(None, "x")):
            self.rp.publish(self.cfg, self.id)
        self.assertEqual(Path(up.call_args.args[1]), mv)

    def test_thumbnail_failure_still_published(self):
        out, *_ = self._pub(thumb=(False, "channel not allowed custom thumbnails (verify phone)"))
        self.assertEqual(self.row()["status"], "published")
        self.assertIn("⚠️ Thumbnail not set", out)
        self.assertIn("youtube.com/verify", out)

    def test_thumbnail_exception_still_published(self):
        out, *_ = self._pub(thumb=RuntimeError("boom"))
        r = self.row()
        self.assertEqual((r["status"], r["youtube_id"]), ("published", "RV1"))
        self.assertIn("Thumbnail not set: RuntimeError", out)
        self.assertNotIn("youtube.com/verify", out)  # the phone-verify hint is only for 403 / not-allowed

    def test_comment_failure_keeps_id(self):
        self.make_recap()
        with mock.patch("cybershorts.youtube.upload", return_value="RV2"), \
             mock.patch("cybershorts.youtube.set_thumbnail", return_value=(True, "ok")), \
             mock.patch("cybershorts.youtube.post_comment", side_effect=RuntimeError("x")):
            out = self.rp.publish(self.cfg, self.id)
        self.assertEqual(self.row()["youtube_id"], "RV2")
        self.assertEqual(self.row()["status"], "published")
        self.assertIn("Comment skipped", out)

    def test_upload_failure(self):
        out, *_ = self._pub(upload=mock.Mock(side_effect=RuntimeError("quota")))
        r = self.row()
        self.assertEqual(r["status"], "failed")
        self.assertIn("quota", r["error"])
        self.assertIn("YouTube failed", out)

    def test_already_published(self):
        self.make_recap()
        conn = self.db.connect(self.cfg)
        self.db.update_draft(conn, self.id, status="published")
        conn.close()
        up = mock.Mock()
        with mock.patch("cybershorts.youtube.upload", up):
            self.assertIn("already published", self.rp.publish(self.cfg, self.id))
        up.assert_not_called()


class CaptionTests(Base):
    def test_escapes_html(self):
        self.make_recap()
        cap = self.rp.recap_caption({"youtube_title": "<b>x</b> & y", "thumbnail_text": "<A>",
                                     "chapters": [[0, "<i>"], [20, "b"], [50, "c"]],
                                     "fact_check": {"ok": False, "issues": ["<script>"]}}, 5)
        self.assertNotIn("<b>x", cap)
        self.assertNotIn("<script>", cap)
        self.assertIn("&lt;A&gt;", cap)
        self.assertIn("&amp; y", cap)

    def test_length_limit_many_chapters_and_issues(self):
        data = {"youtube_title": "T" * 100, "week": "2026-W47", "duration": 3700,
                "chapters": [[i * 15, "Chapter title number %d <&>" % i] for i in range(60)],
                "thumbnail_text": "X", "fact_check": {"ok": False, "issues": ["issue <&> " * 20] * 5}}
        cap = self.rp.recap_caption(data, 12)
        self.assertLessEqual(len(cap), 1024)
        self.assertIn("Weekly recap #12", cap)

    def test_no_chapters_warning(self):
        cap = self.rp.recap_caption({"youtube_title": "T", "chapters": [], "chapters_warning": "too short"}, 1)
        self.assertIn("⚠️ No chapters: too short", cap)


class SendReviewTests(Base):
    def _tg(self):
        tg = mock.Mock()
        tg.send_video.return_value = {"message_id": 7}
        tg.send_message.return_value = {"message_id": 8}
        return tg

    def test_video_path(self):
        self.make_recap()
        prev, thumb = self.cfg.path("work", "p.mp4"), self.cfg.path("work", "t.jpg")
        prev.write_bytes(b"v")
        thumb.write_bytes(b"j")
        tg = self._tg()
        with mock.patch.object(self.pipeline, "_telegram", return_value=tg):
            mid = self.rp.send_review(self.cfg, self.id, {"youtube_title": "T"}, prev, thumb, "/full.mp4")
        self.assertEqual(mid, 7)
        tg.send_photo.assert_called_once()
        kb = tg.send_video.call_args.args[2]
        self.assertEqual(kb["inline_keyboard"][0][0]["callback_data"], "pub:yt:%d" % self.id)
        tg.send_message.assert_not_called()

    def test_fallback_missing_preview(self):
        self.make_recap()
        tg = self._tg()
        with mock.patch.object(self.pipeline, "_telegram", return_value=tg):
            mid = self.rp.send_review(self.cfg, self.id, {"youtube_title": "T"}, None, None, "/srv/full.mp4")
        self.assertEqual(mid, 8)
        text, kb = tg.send_message.call_args.args
        self.assertIn("/srv/full.mp4", text)
        self.assertEqual(kb["inline_keyboard"][1][0]["callback_data"], "rej:%d" % self.id)
        tg.send_video.assert_not_called()

    def test_fallback_too_big(self):
        self.make_recap()
        prev = self.cfg.path("work", "p.mp4")
        prev.write_bytes(b"v")
        tg = self._tg()
        with mock.patch.object(self.pipeline, "_telegram", return_value=tg), \
             mock.patch.object(self.pipeline, "MAX_TG_VIDEO_BYTES", 0):
            self.rp.send_review(self.cfg, self.id, {"youtube_title": "T"}, prev, None, "/f.mp4")
        tg.send_video.assert_not_called()
        tg.send_message.assert_called_once()

    def test_photo_failure_ignored(self):
        self.make_recap()
        thumb = self.cfg.path("work", "t.jpg")
        thumb.write_bytes(b"j")
        tg = self._tg()
        tg.send_photo.side_effect = RuntimeError("x")
        with mock.patch.object(self.pipeline, "_telegram", return_value=tg):
            self.assertEqual(self.rp.send_review(self.cfg, self.id, {}, None, thumb, "/f"), 8)

    def test_keyboard_and_send_photo(self):
        from cybershorts.telegram import Telegram, recap_keyboard
        kb = recap_keyboard(3)
        self.assertEqual([b["callback_data"] for r in kb["inline_keyboard"] for b in r], ["pub:yt:3", "rej:3"])
        t = Telegram("tok", 1)
        p = self.cfg.path("work", "x.jpg")
        p.write_bytes(b"j")
        with mock.patch.object(t, "_call", return_value={}) as c:
            t.send_photo(p, "cap")
        self.assertEqual(c.call_args.args[0], "sendPhoto")


class CleanupTests(Base):
    def test_recap_files_removed(self):
        self.make_recap()
        work = self.cfg.path("work")
        names = [f"draft_{self.id}.wav", f"draft_{self.id}_preview.mp4", f"draft_{self.id}_thumb.jpg",
                 f"draft_{self.id}_music.mp4"]
        for n in names:
            (work / n).write_bytes(b"x")
        old = "2000-01-01T00:00:00"
        conn = self.db.connect(self.cfg)
        conn.execute("UPDATE drafts SET status='published', updated_at=? WHERE id=?", (old, self.id))
        conn.commit()
        conn.close()
        self.pipeline.cleanup(self.cfg)
        for n in names:
            self.assertFalse((work / n).exists(), n)
        self.assertFalse(self.video.exists())


class ScriptTests(Base):
    def _run(self, argv, lock=None, run=None):
        mod = _load_script("recap")
        import types
        recap = types.ModuleType("cybershorts.recap")  # real module is written elsewhere; always mock
        lock = lock or mock.MagicMock()
        run = run or mock.Mock(return_value=1)
        recap.run = run
        with mock.patch("cybershorts.pipeline.draft_lock", return_value=lock) as dl, \
             mock.patch.dict(sys.modules, {"cybershorts.recap": recap}), \
             mock.patch.object(__import__("cybershorts"), "recap", recap, create=True), \
             mock.patch("cybershorts.pipeline._notify") as nt, \
             mock.patch("cybershorts.config.load", return_value=self.cfg):
            rc = mod.main(argv)
        return rc, dl, run, nt

    def test_timer_blocking_with_timeout(self):
        rc, dl, run, _ = self._run([])
        self.assertEqual(rc, 0)
        self.assertEqual(dl.call_args.kwargs["blocking"], True)
        self.assertEqual(dl.call_args.kwargs["timeout"], float(self.cfg.get("recap.lock_timeout")))
        self.assertEqual(run.call_args.kwargs, {"force": False, "on_demand": False})

    def test_force_flag(self):
        _, _, run, _ = self._run(["--on-demand", "--force"])
        self.assertEqual(run.call_args.kwargs, {"force": True, "on_demand": True})

    def _busy_lock(self):
        lock = mock.MagicMock()
        lock.__enter__.side_effect = self.pipeline.Busy("x")
        return lock

    def test_timer_busy_exit_1(self):
        rc, _, run, nt = self._run([], lock=self._busy_lock())
        self.assertEqual(rc, 1)
        run.assert_not_called()

    def test_on_demand_busy_notifies_exit_0(self):
        rc, dl, _, nt = self._run(["--on-demand"], lock=self._busy_lock())
        self.assertEqual(rc, 0)
        self.assertEqual(dl.call_args.kwargs["blocking"], False)
        self.assertIn("/recap later", nt.call_args.args[1])

    def test_exception_exit_1(self):
        rc, *_ = self._run([], run=mock.Mock(side_effect=RuntimeError("boom")))
        self.assertEqual(rc, 1)


class AnalyticsTests(Base):
    def test_learnings_exclude_recaps(self):
        from cybershorts import analytics
        conn = self.db.connect(self.cfg)
        self.db.init(conn)
        old = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 5 * 86400))
        ids = []
        for n in range(7):
            kind = "recap" if n == 0 else "regular"
            i = self.db.create_draft(conn, f"u{n}", "published", json.dumps({"youtube_title": f"t{n}"}), kind=kind)
            conn.execute("UPDATE drafts SET youtube_id=?, updated_at=?, created_at=? WHERE id=?",
                         (f"yt{n}", old, old, i))
            ids.append(f"yt{n}")
        conn.commit()
        conn.close()
        stats = {i: {"views": 100 * k, "likes": 1, "comments": 0} for k, i in enumerate(ids)}
        seen = []
        chat = mock.Mock(side_effect=lambda cfg, msgs, **kw: (seen.append(msgs[1]["content"]) or "- a bullet"))
        with mock.patch("cybershorts.youtube.has_scope", return_value=True), \
             mock.patch("cybershorts.youtube.video_stats", return_value=stats), \
             mock.patch("cybershorts.writer.chat", chat):
            res = analytics._run(self.cfg, telegram=False)
        # 7 videos tracked, 6 Shorts: still meets min_videos=6
        self.assertEqual(res["videos"], 7)
        self.assertTrue(res["learnings"])
        self.assertNotIn("title: t0", seen[0])
        conn = self.db.connect(self.cfg)
        n = conn.execute("SELECT COUNT(*) FROM video_stats").fetchone()[0]
        conn.close()
        self.assertEqual(n, 7)

    def test_too_few_shorts_no_learnings(self):
        from cybershorts import analytics
        conn = self.db.connect(self.cfg)
        self.db.init(conn)
        old = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 5 * 86400))
        stats = {}
        for n in range(6):
            kind = "recap" if n < 2 else "regular"
            i = self.db.create_draft(conn, f"u{n}", "published", "{}", kind=kind)
            conn.execute("UPDATE drafts SET youtube_id=?, updated_at=?, created_at=? WHERE id=?",
                         (f"yt{n}", old, old, i))
            stats[f"yt{n}"] = {"views": n, "likes": 0, "comments": 0}
        conn.commit()
        conn.close()
        chat = mock.Mock(return_value="- x")
        with mock.patch("cybershorts.youtube.has_scope", return_value=True), \
             mock.patch("cybershorts.youtube.video_stats", return_value=stats), \
             mock.patch("cybershorts.writer.chat", chat):
            res = analytics._run(self.cfg, telegram=False)
        self.assertFalse(res["learnings"])
        chat.assert_not_called()


if __name__ == "__main__":
    unittest.main()
