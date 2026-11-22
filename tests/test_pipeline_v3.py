import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


def _data(**kw):
    d = {"on_screen_title": "Title", "script": "Hook one.\n\nBody.\n\nFollow for daily cyber news.",
         "youtube_title": "T #Shorts", "description": "Desc one. Source: http://x/a",
         "hashtags": ["a", "b", "c"], "keywords": ["k"], "pexels_query": "server"}
    d.update(kw)
    return d


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("APP_DIR")
        os.environ["APP_DIR"] = self._tmp.name
        from cybershorts import config, db, pipeline

        self.db, self.pipeline = db, pipeline
        self.cfg = config.load()
        self.cfg.telegram_bot_token = ""
        self.cfg.telegram_chat_id = ""
        self.cfg.raw["news"]["fetch_full_text"] = False
        self.story = {"title": "Story", "summary": "S", "url": "http://x/a", "source": "Src",
                      "published": None}

    def tearDown(self):
        if self._old is None:
            os.environ.pop("APP_DIR", None)
        else:
            os.environ["APP_DIR"] = self._old
        self._tmp.cleanup()

    def row(self, i):
        conn = self.db.connect(self.cfg)
        try:
            return self.db.get_draft(conn, i)
        finally:
            conn.close()

    def new_draft(self, data, status="rendered", **fields):
        conn = self.db.connect(self.cfg)
        self.db.init(conn)
        i = self.db.create_draft(conn, "http://x/a", status, json.dumps(data))
        if fields:
            self.db.update_draft(conn, i, **fields)
        conn.close()
        return i


