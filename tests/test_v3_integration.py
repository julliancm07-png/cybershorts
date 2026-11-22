"""v3 integration: real tags/writer validation/cvedata.confirm_card/db/captions/render/pipeline.
Only the network edges are mocked: writer.chat, NVD/KEV requests, article fetch, TTS engine,
whisper, Telegram and the YouTube API."""
import datetime as dt
import json
import os
import shutil
import tempfile
import unittest
from array import array
from unittest import mock

CVE = "CVE-2025-1111"
SIGN_OFF = "Follow for daily cyber news."
HOOK_A = "Attackers are already using a new Fortinet flaw."
HOOK_B = "Your firewall may be wide open right now."
HOOK_C = "A Fortinet bug is being used against real networks."
SCRIPT = (f"[curious] {HOOK_A}\n\nThe bug sits in FortiOS and lets outsiders run code. "
          "[serious] Fortinet released a fix.\n\nUpdate your firewall today.")

STORY = {"url": "https://example.com/fortinet-flaw",
         "title": f"Fortinet patches {CVE} exploited in the wild",
         "summary": f"Fortinet released a fix for {CVE} in FortiOS. Attackers are actively exploiting it. "
                    "Update now.",
         "source": "Example", "published": None}

SCRIPT_JSON = {
    "on_screen_title": "Fortinet flaw exploited",
    "script": SCRIPT,
    "youtube_title": "Fortinet flaw exploited now #Shorts",
    "description": f"Fortinet fixed {CVE}. Update now.",
    "hashtags": ["fortinet", "patch", "cybersecurity"],
    "pexels_query": "server room",
    "keywords": ["FortiOS", "update"],
    "alt_hooks": [HOOK_B, HOOK_C],
    "series": "Patch This Now",
    "first_comment": "Have you updated your Fortinet gear yet?",
    "data_card": {"cve_id": CVE, "cvss": 9.1, "severity": "critical", "products": ["FortiOS", "Made Up Product"],
                  "exploited": True, "patch_available": True},
}

KEV_ENTRY = {"cveID": CVE, "vendorProject": "Fortinet", "product": "FortiOS",
             "vulnerabilityName": "Fortinet FortiOS Out-of-Bounds Write Vulnerability",
             "shortDescription": "Fortinet FortiOS contains an out-of-bounds write vulnerability.",
             "requiredAction": "Apply updates per vendor instructions.",
             "knownRansomwareCampaignUse": "Known", "dateAdded": "2020-01-01"}

NVD_JSON = {"vulnerabilities": [{"cve": {"metrics": {"cvssMetricV31": [
    {"type": "Primary", "cvssData": {"baseScore": 9.8, "baseSeverity": "CRITICAL"}}]}}}]}


class FakeResp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status
        self.headers = {}
        self.content = b"x"

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


class FakeEngine:
    context = False

    def __init__(self):
        self.chunks = []

    def say(self, text, **kw):
        self.chunks.append(text)
        n = 24000 * max(1, len(text.split())) // 3  # 1/3 s per word of silence
        return array("h", [0]) * n, 24000


