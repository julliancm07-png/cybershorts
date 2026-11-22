import copy
import json
import re
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from cybershorts import config, db, recap, recap_publish, tags

FIX = Path(__file__).parent / "fixtures" / "recap_llm.json"
NOW = datetime(2026, 11, 22, 14, 0, tzinfo=timezone.utc)


def load_raw():
    return json.loads(FIX.read_text())


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = config.Config(Path(self._tmp.name), copy.deepcopy(config.DEFAULTS), {})
        self.cfg.raw["recap"]["target_words"] = [150, 220]
        self.cfg.raw["recap"]["fetch_article"] = False
        self.cfg.raw["recap"]["layout"] = "classic"
        self.cfg.raw["llm"]["fact_check"] = False
        self.conn = db.connect(self.cfg)
        db.init(self.conn)

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def tags_on(self):
        self.cfg.raw["voice"]["engine"] = "elevenlabs"
        self.cfg.raw["voice"]["elevenlabs"]["model"] = "eleven_v3"

    def add(self, n, *, views=None, breaking=False, updated="2026-11-20T10:00:00", url=None, status="published"):
        url = url or f"https://ex.com/{n}"
        data = {"on_screen_title": f"Short {n}", "script": f"Script {n}.", "description": f"Desc {n}",
                "source_title": f"Story {n}"}
        if breaking:
            data["breaking"] = True
        self.conn.execute("INSERT OR IGNORE INTO stories(url,title,source,published,summary,seen_at,used)"
                          " VALUES(?,?,?,?,?,?,1)", (url, f"Story {n}", "Src", None, f"Sum {n}", updated))
        cur = self.conn.execute(
            "INSERT INTO drafts(story_url,status,data_json,youtube_id,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (url, status, json.dumps(data), f"yt{n}", updated, updated))
        did = cur.lastrowid
        if views is not None:
            db.upsert_video_stats(self.conn, f"yt{n}", draft_id=did, views=views)
        self.conn.commit()
        return did


class SelectTests(Base):
    def test_rank_views_then_breaking_then_recency(self):
        self.add(1, views=10)
        self.add(2, views=500)
        self.add(3, views=10, breaking=True)
        self.add(4, views=10, updated="2026-11-21T10:00:00")
        got = [s["title"] for s in recap.select_stories(self.cfg, self.conn, NOW)]
        self.assertEqual(got, ["Story 2", "Story 3", "Story 4", "Story 1"])

    def test_fallback_without_stats(self):
        self.add(1, updated="2026-11-19T10:00:00")
        self.add(2, updated="2026-11-21T10:00:00")
        self.add(3, updated="2026-11-20T10:00:00", breaking=True)
        got = [s["title"] for s in recap.select_stories(self.cfg, self.conn, NOW)]
        self.assertEqual(got, ["Story 3", "Story 2", "Story 1"])

    def test_limit_dedup_and_lookback(self):
        for i in range(1, 8):
            self.add(i, views=i)
        self.add(20, views=1, url="https://ex.com/1")  # duplicate url
        self.add(30, views=999, updated="2026-10-01T00:00:00")  # too old
        got = recap.select_stories(self.cfg, self.conn, NOW)
        self.assertEqual(len(got), 5)
        self.assertEqual(len({s["url"] for s in got}), 5)
        self.assertNotIn("yt30", [s["youtube_id"] for s in got])

    def test_fresh_stats_used_when_scope(self):
        self.add(1, views=1)
        self.add(2, views=2)
        with mock.patch("cybershorts.youtube.has_scope", return_value=True), \
                mock.patch("cybershorts.youtube.video_stats", return_value={"yt1": {"views": 900}}):
            got = recap.select_stories(self.cfg, self.conn, NOW)
        self.assertEqual([s["youtube_id"] for s in got], ["yt1", "yt2"])
        self.assertEqual(got[0]["views"], 900)

    def test_source_from_row_and_domain_fallback(self):
        self.add(1, views=5)
        self.add(2, views=4, url="https://www.news.example.org/a/b")
        self.conn.execute("UPDATE stories SET source=NULL WHERE url=?", ("https://www.news.example.org/a/b",))
        self.conn.commit()
        got = {s["youtube_id"]: s["source"] for s in recap.select_stories(self.cfg, self.conn, NOW)}
        self.assertEqual(got, {"yt1": "Src", "yt2": "news.example.org"})

    def test_week_key(self):
        self.assertEqual(recap.week_key(self.cfg, datetime(2026, 11, 22, 3, 0, tzinfo=timezone.utc)), "2026-W47")
        self.assertEqual(recap.week_key(self.cfg, datetime(2026, 12, 28, 12, 0, tzinfo=timezone.utc)), "2026-W53")


class ValidateTests(Base):
    def stories(self, n=3):
        return [{"title": f"Story {i}"} for i in range(1, n + 1)]

    def test_clean_outputs(self):
        out = recap.validate_recap(load_raw(), self.stories(), self.cfg)
        self.assertEqual(out["youtube_title"], "This Week in Cyber: Three Big Stories")
        self.assertNotIn("#Shorts", out["youtube_title"])
        self.assertEqual(out["thumbnail_text"], "HUGE BREACH THIS WEEK")
        self.assertEqual(out["hashtags"], ["cybersecurity", "infosec", "weeklyrecap", "cyber"])
        self.assertIn("subscribe", out["outro"].lower())
        self.assertNotIn("http", out["script"])
        self.assertEqual(out["first_comment"], "Which story worried you most?")
        self.assertEqual(sum(out["para_counts"]), len(tags.split_paragraphs(out["script"])))
        self.assertEqual(len(out["para_counts"]), 5)

    def test_thumbnail_fallback_and_limits(self):
        raw = load_raw()
        raw["thumbnail_text"] = ""
        self.assertEqual(recap.validate_recap(raw, self.stories(), self.cfg)["thumbnail_text"], "WEEK IN CYBER")
        raw["thumbnail_text"] = "extraordinarily incomprehensible vulnerabilities"
        t = recap.validate_recap(raw, self.stories(), self.cfg)["thumbnail_text"]
        self.assertLessEqual(len(t), 28)

    def test_tags_capped(self):
        raw = load_raw()
        raw["tags"] = [f"tag{i}" for i in range(40)]
        out = recap.validate_recap(raw, self.stories(), self.cfg)
        self.assertLessEqual(len(out["hashtags"]), 15)
        self.assertLessEqual(len(" ".join(out["hashtags"])), 450)

    def test_rejects(self):
        for mut, label in [
            (lambda r: r["segments"].pop(), "count"),
            (lambda r: r["segments"][0].update(story_index=9), "index"),
            (lambda r: r["segments"][1].update(story_index=1), "dup"),
            (lambda r: r["segments"][2].update(narration="  [sighs]  "), "empty"),
            (lambda r: r["segments"][2].pop("story_index"), "missing"),
            (lambda r: r.update(intro=""), "intro"),
        ]:
            raw = load_raw()
            mut(raw)
            with self.assertRaises(ValueError, msg=label):
                recap.validate_recap(raw, self.stories(), self.cfg)

    def test_word_range(self):
        self.cfg.raw["recap"]["target_words"] = [600, 800]
        with self.assertRaises(ValueError):
            recap.validate_recap(load_raw(), self.stories(), self.cfg)
        self.cfg.raw["recap"]["target_words"] = [20, 40]
        with self.assertRaises(ValueError):
            recap.validate_recap(load_raw(), self.stories(), self.cfg)

    def test_tags_stripped_when_unsupported(self):
        out = recap.validate_recap(load_raw(), self.stories(), self.cfg)
        self.assertNotIn("voice_script", out)
        self.assertNotRegex(out["script"], r"\[\w+\]")

    def test_tags_only_in_voice_script_with_shared_budget(self):
        self.tags_on()
        self.cfg.raw["recap"]["max_audio_tags"] = 2
        out = recap.validate_recap(load_raw(), self.stories(), self.cfg)
        self.assertNotRegex(out["script"], r"\[\w+\]")
        voice = out["voice_script"]
        self.assertEqual(len(re.findall(tags.TAG_RE, voice)), 2)
        self.assertEqual(tags.normalize_paragraphs(tags.strip_tags(voice)), out["script"])
        self.assertEqual(sum(out["para_counts"]), len(tags.split_paragraphs(voice)))

    def test_prompt_and_model(self):
        self.tags_on()
        self.cfg.raw["recap"]["model"] = "x/y"
        stories = [{"title": f"Story {i}", "summary": "S", "url": "u", "script": "sc"} for i in (1, 2, 3)]
        with mock.patch("cybershorts.writer.chat", return_value=json.dumps(load_raw())) as ch:
            out = recap.write_recap_script(self.cfg, stories, "2026-W47")
        self.assertEqual(ch.call_args.kwargs["model"], "x/y")
        prompt = ch.call_args.args[1][1]["content"] + ch.call_args.args[1][0]["content"]
        self.assertIn("ONLY", prompt)
        self.assertIn("[curious]", prompt)  # tag instructions present
        self.assertIn("title", out)

    def test_retry_then_fail(self):
        stories = [{"title": f"S{i}"} for i in (1, 2, 3)]
        bad = json.dumps({"segments": []})
        with mock.patch("cybershorts.writer.chat", side_effect=[bad, json.dumps(load_raw())]) as ch:
            recap.write_recap_script(self.cfg, stories, "w")
        self.assertEqual(ch.call_count, 2)
        with mock.patch("cybershorts.writer.chat", return_value=bad):
            with self.assertRaises(RuntimeError):
                recap.write_recap_script(self.cfg, stories, "w")


class RunTests(Base):
    WEEK = "2026-W47"

    def setUp(self):
        super().setUp()
        self.notes = []
        self.patches = [
            mock.patch("cybershorts.recap.week_key", return_value=self.WEEK),
            mock.patch("cybershorts.recap.select_stories", side_effect=self.fake_select),
            mock.patch("cybershorts.pipeline._notify", side_effect=lambda c, t: self.notes.append(t)),
        ]
        for p in self.patches:
            p.start()
        self.n_stories = 3

    def tearDown(self):
        mock.patch.stopall()
        super().tearDown()

    def fake_select(self, cfg, conn, now=None, **kw):
        return [{"draft_id": i, "youtube_id": f"yt{i}", "url": f"https://ex.com/{i}", "title": f"Story {i}",
                 "summary": "S", "script": "x", "description": "d", "breaking": False, "series": "",
                 "data": {}, "views": 0} for i in range(1, self.n_stories + 1)]

    def rows(self):
        return self.conn.execute("SELECT * FROM drafts WHERE kind='recap' ORDER BY id").fetchall()

    def mocks(self, **over):
        """Mock every heavy dependency; returns the dict of mocks."""
        work = self.cfg.path("work")
        m = {}
        m["chat"] = mock.patch("cybershorts.writer.chat", return_value=json.dumps(load_raw())).start()

        def voice(cfg, did, data):
            self.voice_data = data
            return work / f"draft_{did}.wav"
        m["voice"] = mock.patch("cybershorts.pipeline._voice", side_effect=voice).start()
        mock.patch("cybershorts.tts.load_spans", return_value=None).start()
        words = [(i * 0.5, i * 0.5 + 0.4, "w") for i in range(60)]
        mock.patch("cybershorts.captions.transcribe_words", return_value=words).start()
        mock.patch("cybershorts.captions.align_to_script", side_effect=lambda w, s: w).start()
        mock.patch("cybershorts.render.probe_duration", return_value=240.0).start()
        m["broll"] = mock.patch("cybershorts.visuals.fetch_broll_set", return_value=[]).start()
        mock.patch("cybershorts.music.add_music", return_value=(None, "off")).start()
        mod_c = types.ModuleType("cybershorts.recap_captions")
        mod_c.write_recap_ass = mock.Mock(return_value=work / "x.ass")
        mod_c.build_thumbnail_ass = mock.Mock(return_value="[Script Info]")
        mod_r = types.ModuleType("cybershorts.recap_render")
        for name in ("render_recap", "extract_still", "render_thumbnail", "render_preview"):
            setattr(mod_r, name, mock.Mock(return_value=work / "o"))
        m["caps"], m["render"] = mod_c, mod_r
        mock.patch.dict(sys.modules, {"cybershorts.recap_captions": mod_c, "cybershorts.recap_render": mod_r}).start()
        # `from . import x` prefers the package attribute when the real module was imported earlier
        import cybershorts
        mock.patch.object(cybershorts, "recap_captions", mod_c, create=True).start()
        mock.patch.object(cybershorts, "recap_render", mod_r, create=True).start()
        m["send"] = mock.patch.object(recap_publish, "send_review", return_value=777).start()
        return m

    def test_happy_path(self):
        m = self.mocks()
        did = recap.run(self.cfg)
        row = db.get_draft(self.conn, did)
        self.assertEqual(row["kind"], "recap")
        self.assertEqual(row["story_url"], f"recap:{self.WEEK}")
        self.assertEqual(row["status"], "rendered")
        self.assertEqual(row["tg_message_id"], 777)
        self.assertTrue(row["video_path"].endswith(f"draft_{did}.mp4"))
        data = json.loads(row["data_json"])
        for k in ("youtube_title", "description", "hashtags", "first_comment", "week", "duration", "words",
                  "chapters", "thumbnail_text", "script", "stories", "title"):
            self.assertIn(k, data, k)
        self.assertIsInstance(data["hashtags"], list)
        self.assertEqual(data["week"], self.WEEK)
        self.assertAlmostEqual(data["duration"], 240.6, places=1)
        self.assertEqual(data["stories"][0]["youtube_id"], "yt1")
        self.assertIn("Source: https://ex.com/1", data["description"])
        m["send"].assert_called_once()
        args = m["send"].call_args.args
        self.assertEqual(args[1], did)
        self.assertEqual(Path(args[3]).name, f"draft_{did}_preview.mp4")
        self.assertEqual(Path(args[4]).name, f"draft_{did}_thumb.jpg")

    def test_chapters_with_exact_spans(self):
        self.mocks()
        # 5 parts: intro(1) + 3 stories(3 paras each) + outro(1) = 11 paragraphs
        spans = [[i * 20.0, i * 20.0 + 19.0] for i in range(11)]
        mock.patch("cybershorts.tts.load_spans", return_value=spans).start()
        mock.patch("cybershorts.captions.transcribe_by_spans", return_value=[(0, 1, "w")]).start()
        did = recap.run(self.cfg)
        data = json.loads(db.get_draft(self.conn, did)["data_json"])
        self.assertEqual([c[0] for c in data["chapters"]], [0, 20, 80, 140])
        self.assertNotIn("chapters_warning", data)
        self.assertIn("Chapters\n00:00 Intro", data["description"])

    def test_no_chapters_warning(self):
        self.mocks()
        did = recap.run(self.cfg)  # no spans: only proportional guess
        data = json.loads(db.get_draft(self.conn, did)["data_json"])
        self.assertEqual(data["chapters"], [])
        self.assertIn("chapters_warning", data)
        self.assertNotIn("Chapters\n", data["description"])

    def test_week_dedup(self):
        m = self.mocks()
        first = recap.run(self.cfg)
        # the render is mocked: give the row the video file a real render would have left
        Path(db.get_draft(self.conn, first)["video_path"]).write_bytes(b"v")
        self.assertIsNone(recap.run(self.cfg))
        self.assertEqual(len(self.rows()), 1)
        self.notes.clear()
        self.assertIsNone(recap.run(self.cfg, on_demand=True))
        self.assertTrue(any(f"#{first}" in n and "force" in n for n in self.notes))
        second = recap.run(self.cfg, force=True)
        self.assertNotEqual(first, second)
        self.assertEqual(m["send"].call_count, 2)

    def test_failed_allows_retry(self):
        self.mocks()
        first = recap.run(self.cfg)
        db.update_draft(self.conn, first, status="failed")
        second = recap.run(self.cfg)
        self.assertIsNotNone(second)
        self.assertNotEqual(first, second)

    def test_skip_below_min(self):
        m = self.mocks()
        self.n_stories = 2
        self.assertIsNone(recap.run(self.cfg))
        self.assertEqual(self.rows(), [])
        self.assertTrue(any("skipped" in n and "2" in n and "3" in n for n in self.notes))
        m["chat"].assert_not_called()

    def test_disabled(self):
        m = self.mocks()
        self.cfg.raw["recap"]["enabled"] = False
        self.assertIsNone(recap.run(self.cfg))
        m["chat"].assert_not_called()

    def test_failure_after_row_marks_failed(self):
        m = self.mocks()
        m["render"].render_recap.side_effect = RuntimeError("boom sk-secret")
        self.cfg.openrouter_api_key = "sk-secret"
        with self.assertRaises(RuntimeError):
            recap.run(self.cfg)
        row = self.rows()[0]
        self.assertEqual(row["status"], "failed")
        self.assertIn("boom", row["error"])
        self.assertNotIn("sk-secret", row["error"])
        self.assertTrue(any("failed" in n for n in self.notes))
        self.assertFalse(any("sk-secret" in n for n in self.notes))
        m["send"].assert_not_called()

    def test_render_retries_without_broll(self):
        m = self.mocks()
        clip = self.cfg.path("work") / "draft_1_broll_0.mp4"
        m["broll"].return_value = [clip]
        calls = []

        def rr(cfg, wav, ass, part_clips, durs, out, *, prefix):
            calls.append(part_clips)
            if len(calls) == 1:
                raise RuntimeError("ffmpeg died")
            return out
        m["render"].render_recap.side_effect = rr
        recap.run(self.cfg)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(c for c in calls[0]))
        self.assertTrue(all(c == [] for c in calls[1]))

    def test_script_failure_before_row_raises_without_row(self):
        self.mocks()
        mock.patch("cybershorts.writer.chat", return_value="not json").start()
        with self.assertRaises(RuntimeError):
            recap.run(self.cfg)
        self.assertEqual(self.rows(), [])


if __name__ == "__main__":
    unittest.main()