class MakeDraftTests(Base):
    def run_story(self, data, *, hooks_pick=0, kind="regular", kev=None, card=None, story=None):
        synth = mock.Mock()
        with mock.patch.object(self.pipeline.writer, "write_script", return_value=data) as ws, \
             mock.patch.object(self.pipeline.writer, "pick_hook", return_value=hooks_pick) as ph, \
             mock.patch.object(self.pipeline.writer, "fact_check", return_value={"ok": True, "issues": []}), \
             mock.patch("cybershorts.cvedata.build_card", return_value=card) as bc, \
             mock.patch("cybershorts.tts.ensure_voice"), mock.patch("cybershorts.tts.synthesize", synth), \
             mock.patch.object(self.pipeline, "render_draft") as rd:
            did = self.pipeline.make_draft_for_story(self.cfg, story or self.story, kind=kind, kev=kev)
        self.ws, self.ph, self.bc, self.synth, self.rd = ws, ph, bc, synth, rd
        return did

    def test_only_relevant_cves_marked_used(self):
        story = dict(self.story, title="Flaw CVE-2026-1111 and cve-2026-2222", summary="also CVE-2026-3333",
                     article="see CVE-2026-4444")
        data = _data(description="Covers CVE-2026-5555. Source: http://x/a", data_card={"cve_id": "CVE-2026-6666"})
        self.run_story(data, story=story, kev={"cveID": "CVE-2026-7777"}, card={"cve_id": "CVE-2026-6666"})
        conn = self.db.connect(self.cfg)
        try:
            for c in ("CVE-2026-1111", "CVE-2026-2222", "CVE-2026-5555", "CVE-2026-6666", "CVE-2026-7777"):
                self.assertTrue(self.db.cve_used(conn, c), c)
            for c in ("CVE-2026-3333", "CVE-2026-4444"):  # passing mentions in summary/article
                self.assertFalse(self.db.cve_used(conn, c), c)
        finally:
            conn.close()

    def test_voice_script_goes_only_to_tts(self):
        data = _data(voice_script="[curious] Hook one.\n\nBody.\n\nFollow for daily cyber news.")
        did = self.run_story(data)
        self.assertEqual(self.synth.call_args.args[1], data["voice_script"])
        saved = json.loads(self.row(did)["data_json"])
        self.assertNotIn("[", saved["script"])
        self.rd.assert_called_once()

    def test_plain_script_to_tts_when_no_voice_script(self):
        did = self.run_story(_data())
        self.assertEqual(self.synth.call_args.args[1], _data()["script"])
        self.assertTrue(self.row(did))

    def test_hook_swap_changes_script_and_voice_script(self):
        data = _data(hooks=["Hook one.", "Better hook."],
                     voice_script="[curious] Hook one.\n\nBody.\n\nFollow for daily cyber news.")
        did = self.run_story(data, hooks_pick=1)
        saved = json.loads(self.row(did)["data_json"])
        self.assertEqual(saved["script"], "Better hook.\n\nBody.\n\nFollow for daily cyber news.")
        self.assertEqual(saved["voice_script"], "Better hook.\n\nBody.\n\nFollow for daily cyber news.")
        self.assertEqual(saved["hook_index"], 1)
        self.assertEqual(self.synth.call_args.args[1], saved["voice_script"])

    def test_hook_zero_keeps_script(self):
        did = self.run_story(_data(hooks=["Hook one.", "Alt."]), hooks_pick=0)
        saved = json.loads(self.row(did)["data_json"])
        self.assertTrue(saved["script"].startswith("Hook one."))
        self.assertEqual(saved["hook_index"], 0)

    def test_no_hooks_no_pick(self):
        self.run_story(_data())
        self.ph.assert_not_called()

    def test_series_per_week_filters_options(self):
        # Scam of the Week already used within the last week -> not offered
        conn = self.db.connect(self.cfg)
        self.db.init(conn)
        self.db.create_draft(conn, "http://old", "rendered", "{}", series="Scam of the Week")
        conn.close()
        self.run_story(_data())
        names = [o["name"] for o in self.ws.call_args.kwargs["series_options"]]
        self.assertNotIn("Scam of the Week", names)
        self.assertIn("Patch This Now", names)

    def test_series_offered_when_not_used_or_rejected(self):
        conn = self.db.connect(self.cfg)
        self.db.init(conn)
        self.db.create_draft(conn, "http://old", "rejected", "{}", series="Scam of the Week")
        conn.close()
        self.run_story(_data())
        names = [o["name"] for o in self.ws.call_args.kwargs["series_options"]]
        self.assertIn("Scam of the Week", names)

    def test_count_drafts_since_called_with_week_window(self):
        with mock.patch.object(self.db, "count_drafts_since", return_value=5) as c:
            self.run_story(_data())
        self.assertTrue(any(k.kwargs.get("series") == "Scam of the Week" for k in c.call_args_list))
        # only capped series are counted; per_week=1 and count 5 -> excluded
        names = [o["name"] for o in self.ws.call_args.kwargs["series_options"]]
        self.assertNotIn("Scam of the Week", names)

    def test_series_off_means_no_options(self):
        self.cfg.raw["series"]["enabled"] = False
        self.run_story(_data())
        self.assertNotIn("series_options", self.ws.call_args.kwargs)
        self.assertNotIn("breaking", self.ws.call_args.kwargs)

    def test_series_saved_on_row(self):
        did = self.run_story(_data(series="Patch This Now"))
        r = self.row(did)
        self.assertEqual(r["series"], "Patch This Now")
        self.assertEqual(r["kind"], "regular")

    def test_breaking_kind_prefix_and_flag(self):
        did = self.run_story(_data(), kind="breaking", kev={"cveID": "CVE-2026-9999"})
        r = self.row(did)
        saved = json.loads(r["data_json"])
        self.assertEqual(r["kind"], "breaking")
        self.assertTrue(saved["breaking"])
        self.assertTrue(saved["youtube_title"].startswith("🚨 "))
        self.assertTrue(saved["youtube_title"].endswith("#Shorts"))
        self.assertTrue(self.ws.call_args.kwargs["breaking"])
        conn = self.db.connect(self.cfg)
        try:
            self.assertTrue(self.db.cve_used(conn, "CVE-2026-9999"))
        finally:
            conn.close()

    def test_long_breaking_title_stays_under_limit(self):
        did = self.run_story(_data(youtube_title="x" * 80 + " #Shorts"), kind="breaking")
        self.assertLessEqual(len(json.loads(self.row(did)["data_json"])["youtube_title"]), 95)

    def test_data_card_confirmed_and_marked_used(self):
        card = {"cve_id": "CVE-2026-1234", "cvss": 9.8}
        did = self.run_story(_data(data_card={"cve_id": "CVE-2026-1234", "cvss": 1}), card=card)
        saved = json.loads(self.row(did)["data_json"])
        self.assertEqual(saved["data_card"], card)
        self.bc.assert_called_once()
        conn = self.db.connect(self.cfg)
        try:
            self.assertTrue(self.db.cve_used(conn, "CVE-2026-1234"))
        finally:
            conn.close()

    def test_unconfirmed_card_dropped(self):
        did = self.run_story(_data(data_card={"cve_id": "CVE-2026-1234"}), card=None)
        self.assertNotIn("data_card", json.loads(self.row(did)["data_json"]))

    def test_build_card_not_called_without_raw_or_when_disabled(self):
        self.run_story(_data())
        self.bc.assert_not_called()
        self.cfg.raw["data_card"]["enabled"] = False
        self.run_story(_data(data_card={"cve_id": "CVE-2026-1234"}))
        self.bc.assert_not_called()

    def test_build_card_error_does_not_fail_draft(self):
        with mock.patch("cybershorts.cvedata.build_card", side_effect=RuntimeError("boom")):
            data = _data(data_card={"cve_id": "CVE-2026-1234"})
            with mock.patch.object(self.pipeline.writer, "write_script", return_value=data), \
                 mock.patch.object(self.pipeline.writer, "fact_check", return_value={"ok": True, "issues": []}), \
                 mock.patch("cybershorts.tts.ensure_voice"), mock.patch("cybershorts.tts.synthesize"), \
                 mock.patch.object(self.pipeline, "render_draft"):
                did = self.pipeline.make_draft_for_story(self.cfg, self.story)
        self.assertNotIn("data_card", json.loads(self.row(did)["data_json"]))

    def test_make_draft_picks_story_and_delegates(self):
        with mock.patch.object(self.pipeline.news, "fetch_candidates", return_value=[self.story]), \
             mock.patch.object(self.pipeline.writer, "pick_story", return_value=0), \
             mock.patch.object(self.pipeline, "make_draft_for_story", return_value=7) as m:
            self.assertEqual(self.pipeline.make_draft(self.cfg), 7)
        m.assert_called_once_with(self.cfg, self.story)

    def test_make_draft_no_candidates(self):
        with mock.patch.object(self.pipeline.news, "fetch_candidates", return_value=[]):
            self.assertIsNone(self.pipeline.make_draft(self.cfg))