def fake_chat_factory(calls):
    def fake_chat(cfg, messages, temperature=None, model=None, **kw):
        text = "\n".join(str(m.get("content", "")) for m in messages)
        calls.append(text[:60])
        if "Pick the opening hook" in text:
            return '{"index": 1}'
        if "strict fact-checker" in text:
            return '{"issues": []}'
        if "rate cybersecurity headlines" in text:
            return "[9]"
        return json.dumps(SCRIPT_JSON)
    return fake_chat


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg not installed")
class V3IntegrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("APP_DIR")
        os.environ["APP_DIR"] = self._tmp.name
        from cybershorts import config, pipeline, tts

        self.pipeline, self.tts = pipeline, tts
        cfg = self.cfg = config.load()
        cfg.telegram_bot_token, cfg.telegram_chat_id = "t", "1"
        cfg.raw["voice"]["engine"] = "elevenlabs"
        cfg.raw["voice"]["elevenlabs"]["model"] = "eleven_v3"
        cfg.raw["broll"]["provider"] = "none"
        cfg.raw["breaking"]["quiet_hours"] = []
        cfg.raw["breaking"]["daily_cap"] = 5
        self.engine = FakeEngine()
        self.chat_calls = []
        self.fetch_calls = []
        self.tg = mock.Mock()
        self.tg.send_video.return_value = {"result": {"message_id": 7}}
        self.kev_vulns = [dict(KEV_ENTRY)]

        def fake_get(url, *a, **kw):
            if "cisa.gov" in url:
                return FakeResp({"vulnerabilities": self.kev_vulns})
            if "nvd.nist.gov" in url:
                return FakeResp(NVD_JSON)
            raise AssertionError("unexpected network call: " + url)

        def fake_fetch(url, *a, **kw):
            self.fetch_calls.append(url)
            return "Fortinet says attackers exploit the flaw. Customers should update."

        patches = [
            mock.patch("cybershorts.writer.chat", side_effect=fake_chat_factory(self.chat_calls)),
            mock.patch("cybershorts.cvedata.requests.get", side_effect=fake_get),
            mock.patch("cybershorts.article.fetch_text", side_effect=fake_fetch),
            mock.patch("cybershorts.tts._make_engine", return_value=self.engine),
            mock.patch("cybershorts.tts.ensure_voice", return_value=None),
            mock.patch("cybershorts.captions.transcribe_words", return_value=[]),  # whisper: even spread
            mock.patch("cybershorts.pipeline._telegram", return_value=self.tg),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("APP_DIR", None)
        else:
            os.environ["APP_DIR"] = self._old
        self._tmp.cleanup()

    def draft(self, i):
        from cybershorts import db
        conn = db.connect(self.cfg)
        try:
            row = db.get_draft(conn, i)
            return row, json.loads(row["data_json"])
        finally:
            conn.close()

    def check_ass(self, draft_id, *must):
        ass = (self.cfg.path("work") / f"draft_{draft_id}.ass").read_text(encoding="utf-8")
        for m in must:
            self.assertIn(m, ass)
        return ass

    # -------------------------------------------------------------------
    def test_make_draft_swap_hook_and_publish(self):
        from cybershorts import db, tags, youtube

        story = dict(STORY)  # no "article" key: the pipeline fetches it exactly once
        did = self.pipeline.make_draft_for_story(self.cfg, story, kind="breaking", kev=dict(KEV_ENTRY))
        self.assertEqual(self.fetch_calls, [STORY["url"]])

        row, data = self.draft(did)
        self.assertEqual((row["status"], row["kind"], row["series"]), ("rendered", "breaking", "Patch This Now"))
        self.assertTrue(row["video_path"] and os.path.exists(row["video_path"]))
        # tags: only in voice_script; script and everything else is tag-free
        self.assertNotIn("[", data["script"])
        self.assertIn("[serious]", data["voice_script"])
        self.assertEqual(tags.strip_tags(data["voice_script"]).split("\n\n")[1:],
                         data["script"].split("\n\n")[1:])
        self.assertTrue(any("[curious]" in c or "[serious]" in c for c in self.engine.chunks))
        # hook pick (index 1) swapped into both scripts; hooks stored
        self.assertEqual(data["hooks"], [HOOK_A, HOOK_B, HOOK_C])
        self.assertEqual(data["hook_index"], 1)
        self.assertTrue(data["script"].startswith(HOOK_B))
        self.assertTrue(data["voice_script"].startswith(HOOK_B))
        # series hashtag first, comment kept, breaking title prefix
        self.assertEqual(data["hashtags"][0], "PatchThisNow")
        self.assertTrue(data["youtube_title"].startswith("🚨 "))
        self.assertEqual(data["first_comment"], SCRIPT_JSON["first_comment"])
        # data card confirmed in code: NVD score wins, unknown product dropped
        card = data["data_card"]
        self.assertEqual((card["cve_id"], card["cvss"], card["severity"]), (CVE, 9.8, "CRITICAL"))
        self.assertEqual(card["products"], ["FortiOS"])
        self.assertTrue(card["exploited"] and card["patch_available"])
        # bookkeeping
        conn = db.connect(self.cfg)
        try:
            self.assertTrue(db.cve_used(conn, CVE))
            self.assertEqual(db.count_drafts_since(conn, "2000-01-01T00:00:00", kind="breaking"), 1)
            self.assertEqual(db.count_drafts_since(conn, "2000-01-01T00:00:00", series="Patch This Now"), 1)
        finally:
            conn.close()
        # overlay reached the ASS file
        self.check_ass(did, "BREAKING", CVE, "CVSS 9.8", "ACTIVELY EXPLOITED", "PATCH AVAILABLE")
        # telegram: caption + keyboard with alt hook buttons for hooks 1 and 3 (1-based)
        args = self.tg.send_video.call_args[0]
        caption, kb = args[1], args[2]
        self.assertLessEqual(len(caption), 1024)
        self.assertIn("BREAKING", caption)
        self.assertIn("Patch This Now", caption)
        self.assertIn("Alt hooks", caption)
        cbs = [b["callback_data"] for row_ in kb["inline_keyboard"] for b in row_]
        self.assertIn(f"hook:1:{did}", cbs)
        self.assertIn(f"hook:3:{did}", cbs)
        self.assertNotIn(f"hook:2:{did}", cbs)

        # bot callback "hook:3:<id>" -> swap_hook(n=3) is 1-based
        self.tg.send_video.reset_mock()
        self.pipeline.swap_hook(self.cfg, did, 3)
        row, data = self.draft(did)
        self.assertEqual(data["hook_index"], 2)
        self.assertTrue(data["script"].startswith(HOOK_C))
        self.assertTrue(data["voice_script"].startswith(HOOK_C))
        self.assertEqual(row["status"], "rendered")
        self.tg.send_video.assert_called_once()
        cbs = [b["callback_data"] for row_ in self.tg.send_video.call_args[0][2]["inline_keyboard"] for b in row_]
        self.assertIn(f"hook:2:{did}", cbs)
        self.assertNotIn(f"hook:3:{did}", cbs)

        # publish: upload mocked at the youtube service edge, comment posted, pin hint shown
        token = youtube.token_path(self.cfg)
        token.write_text(json.dumps({"scopes": [youtube.SCOPE_UPLOAD, youtube.SCOPE_FORCE_SSL]}))
        yt = mock.Mock()
        yt.commentThreads.return_value.insert.return_value.execute.return_value = {"id": "cmt1"}
        with mock.patch("cybershorts.youtube.upload", return_value="vid123"), \
             mock.patch("cybershorts.youtube.get_service", return_value=yt):
            msg = self.pipeline.publish(self.cfg, did, {"yt"})
        self.assertIn("Draft %d: published" % did, msg)
        self.assertIn("First comment posted", msg)
        self.assertIn("Pin it", msg)
        body = yt.commentThreads.return_value.insert.call_args.kwargs["body"]
        self.assertEqual(body["snippet"]["videoId"], "vid123")
        self.assertEqual(body["snippet"]["topLevelComment"]["snippet"]["textOriginal"], data["first_comment"])
        row, _ = self.draft(did)
        self.assertEqual((row["status"], row["youtube_id"], row["yt_comment_id"]), ("published", "vid123", "cmt1"))

    def test_publish_comment_failure_keeps_published(self):
        did = self.pipeline.make_draft_for_story(self.cfg, dict(STORY, article="a"))
        # no token file: post_comment must skip (never raise) and the publish stays successful
        with mock.patch("cybershorts.youtube.upload", return_value="vid9"):
            msg = self.pipeline.publish(self.cfg, did, {"yt"})
        self.assertIn(": published", msg)
        self.assertIn("Comment skipped", msg)
        row, _ = self.draft(did)
        self.assertEqual((row["status"], row["yt_comment_id"]), ("published", None))

    def test_analytics_sees_published_row(self):
        from cybershorts import analytics, db, youtube

        did = self.pipeline.make_draft_for_story(self.cfg, dict(STORY, article="a"))
        youtube.token_path(self.cfg).write_text(json.dumps({"scopes": [youtube.SCOPE_UPLOAD, youtube.SCOPE_FORCE_SSL]}))
        with mock.patch("cybershorts.youtube.upload", return_value="vidA"):
            self.pipeline.publish(self.cfg, did, {"yt"})
        conn = db.connect(self.cfg)
        conn.execute("UPDATE drafts SET updated_at='2000-01-02T00:00:00' WHERE id=?", (did,))
        conn.commit()
        conn.close()
        conn = db.connect(self.cfg)
        try:
            self.assertEqual(len(db.published_videos(conn, "1999-01-01T00:00:00")), 1)
        finally:
            conn.close()
        self.cfg.raw["analytics"]["lookback_days"] = 36500
        stats = {"vidA": {"views": 10, "likes": 2, "comments": 1}}
        with mock.patch("cybershorts.youtube.video_stats", return_value=stats), \
             mock.patch("cybershorts.analytics._send") as send:
            res = analytics.run(self.cfg, telegram=True)
        self.assertEqual(res["videos"], 1)
        conn = db.connect(self.cfg)
        try:
            rows = db.all_video_stats(conn)
            self.assertEqual((rows[0]["youtube_id"], rows[0]["draft_id"], rows[0]["views"]), ("vidA", did, 10))
        finally:
            conn.close()
        send.assert_called_once()

    def test_breaking_run_seeds_then_drafts_from_kev(self):
        from cybershorts import breaking, db

        today = breaking._local_now(self.cfg).date().isoformat()
        old = dict(KEV_ENTRY, cveID="CVE-2019-0001")
        self.kev_vulns = [old]
        with mock.patch("cybershorts.news.fetch_candidates", return_value=[]):
            self.assertIsNone(breaking.run(self.cfg))  # first run seeds only
            conn = db.connect(self.cfg)
            try:
                self.assertEqual(db.kv_get(conn, "kev_seeded"), "1")
            finally:
                conn.close()
            self.kev_vulns = [old, dict(KEV_ENTRY, dateAdded=today)]
            self.cfg.path("kev.json").unlink(missing_ok=True)
            did = breaking.run(self.cfg)
            self.assertIsInstance(did, int)
            self.assertEqual(self.fetch_calls, [], "KEV-built story must not fetch the NVD page")
            row, data = self.draft(did)
            self.assertEqual((row["kind"], row["status"]), ("breaking", "rendered"))
            self.assertTrue(data["breaking"])
            self.assertEqual(data["data_card"]["cve_id"], CVE)
            self.check_ass(did, "BREAKING", CVE)
            conn = db.connect(self.cfg)
            try:
                self.assertIn(CVE, db.kev_seen_ids(conn))
                self.assertTrue(db.cve_used(conn, CVE))
            finally:
                conn.close()
            # a held draft lock blocks the next run; nothing new to draft otherwise
            with self.pipeline.draft_lock(self.cfg, blocking=False):
                self.kev_vulns.append(dict(KEV_ENTRY, cveID="CVE-2025-2222", dateAdded=today))
                self.cfg.path("kev.json").unlink(missing_ok=True)
                self.assertIsNone(breaking.run(self.cfg))

    def test_breaking_rss_fetches_article_once(self):
        from cybershorts import breaking, db

        self.cfg.raw["breaking"]["kev"] = False
        story = dict(STORY, published=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     title="Big outage hits everyone today")
        with mock.patch("cybershorts.news.fetch_candidates", return_value=[dict(story)]):
            did = breaking.run(self.cfg)
        self.assertIsInstance(did, int)
        self.assertEqual(self.fetch_calls, [STORY["url"]])
        row, _ = self.draft(did)
        self.assertEqual(row["kind"], "breaking")
        conn = db.connect(self.cfg)
        try:
            self.assertEqual(db.get_urgency(conn, STORY["url"]), 9)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
