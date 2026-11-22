"""Recap newsroom layout wiring in recap.run/_produce (LLM, ffmpeg and recap_newsroom mocked)."""
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from cybershorts import db, recap
from tests.test_recap_script import Base, RunTests, load_raw

VIS_FIX = Path(__file__).parent / "fixtures" / "recap_llm_visuals.json"


class NewsroomWiring(Base):
    LABEL = "On-screen text (check only numbers, names, versions, dates):"
    WEEK = RunTests.WEEK
    fake_select = RunTests.fake_select
    rows = RunTests.rows
    _base_mocks = RunTests.mocks

    def setUp(self):
        super().setUp()
        self.cfg.raw["recap"]["layout"] = "newsroom"
        self.notes = []
        for p in (mock.patch("cybershorts.recap.week_key", return_value=self.WEEK),
                  mock.patch("cybershorts.recap.select_stories", side_effect=self.fake_select),
                  mock.patch("cybershorts.pipeline._notify", side_effect=lambda c, t: self.notes.append(t))):
            p.start()
        self.n_stories = 3

    def tearDown(self):
        mock.patch.stopall()
        super().tearDown()

    def mocks(self, **over):
        m = self._base_mocks(**over)
        work = self.cfg.path("work")
        nr = types.ModuleType("cybershorts.recap_newsroom")
        nr.build_chrome_ass = mock.Mock(return_value="[Script Info]")
        nr.write_newsroom_ass = mock.Mock(return_value=work / "n.ass")
        m["nr"] = nr
        for name in ("render_background", "render_recap_single"):
            setattr(m["render"], name, mock.Mock(return_value=work / "o"))
        mock.patch.dict(sys.modules, {"cybershorts.recap_newsroom": nr}).start()
        import cybershorts
        mock.patch.object(cybershorts, "recap_newsroom", nr, create=True).start()

        script_json = json.dumps(load_raw())
        vis_json = VIS_FIX.read_text()
        self.fail_visuals = False
        self.chat_calls = []

        def chat(cfg, messages, model=None, **kw):
            self.chat_calls.append(messages)
            if "story_index" in messages[0]["content"] + messages[-1]["content"] and \
                    "key_points" in messages[-1]["content"]:
                if self.fail_visuals:
                    raise RuntimeError("visuals llm down")
                return vis_json
            return script_json
        m["chat"].side_effect = chat
        return m

    def data_of(self, did):
        return json.loads(db.get_draft(self.conn, did)["data_json"])

    def test_visuals_failure_still_produces_recap(self):
        m = self.mocks()
        self.fail_visuals = True
        did = recap.run(self.cfg)
        self.assertEqual(db.get_draft(self.conn, did)["status"], "rendered")
        data = self.data_of(did)
        self.assertEqual(len(data["visuals"]), 3)
        self.assertTrue(all(not v["scenes"] and not v["key_points"] for v in data["visuals"]))
        self.assertEqual(data["visuals_stats"], {"scenes": 0, "points": 0, "dropped": 0})
        m["nr"].write_newsroom_ass.assert_called_once()
        vis = m["nr"].write_newsroom_ass.call_args.kwargs["visuals"]
        self.assertEqual(len(vis), 3)

    def test_newsroom_calls(self):
        m = self.mocks()
        self.cfg.raw["recap"]["broll"] = True
        with self.assertLogs("cybershorts", "WARNING") as lg:
            recap.run(self.cfg)
        self.assertEqual(sum("recap.broll" in r for r in lg.output), 1)
        m["broll"].assert_not_called()
        r = m["render"]
        r.render_background.assert_any_call(mock.ANY, mock.ANY, chrome_ass=mock.ANY)
        self.assertEqual(r.render_background.call_count, 2)
        thumb_call = [c for c in r.render_background.call_args_list if c.kwargs.get("w") == 1280][0]
        self.assertEqual(thumb_call.kwargs["h"], 720)
        self.assertNotIn("chrome_ass", thumb_call.kwargs)
        r.render_recap_single.assert_called_once()
        r.render_recap.assert_not_called()
        m["nr"].build_chrome_ass.assert_called_once()
        m["nr"].write_newsroom_ass.assert_called_once()
        m["caps"].write_recap_ass.assert_not_called()
        kw = m["nr"].write_newsroom_ass.call_args.kwargs
        self.assertEqual(kw["para_counts"], self.voice_data["para_counts"])
        self.assertEqual(kw["week_label"], "Week 1")
        self.assertEqual(len(kw["cards"]), 3)
        r.render_thumbnail.assert_called_once()

    def test_visuals_stats_and_stored(self):
        self.mocks()
        did = recap.run(self.cfg)
        data = self.data_of(did)
        st = data["visuals_stats"]
        self.assertEqual(st["points"], sum(len(v["key_points"]) for v in data["visuals"]))
        self.assertEqual(st["scenes"], sum(len(v["scenes"]) for v in data["visuals"]))
        self.assertEqual(st["dropped"], sum(len(v["dropped"]) for v in data["visuals"]))
        self.assertGreater(st["points"] + st["dropped"], 0)
        for k in ("youtube_title", "description", "hashtags", "chapters", "stories", "duration"):
            self.assertIn(k, data)

    def test_fact_check_sees_visual_text(self):
        self.mocks()
        self.cfg.raw["llm"]["fact_check"] = True
        text = "Zebra callout text"
        fake = [{"story_index": i, "keywords": [], "key_points": [{"text": text, "para": 1, "cue": "x"}],
                 "checklist": [], "scenes": [{"type": "stat", "value": "42", "label": "Lonely label"}],
                 "dropped": []} for i in (1, 2, 3)]
        with mock.patch("cybershorts.recap_visuals.write_recap_visuals", return_value=fake), \
                mock.patch("cybershorts.writer.fact_check", return_value={"ok": True}) as fc:
            did = recap.run(self.cfg)
        sent = fc.call_args.args[2]["script"]
        self.assertIn(text, sent)
        self.assertIn("Lonely label", sent)
        self.assertNotIn("stat", sent.split(self.LABEL)[1])
        self.assertTrue(sent.startswith(self.voice_data["script"]))
        self.assertEqual(self.data_of(did)["visuals_stats"], {"scenes": 3, "points": 3, "dropped": 0})

    def test_fact_check_gets_chapters_but_not_dramatizations(self):
        self.mocks()
        self.cfg.raw["llm"]["fact_check"] = True
        fake = [{"story_index": i, "keywords": ["kw%d" % i], "key_points": [], "checklist": [],
                 "scenes": [{"type": "terminal", "title": "ZZTERM", "lines": [{"kind": "cmd", "text": "ZZCMD"}]},
                            {"type": "scam", "channel": "sms", "sender": "ZZSENDER", "body": "ZZBODY {link}",
                             "flags": ["ZZFLAG"]},
                            {"type": "timeline", "events": [{"date": "Nov 12", "label": "ZZEVENT"}]}],
                 "dropped": []} for i in (1, 2, 3)]
        with mock.patch("cybershorts.recap_visuals.write_recap_visuals", return_value=fake), \
                mock.patch("cybershorts.writer.fact_check", return_value={"ok": True}) as fc:
            recap.run(self.cfg)
        sent = fc.call_args.args[2]["script"]
        self.assertIn(self.LABEL, sent)
        tail = sent.split(self.LABEL)[1]
        for word in ("ZZTERM", "ZZCMD", "ZZSENDER", "ZZBODY", "ZZFLAG", "kw1"):
            self.assertNotIn(word, sent)
        self.assertEqual(tail.count("ZZEVENT"), 3)
        for seg in self.voice_data["segments"]:
            self.assertIn(seg["chapter"], tail)

    def test_visual_text_unit(self):
        v = [{"key_points": [{"text": "KP", "para": 1, "cue": "c"}], "checklist": [{"text": "CL"}],
              "scenes": [{"type": "terminal", "lines": [{"kind": "cmd", "text": "T"}]},
                         {"type": "scam", "sender": "S"}, {"type": "stat", "value": "4,000", "label": "L"}]}]
        self.assertEqual(recap.visual_text(v, ["Chap A", None]).split("\n"), ["KP", "CL", "4,000", "L", "Chap A"])
        self.assertEqual(recap.visual_text(v), "KP\nCL\n4,000\nL")

    def test_chrome_file_cleaned_when_ass_writing_fails(self):
        m = self.mocks()
        work = self.cfg.path("work")

        def chrome(*a, **k):
            return "[Script Info]"
        m["nr"].build_chrome_ass.side_effect = chrome
        m["nr"].write_newsroom_ass.side_effect = RuntimeError("ass boom")
        with self.assertRaises(RuntimeError):
            recap.run(self.cfg)
        self.assertEqual(list(work.glob("draft_*_chrome.ass")), [])
        m["send"].assert_not_called()

    def test_classic_broll_false_fetches_nothing(self):
        self.cfg.raw["recap"]["layout"] = "classic"
        for off in (False, "false", "False"):
            m = self.mocks()
            self.cfg.raw["recap"]["broll"] = off
            recap.run(self.cfg)
            m["broll"].assert_not_called()
            m["render"].render_recap.assert_called()
            mock.patch.stopall()
            for p in (mock.patch("cybershorts.recap.week_key", return_value=self.WEEK),
                      mock.patch("cybershorts.recap.select_stories", side_effect=self.fake_select),
                      mock.patch("cybershorts.pipeline._notify", side_effect=lambda c, t: self.notes.append(t))):
                p.start()
            self.conn.execute("DELETE FROM drafts")
            self.conn.commit()
        for on in ("auto", True, "true"):
            m = self.mocks()
            self.cfg.raw["recap"]["broll"] = on
            recap.run(self.cfg, force=True)
            m["broll"].assert_called()
            mock.patch.stopall()
            for p in (mock.patch("cybershorts.recap.week_key", return_value=self.WEEK),
                      mock.patch("cybershorts.recap.select_stories", side_effect=self.fake_select),
                      mock.patch("cybershorts.pipeline._notify", side_effect=lambda c, t: self.notes.append(t))):
                p.start()
            self.conn.execute("DELETE FROM drafts")
            self.conn.commit()

    def test_classic_path_unchanged(self):
        self.cfg.raw["recap"]["layout"] = "classic"
        m = self.mocks()
        recap.run(self.cfg)
        m["render"].render_recap.assert_called_once()
        m["render"].render_recap_single.assert_not_called()
        m["render"].render_background.assert_not_called()
        m["nr"].write_newsroom_ass.assert_not_called()
        m["caps"].write_recap_ass.assert_called_once()
        m["chat"].assert_called()
        self.assertEqual(len(self.chat_calls), 1)  # no visuals call
        m["broll"].assert_called()

    def test_temp_files_cleaned_on_failure(self):
        m = self.mocks()
        work = self.cfg.path("work")
        did_holder = {}

        def bg(cfg, out, **kw):
            Path(out).write_bytes(b"x")
            did_holder["bg"] = Path(out)
            return Path(out)
        m["render"].render_background.side_effect = bg
        m["render"].render_recap_single.side_effect = RuntimeError("ffmpeg died")
        with self.assertRaises(RuntimeError):
            recap.run(self.cfg)
        self.assertFalse(did_holder["bg"].exists())
        self.assertEqual(list(work.glob("draft_*_bg.png")), [])
        m["send"].assert_not_called()




if __name__ == "__main__":
    unittest.main()