class SwapHookBase(Base):
    def setUp(self):
        super().setUp()
        self.data = _data(hooks=["Hook one.", "Alt two.", "Alt three."], hook_index=0,
                          voice_script="[curious] Hook one.\n\nBody.\n\nFollow for daily cyber news.")
        work = self.cfg.path("work")
        self.wav = work / "draft_1.wav"
        self.wav.write_bytes(b"old")
        (work / "draft_1.spans.json").write_text("[[0,1]]")
        self.did = self.new_draft(self.data, audio_path=str(self.wav))

    def swap(self, n, synth=None):
        def fake_synth(cfg, text, out):
            Path(out).write_bytes(b"new")
            Path(out).with_name(Path(out).stem + ".spans.json").write_text("[[0,2]]")
            self.texts.append(text)
            return out

        self.texts = []
        with mock.patch("cybershorts.tts.ensure_voice"), \
             mock.patch("cybershorts.tts.synthesize", synth or fake_synth), \
             mock.patch.object(self.pipeline, "render_draft", return_value=Path("/x.mp4")) as rd:
            out = self.pipeline.swap_hook(self.cfg, self.did, n)
        self.rd = rd
        return out


class SwapHookTests(SwapHookBase):
    def test_swap_replaces_first_paragraph_and_retts(self):
        self.swap(2)
        saved = json.loads(self.row(self.did)["data_json"])
        self.assertEqual(saved["script"], "Alt two.\n\nBody.\n\nFollow for daily cyber news.")
        self.assertEqual(saved["voice_script"], "Alt two.\n\nBody.\n\nFollow for daily cyber news.")
        self.assertEqual(saved["hook_index"], 1)
        self.assertEqual(self.texts, [saved["voice_script"]])
        self.assertEqual(self.wav.read_bytes(), b"new")
        self.assertEqual((self.wav.with_name("draft_1.spans.json")).read_text(), "[[0,2]]")
        self.assertFalse((self.wav.parent / "draft_1_swap.wav").exists())
        self.rd.assert_called_once_with(self.cfg, self.did, fail_on_error=False)

    def test_swap_back_to_original(self):
        self.swap(2)
        self.swap(1)
        saved = json.loads(self.row(self.did)["data_json"])
        self.assertTrue(saved["script"].startswith("Hook one."))
        self.assertEqual(saved["hook_index"], 0)

    def test_only_rendered(self):
        self.db.update_draft(self.db.connect(self.cfg), self.did, status="published")
        with self.assertRaises(ValueError):
            self.swap(2)

    def test_only_tts_mode(self):
        self.cfg.raw["voice"]["mode"] = "record"
        with self.assertRaises(ValueError):
            self.swap(2)

    def test_bad_number(self):
        for n in (0, 4):
            with self.assertRaises(ValueError):
                self.swap(n)

    def test_tts_failure_leaves_old_audio_and_data(self):
        with self.assertRaises(RuntimeError):
            self.swap(2, synth=mock.Mock(side_effect=RuntimeError("tts down")))
        self.assertEqual(self.wav.read_bytes(), b"old")
        self.assertEqual(json.loads(self.row(self.did)["data_json"])["hook_index"], 0)
        self.assertEqual(self.row(self.did)["status"], "rendered")

    def test_render_failure_restores_everything(self):
        work, videos = self.cfg.path("work"), self.cfg.path("videos")
        mp4 = videos / "draft_1.mp4"
        mp4.write_bytes(b"oldvideo")
        music = work / "draft_1_music.mp4"
        music.write_bytes(b"oldmusic")
        old_json = self.row(self.did)["data_json"]

        def fake_synth(cfg, text, out):
            Path(out).write_bytes(b"new")
            Path(out).with_name(Path(out).stem + ".spans.json").write_text("[[0,2]]")
            return out

        def boom(cfg, did, fail_on_error=True):
            mp4.write_bytes(b"partial")  # render overwrote the video before failing
            raise RuntimeError("telegram down")

        with mock.patch("cybershorts.tts.ensure_voice"), mock.patch("cybershorts.tts.synthesize", fake_synth), \
             mock.patch.object(self.pipeline, "render_draft", side_effect=boom):
            with self.assertRaises(RuntimeError):
                self.pipeline.swap_hook(self.cfg, self.did, 2)
        self.assertEqual(self.wav.read_bytes(), b"old")
        self.assertEqual((work / "draft_1.spans.json").read_text(), "[[0,1]]")
        self.assertEqual(mp4.read_bytes(), b"oldvideo")
        self.assertEqual(music.read_bytes(), b"oldmusic")
        self.assertEqual(list(work.glob("*.swapbak")) + list(videos.glob("*.swapbak")), [])
        row = self.row(self.did)
        self.assertEqual(row["status"], "rendered")
        self.assertEqual(row["data_json"], old_json)

    def test_render_failure_does_not_mark_failed_via_real_render_draft(self):
        # real render_draft with fail_on_error=False must not set status=failed
        with mock.patch.object(self.pipeline, "_fail") as fail, \
             mock.patch.object(self.pipeline, "_draft_data", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.pipeline.render_draft(self.cfg, self.did, fail_on_error=False)
        fail.assert_not_called()
        self.assertEqual(self.row(self.did)["status"], "rendered")


class PublishCommentTests(Base):
    def setUp(self):
        super().setUp()
        self.video = self.cfg.path("videos") / "draft_1.mp4"
        self.video.write_bytes(b"fake")
        self.did = self.new_draft(_data(first_comment="Patched yet?"), video_path=str(self.video))

    def publish(self, comment):
        with mock.patch("cybershorts.youtube.upload", return_value="YT1"), \
             mock.patch("cybershorts.youtube.post_comment", comment) as _:
            return self.pipeline.publish(self.cfg, self.did, {"yt"})

    def test_success_has_pin_hint_and_saves_id(self):
        pc = mock.Mock(return_value=("C1", "ok"))
        out = self.publish(pc)
        pc.assert_called_once_with(self.cfg, "YT1", "Patched yet?")
        self.assertIn("Pin it in the YouTube app", out)
        self.assertIn("published", out)
        r = self.row(self.did)
        self.assertEqual(r["yt_comment_id"], "C1")
        self.assertEqual(r["status"], "published")

    def test_failure_does_not_change_result(self):
        out = self.publish(mock.Mock(return_value=(None, "token lacks scope")))
        self.assertIn("Comment skipped: token lacks scope", out)
        self.assertEqual(self.row(self.did)["status"], "published")
        self.assertIsNone(self.row(self.did)["yt_comment_id"])

    def test_exception_does_not_change_result(self):
        out = self.publish(mock.Mock(side_effect=RuntimeError("kaboom")))
        self.assertIn("Comment skipped", out)
        r = self.row(self.did)
        self.assertEqual(r["status"], "published")
        self.assertEqual(r["youtube_id"], "YT1")

    def test_disabled_or_empty_posts_nothing(self):
        pc = mock.Mock(return_value=("C1", "ok"))
        self.cfg.raw["youtube"]["first_comment"] = False
        out = self.publish(pc)
        pc.assert_not_called()
        self.assertNotIn("comment", out.lower())

    def test_no_comment_text_posts_nothing(self):
        d = self.new_draft(_data(), video_path=str(self.video))
        pc = mock.Mock()
        with mock.patch("cybershorts.youtube.upload", return_value="YT2"), \
             mock.patch("cybershorts.youtube.post_comment", pc):
            self.pipeline.publish(self.cfg, d, {"yt"})
        pc.assert_not_called()

    def test_retry_does_not_repost(self):
        self.publish(mock.Mock(return_value=("C1", "ok")))
        pc = mock.Mock()
        with mock.patch("cybershorts.youtube.post_comment", pc):
            self.db.update_draft(self.db.connect(self.cfg), self.did, status="partial")
            self.pipeline.publish(self.cfg, self.did, {"yt"})
        pc.assert_not_called()


class CaptionTests(Base):
    def test_length_cap_with_everything(self):
        data = _data(script="Sentence here. " * 40, breaking=True, series="Patch This Now",
                     hooks=["h" * 200, "Alt two " * 30, "Alt three " * 30], hook_index=0,
                     description="d" * 300,
                     fact_check={"ok": False, "issues": ["issue " * 20] * 5})
        cap = self.pipeline._telegram_caption(data, "http://example.com/" + "u" * 100, 12)
        self.assertLessEqual(len(cap), 1024)
        self.assertIn("🚨 BREAKING", cap)
        self.assertIn("📺 Patch This Now", cap)
        self.assertIn("TikTok caption (tap to copy): <code>", cap)

    def test_alt_hooks_listed_and_truncated(self):
        data = _data(hooks=["Hook one.", "A" * 200, "Third hook."], hook_index=0)
        cap = self.pipeline._telegram_caption(data, "http://x", 1)
        self.assertIn("🪝 Alt hooks: 2) ", cap)
        self.assertIn("3) Third hook.", cap)
        line = [ln for ln in cap.split("\n") if ln.startswith("🪝")][0]
        self.assertIn("…", line)

    def test_active_alt_hook_offers_original(self):
        data = _data(hooks=["Hook one.", "Alt two.", "Alt three."], hook_index=1)
        self.assertEqual(self.pipeline._alt_hook_numbers(data), [1, 3])

    def test_trim_order_preview_then_alt_then_tiktok_then_issues(self):
        data = _data(script="S" * 300, hooks=["H", "a" * 90, "b" * 90], hook_index=0,
                     description="D" * 400, fact_check={"ok": False, "issues": ["i" * 100] * 3})
        cap = self.pipeline._telegram_caption(data, "http://x", 1)
        self.assertLessEqual(len(cap), 1024)
        # the preview absorbs the trimming first: alt hooks, TikTok caption and issues stay whole
        self.assertIn("a" * 90, cap)
        self.assertIn("b" * 90, cap)
        self.assertIn("D" * 100, cap)
        self.assertEqual(cap.count("i" * 100), 3)
        # with far more text, alt hooks shrink before the TikTok caption does
        data2 = _data(script="S" * 300, hooks=["H", "a" * 90, "b" * 90], hook_index=0,
                      description="D" * 400, fact_check={"ok": False, "issues": ["i" * 130] * 5})
        cap2 = self.pipeline._telegram_caption(data2, "http://x", 1)
        self.assertLessEqual(len(cap2), 1024)
        self.assertNotIn("a" * 90, cap2)
        self.assertIn("D" * 200, cap2)

    def test_tiktok_caption_feature_kept(self):
        data = _data()
        cap = self.pipeline._telegram_caption(data, "http://x", 1)
        self.assertIn("<code>Desc one. #a #b #c</code>", cap)
        self.assertEqual(self.pipeline.tiktok_caption(data), "Desc one. #a #b #c")

    def test_plain_caption_unchanged_when_features_off(self):
        cap = self.pipeline._telegram_caption(_data(), "http://x", 3)
        self.assertNotIn("BREAKING", cap)
        self.assertNotIn("Alt hooks", cap)
        self.assertNotIn("📺", cap)


class OverlayAndKeyboardTests(Base):
    def test_overlay_badge_breaking_wins_over_series(self):
        kw = self.pipeline._overlay_kwargs(self.cfg, _data(breaking=True, series="Patch This Now"))
        self.assertEqual(kw["badge"], "BREAKING")
        self.assertEqual(kw["badge_color"], "#FF3B30")

    def test_overlay_series_badge_and_card(self):
        card = {"cve_id": "CVE-2026-1"}
        kw = self.pipeline._overlay_kwargs(self.cfg, _data(series="Scam of the Week", data_card=card))
        self.assertEqual(kw["badge"], "SCAM OF THE WEEK")
        self.assertEqual(kw["card"], card)
        self.cfg.raw["data_card"]["enabled"] = False
        self.assertNotIn("card", self.pipeline._overlay_kwargs(self.cfg, _data(data_card=card)))

    def test_overlay_empty_for_plain_draft(self):
        self.assertEqual(self.pipeline._overlay_kwargs(self.cfg, _data()), {})

    def test_render_passes_overlay_kwargs_to_write_ass(self):
        wav = self.cfg.path("work") / "draft_1.wav"
        wav.write_bytes(b"w")
        did = self.new_draft(_data(series="Patch This Now", data_card={"cve_id": "CVE-2026-1"},
                                   hooks=["a", "b", "c"], hook_index=0),
                             audio_path=str(wav))
        from cybershorts import captions, render

        tg = mock.Mock()
        tg.send_video.return_value = {"result": {"message_id": 5}}
        out = self.cfg.path("videos") / f"draft_{did}.mp4"
        out.write_bytes(b"v")
        words = [(0.0, 0.5, "hi"), (0.5, 1.0, "there")]
        with mock.patch.object(captions, "transcribe_words", return_value=words), \
             mock.patch.object(captions, "write_ass") as wa, \
             mock.patch.object(render, "probe_duration", return_value=1.0), \
             mock.patch.object(render, "render"), \
             mock.patch.dict(self.cfg.raw["broll"], {"provider": "none"}), \
             mock.patch("cybershorts.music.add_music", return_value=(None, None)), \
             mock.patch.object(self.pipeline, "_telegram", return_value=tg):
            self.pipeline.render_draft(self.cfg, did)
        self.assertEqual(wa.call_args.kwargs["badge"], "PATCH THIS NOW")
        self.assertEqual(wa.call_args.kwargs["card"], {"cve_id": "CVE-2026-1"})
        kb = tg.send_video.call_args.args[2]
        cbs = [b["callback_data"] for row in kb["inline_keyboard"] for b in row]
        self.assertIn(f"hook:2:{did}", cbs)
        self.assertIn(f"hook:3:{did}", cbs)
        self.assertNotIn(f"hook:1:{did}", cbs)

    def test_keyboard_hook_row_above_reject(self):
        from cybershorts.telegram import keyboard

        kb = keyboard(5, alt_hooks=[2, 3])
        rows = kb["inline_keyboard"]
        self.assertEqual([b["callback_data"] for b in rows[-2]], ["hook:2:5", "hook:3:5"])
        self.assertEqual(rows[-1][0]["callback_data"], "rej:5")
        self.assertEqual(rows[-2][0]["text"], "🪝 Use hook 2")
        self.assertEqual([b["callback_data"] for b in keyboard(5, alt_hooks=[1, 3])["inline_keyboard"][-2]],
                         ["hook:1:5", "hook:3:5"])
        plain = [b["callback_data"] for row in keyboard(5)["inline_keyboard"] for b in row]
        self.assertFalse(any(c.startswith("hook:") for c in plain))


class LockTests(Base):
    def test_nonblocking_busy_while_held(self):
        with self.pipeline.draft_lock(self.cfg):
            with self.assertRaises(self.pipeline.Busy):
                with self.pipeline.draft_lock(self.cfg, blocking=False):
                    pass
        with self.pipeline.draft_lock(self.cfg, blocking=False):  # released
            pass

    def test_timeout(self):
        with self.pipeline.draft_lock(self.cfg):
            t = time.monotonic()
            with self.assertRaises(self.pipeline.Busy):
                with self.pipeline.draft_lock(self.cfg, blocking=True, timeout=0.2):
                    pass
            self.assertLess(time.monotonic() - t, 3)

    def test_short_error_scrubs_new_keys(self):
        self.cfg.elevenlabs_api_key = "ELKEY123"
        self.cfg.nvd_api_key = "NVDKEY456"
        msg = self.pipeline._short_error(self.cfg, RuntimeError("bad ELKEY123 and NVDKEY456"))
        self.assertNotIn("ELKEY123", msg)
        self.assertNotIn("NVDKEY456", msg)


class EntryPointTests(unittest.TestCase):
    def test_bot_and_make_draft_use_lock(self):
        root = Path(__file__).resolve().parents[1]
        bot = (root / "scripts" / "bot.py").read_text()
        self.assertIn('action == "hook"', bot)
        self.assertIn("draft_lock", bot)
        self.assertEqual(bot.count("pipeline.draft_lock("), 1)  # /draft; hook swap locks in press_hook
        self.assertIn("press_hook", bot)
        self.assertIn("draft_lock(", (root / "cybershorts" / "pipeline.py").read_text())
        self.assertIn("draft_lock", (root / "scripts" / "make_draft.py").read_text())


class HookRegressionTests(SwapHookBase):
    def test_replace_first_paragraph_uses_beat_rule(self):
        self.assertEqual(self.pipeline._replace_first_paragraph("A line.\nB line.\nC line.", "X hook."),
                         "X hook.\n\nB line.\n\nC line.")

    def test_swap_on_single_newline_draft_keeps_body(self):
        data = _data(script="Hook one.\nBody.\nFollow.", hooks=["Hook one.", "Alt two."], hook_index=0)
        self.db.update_draft(self.db.connect(self.cfg), self.did, data_json=json.dumps(data))
        self.swap(2)
        saved = json.loads(self.row(self.did)["data_json"])
        self.assertEqual(saved["script"], "Alt two.\n\nBody.\n\nFollow.")

    def test_swap_same_hook_is_noop(self):
        msg = self.swap(1)  # hook_index is already 0
        self.assertIsInstance(msg, str)
        self.assertEqual(self.texts, [])
        self.rd.assert_not_called()
        self.assertEqual(self.wav.read_bytes(), b"old")
        self.swap(2)
        again = self.swap(2)
        self.assertIsInstance(again, str)
        self.assertEqual(self.rd.call_count, 0)


class PressHookTests(SwapHookBase):
    def setUp(self):
        super().setUp()
        self.db.update_draft(self.db.connect(self.cfg), self.did, tg_message_id=77)
        self.tg = mock.Mock()

    def press(self, n=2, msg_id=77, timeout=0.2):
        with mock.patch("cybershorts.tts.ensure_voice"), \
             mock.patch("cybershorts.tts.synthesize", side_effect=lambda c, t, o: Path(o).write_bytes(b"n")), \
             mock.patch.object(self.pipeline, "render_draft", return_value=Path("/x.mp4")) as rd:
            out = self.pipeline.press_hook(self.cfg, self.tg, self.did, n, msg_id, lock_timeout=timeout)
        self.rd = rd
        return out

    def test_stale_message_ignored(self):
        out = self.press(msg_id=12)
        self.assertIn("out of date", out)
        self.rd.assert_not_called()
        self.tg.edit_reply_markup.assert_not_called()

    def test_success_removes_keyboard(self):
        self.press()
        self.rd.assert_called_once()
        self.tg.edit_reply_markup.assert_called_once_with(77, None)

    def test_busy_restores_keyboard_with_alt_hooks(self):
        with self.pipeline.draft_lock(self.cfg):
            out = self.press()
        self.assertIn("still running", out)
        self.rd.assert_not_called()
        calls = self.tg.edit_reply_markup.call_args_list
        self.assertEqual(calls[0].args, (77, None))
        kb = calls[-1].args[1]
        cbs = [b["callback_data"] for row in kb["inline_keyboard"] for b in row]
        self.assertIn(f"hook:2:{self.did}", cbs)
        self.assertIn(f"hook:3:{self.did}", cbs)

    def test_swap_error_restores_keyboard(self):
        with mock.patch.object(self.pipeline, "swap_hook", side_effect=RuntimeError("boom")):
            out = self.press()
        self.assertIn("failed", out)
        kb = self.tg.edit_reply_markup.call_args_list[-1].args[1]
        self.assertTrue(kb["inline_keyboard"])

    def test_noop_swap_restores_keyboard(self):
        out = self.press(n=1)
        self.assertIn("already uses", out)
        self.assertTrue(self.tg.edit_reply_markup.call_args_list[-1].args[1]["inline_keyboard"])

    def test_bot_uses_short_lock_timeouts(self):
        bot = (Path(__file__).resolve().parents[1] / "scripts" / "bot.py").read_text()
        self.assertNotIn("1800", bot)
        self.assertIn("A draft is already being made", bot)


class BotStartupTests(unittest.TestCase):
    def test_bot_runs_db_init_before_polling(self):
        import importlib.util

        tmp = tempfile.TemporaryDirectory()
        old = os.environ.get("APP_DIR")
        os.environ["APP_DIR"] = tmp.name
        try:
            from cybershorts import config

            cfg = config.load()
            cfg.telegram_bot_token, cfg.telegram_chat_id = "tok", "1"
            order = []
            tg = mock.Mock()
            tg.get_updates.side_effect = lambda *a, **k: (order.append("poll"), (_ for _ in ()).throw(KeyboardInterrupt))
            spec = importlib.util.spec_from_file_location(
                "bot_under_test", Path(__file__).resolve().parents[1] / "scripts" / "bot.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            with mock.patch("cybershorts.config.load", return_value=cfg), \
                 mock.patch("cybershorts.telegram.Telegram", return_value=tg), \
                 mock.patch("cybershorts.pipeline.stuck_publishing", return_value=[]), \
                 mock.patch("cybershorts.db.init", side_effect=lambda conn: order.append("init")) as init:
                with self.assertRaises(KeyboardInterrupt):
                    mod.main()
            self.assertEqual(init.call_count, 1)
            self.assertEqual(order, ["init", "poll"])
        finally:
            if old is None:
                os.environ.pop("APP_DIR", None)
            else:
                os.environ["APP_DIR"] = old
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
