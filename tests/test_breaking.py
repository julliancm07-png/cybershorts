import datetime as dt
import os
import tempfile
import unittest
from contextlib import contextmanager
from unittest import mock

from cybershorts import breaking, pipeline


class Cfg:
    def __init__(self, d, **vals):
        self.d = d
        self.vals = {"breaking.timezone": "UTC", "breaking.quiet_hours": ["22:00", "07:00"],
                     "news.fetch_full_text": False}
        self.vals.update(vals)

    def path(self, *parts):
        return os.path.join(self.d, *parts)

    def get(self, k, default=None):
        return self.vals.get(k, default)


def today(delta=0):
    return (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=delta)).isoformat()


def kev(cve, added=None, ransom="Unknown"):
    return {"cveID": cve, "vendorProject": "Acme", "product": "Gateway", "vulnerabilityName": "RCE",
            "shortDescription": "bad", "requiredAction": "patch", "dateAdded": added or today(),
            "knownRansomwareCampaignUse": ransom}


NOON = dt.datetime(2026, 11, 21, 12, 0, tzinfo=dt.timezone.utc)
NIGHT = dt.datetime(2026, 11, 21, 23, 30, tzinfo=dt.timezone.utc)


class QuietHourTests(unittest.TestCase):
    def test_window_crossing_midnight(self):
        cfg = Cfg("/tmp")
        self.assertTrue(breaking.in_quiet_hours(cfg, NIGHT))
        self.assertTrue(breaking.in_quiet_hours(cfg, NOON.replace(hour=3)))
        self.assertFalse(breaking.in_quiet_hours(cfg, NOON))
        self.assertFalse(breaking.in_quiet_hours(cfg, NOON.replace(hour=7)))

    def test_timezone_applied_and_fallback(self):
        cfg = Cfg("/tmp", **{"breaking.timezone": "America/New_York"})
        # 03:00 UTC is 22:00 in New York in November -> quiet
        self.assertTrue(breaking.in_quiet_hours(cfg, NOON.replace(hour=3)))
        bad = Cfg("/tmp", **{"breaking.timezone": "Nowhere/Land"})
        with self.assertLogs("cybershorts", "WARNING"):
            self.assertFalse(breaking.in_quiet_hours(bad, NOON))

    def test_local_midnight(self):
        self.assertEqual(breaking.local_midnight_utc_iso(Cfg("/tmp"), NOON), "2026-11-21T00:00:00")


class RunBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = Cfg(self._tmp.name, **{"breaking.quiet_hours": []})
        self.kv = {"kev_seeded": "1"}
        self.seen = set()
        self.used_cves = set()
        self.used_urls = set()
        self.titles = []
        self.cap_count = 0
        self.kev_list = [kev("CVE-2026-1111")]
        self.stories = []
        self.lock_busy = False
        self.entered = 0
        outer = self

        @contextmanager
        def lock(cfg, blocking=True, timeout=None):
            if outer.lock_busy:
                raise pipeline.Busy("held")
            outer.entered += 1
            yield

        P = mock.patch.object
        self.db = breaking.db
        self.mark = mock.Mock(side_effect=lambda conn, entries: self.seen.update(c for c, _ in entries))
        self.set_urg = mock.Mock()
        self.urg = {}
        self.chat = mock.Mock(return_value="[9]")
        patches = [
            P(breaking.db, "connect", return_value=mock.Mock()),
            P(breaking.db, "init"),
            P(breaking.db, "kv_get", create=True, side_effect=lambda c, k, d=None: self.kv.get(k, d)),
            P(breaking.db, "kv_set", create=True, side_effect=lambda c, k, v: self.kv.__setitem__(k, v)),
            P(breaking.db, "kev_seen_ids", create=True, side_effect=lambda c: set(self.seen)),
            P(breaking.db, "mark_kev_seen", create=True, new=self.mark),
            P(breaking.db, "get_urgency", create=True, side_effect=lambda c, u: self.urg.get(u)),
            P(breaking.db, "set_urgency", create=True, new=self.set_urg),
            P(breaking.db, "cve_used", create=True, side_effect=lambda c, cve, since=None: cve in self.used_cves),
            P(breaking.db, "count_drafts_since", create=True, side_effect=lambda *a, **k: self.cap_count),
            P(breaking.db, "upsert_story", create=True),
            P(breaking.db, "mark_used", create=True),
            P(breaking.db, "story_used", side_effect=lambda c, u: u in self.used_urls),
            P(breaking.db, "recent_used_titles", side_effect=lambda c, n=10: self.titles),
            P(breaking.cvedata, "fetch_kev", side_effect=lambda cfg: self.kev_list),
            P(breaking.news, "fetch_candidates", side_effect=lambda cfg, conn: self.stories),
            P(breaking.pipeline, "draft_lock", new=lock),
            P(breaking.pipeline, "make_draft_for_story", return_value=42),
            P(breaking.writer, "chat", new=self.chat),
        ]
        self.m = {p.attribute: p.start() for p in patches}
        self.make = self.m["make_draft_for_story"]

    def tearDown(self):
        mock.patch.stopall()
        self._tmp.cleanup()



class RunTests(RunBase):
    def test_disabled(self):
        self.cfg.vals["breaking.enabled"] = False
        self.assertIsNone(breaking.run(self.cfg))
        self.make.assert_not_called()

    def test_first_run_only_seeds_even_in_quiet_hours(self):
        self.kv.clear()
        self.cfg.vals["breaking.quiet_hours"] = ["00:00", "23:59"]
        self.assertIsNone(breaking.run(self.cfg))
        self.make.assert_not_called()
        self.assertEqual(self.kv["kev_seeded"], "1")
        self.assertIn("CVE-2026-1111", self.seen)

    def test_first_run_with_kev_unavailable_does_not_set_flag(self):
        self.kv.clear()
        self.kev_list = []
        self.assertIsNone(breaking.run(self.cfg))
        self.assertNotIn("kev_seeded", self.kv)

    def test_new_kev_makes_one_breaking_draft(self):
        self.kev_list = [kev("CVE-2026-1111"), kev("CVE-2026-2222")]
        self.assertEqual(breaking.run(self.cfg), 42)
        self.assertEqual(self.make.call_count, 1)
        story = self.make.call_args.args[1]
        self.assertEqual(self.make.call_args.kwargs["kind"], "breaking")
        self.assertEqual(self.make.call_args.kwargs["kev"]["cveID"], story["url"].rsplit("/", 1)[1])
        self.assertTrue(story["url"].startswith("https://nvd.nist.gov/vuln/detail/CVE-2026-"))
        self.assertEqual(len(self.seen), 1)  # the other stays pending

    def test_ransomware_known_first(self):
        self.kev_list = [kev("CVE-2026-1111"), kev("CVE-2026-2222", ransom="Known")]
        breaking.run(self.cfg)
        self.assertEqual(self.make.call_args.kwargs["kev"]["cveID"], "CVE-2026-2222")

    def test_old_and_seen_kev_ignored(self):
        self.kev_list = [kev("CVE-2026-1111", today(10)), kev("CVE-2026-2222")]
        self.seen.add("CVE-2026-2222")
        self.assertIsNone(breaking.run(self.cfg))
        self.make.assert_not_called()

    def test_rss_story_mentioning_cve_is_used(self):
        rss = {"url": "https://x.test/a", "title": "Acme bug CVE-2026-1111 hits", "summary": "s",
               "source": "X", "published": "2026-11-20T00:00:00Z"}
        self.stories = [rss]
        self.cfg.vals["breaking.rss_urgency"] = False
        breaking.run(self.cfg)
        self.assertEqual(self.make.call_args.args[1]["url"], "https://x.test/a")

    def test_quiet_hours_block(self):
        self.cfg.vals["breaking.quiet_hours"] = ["00:00", "23:59"]
        self.assertIsNone(breaking.run(self.cfg))
        self.make.assert_not_called()

    def test_daily_cap_blocks(self):
        self.cap_count = 2
        self.cfg.vals["breaking.daily_cap"] = 2
        self.assertIsNone(breaking.run(self.cfg))
        self.make.assert_not_called()

    def test_held_lock_blocks(self):
        self.lock_busy = True
        self.assertIsNone(breaking.run(self.cfg))
        self.make.assert_not_called()

    def test_cve_dedup_blocks_and_marks_seen(self):
        self.used_cves.add("CVE-2026-1111")
        self.assertIsNone(breaking.run(self.cfg))
        self.make.assert_not_called()
        self.assertIn("CVE-2026-1111", self.seen)

    def test_title_similarity_dedup(self):
        self.kev_list = []
        pub = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.stories = [{"url": "https://x.test/9", "title": "Acme Gateway breach hits millions", "summary": "",
                         "source": "X", "published": pub}]
        self.titles = ["Acme Gateway breach hits millions!"]
        self.assertIsNone(breaking.run(self.cfg))
        self.make.assert_not_called()

    def test_story_used_dedup(self):
        self.used_urls.add("https://nvd.nist.gov/vuln/detail/CVE-2026-1111")
        self.assertIsNone(breaking.run(self.cfg))
        self.make.assert_not_called()

    def test_dry_run_no_draft_no_lock_no_writes_no_telegram(self):
        with mock.patch.object(pipeline, "_notify") as notify, \
             mock.patch("cybershorts.telegram.Telegram") as tg:
            self.assertIsNone(breaking.run(self.cfg, dry_run=True))
        self.make.assert_not_called()
        notify.assert_not_called()
        tg.assert_not_called()
        self.assertEqual(self.entered, 0)
        self.mark.assert_not_called()
        self.assertEqual(self.seen, set())

    def test_rss_urgency_scored_once_batched(self):
        self.kev_list = []
        recent = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        old = "2020-01-01T00:00:00Z"
        self.stories = [
            {"url": "https://x.test/1", "title": "Mega breach", "summary": "", "source": "X", "published": recent},
            {"url": "https://x.test/2", "title": "Minor thing", "summary": "", "source": "X", "published": recent},
            {"url": "https://x.test/3", "title": "Ancient", "summary": "", "source": "X", "published": old}]
        self.chat.return_value = "[9, 3]"
        self.assertEqual(breaking.run(self.cfg), 42)
        self.assertEqual(self.chat.call_count, 1)
        self.assertEqual(self.make.call_args.args[1]["url"], "https://x.test/1")
        self.assertIsNone(self.make.call_args.kwargs["kev"])
        self.assertEqual(self.set_urg.call_count, 2)

    def test_rss_scoring_failure_is_harmless(self):
        self.kev_list = []
        recent = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.stories = [{"url": "https://x.test/1", "title": "t", "summary": "", "source": "X", "published": recent}]
        self.chat.side_effect = RuntimeError("down")
        self.assertIsNone(breaking.run(self.cfg))


class RunRegressionTests(RunBase):
    def recent(self):
        return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_rss_story_marked_used_before_drafting(self):
        self.kev_list = []
        self.stories = [{"url": "https://x.test/1", "title": "Mega breach", "summary": "", "source": "X",
                         "published": self.recent()}]
        order = []
        self.m["mark_used"].side_effect = lambda conn, url: order.append(("used", url))
        self.make.side_effect = lambda *a, **k: (order.append("draft"), (_ for _ in ()).throw(RuntimeError("x")))[0]
        with self.assertRaises(RuntimeError):
            breaking.run(self.cfg)
        self.assertEqual(order, [("used", "https://x.test/1"), "draft"])

    def test_daily_cap_counts_failed_and_rejected(self):
        breaking.run(self.cfg)
        kw = self.m["count_drafts_since"].call_args.kwargs
        self.assertEqual(kw["kind"], "breaking")
        self.assertTrue(kw["include_all"])

    def test_record_mode_skips_with_log(self):
        self.cfg.vals["voice.mode"] = "record"
        with self.assertLogs("cybershorts", "INFO") as cm:
            self.assertIsNone(breaking.run(self.cfg))
        self.assertEqual(len(cm.output), 1)
        self.make.assert_not_called()


class DupTests(RunBase):
    def test_kev_boilerplate_titles_not_similar_dups(self):
        a = "CVE-2026-1111: Acme Widget vulnerability added to CISA's exploited list"
        b = "CVE-2026-2222: Acme Gadget vulnerability added to CISA's exploited list"
        story = {"url": "u2", "title": b}
        self.assertIsNone(breaking._is_dup(mock.Mock(), story, ["CVE-2026-2222"], [a], kev={"cveID": "CVE-2026-2222"}))
        # non-KEV: CVE ids and boilerplate are stripped before comparing
        other = {"url": "u4", "title": "CVE-2026-9999: Zeta Router vulnerability added to CISA's exploited list"}
        self.assertIsNone(breaking._is_dup(mock.Mock(), other, [], [a]))
        same = {"url": "u3", "title": "Acme Widget flaw exploited"}
        self.assertIn("similar", breaking._is_dup(mock.Mock(), same, [], ["Acme Widget flaw exploited!"]))

    def test_cve_used_only_counts_last_30_days(self):
        self.used_cves = {"CVE-2026-1111"}
        breaking._is_dup(mock.Mock(), {"url": "u", "title": "t"}, ["CVE-2026-1111"], [])
        self.m["cve_used"].assert_called()
        since = self.m["cve_used"].call_args.kwargs["since"]
        age = dt.datetime.now(dt.timezone.utc) - dt.datetime.strptime(since, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)
        self.assertAlmostEqual(age.days, 30, delta=1)


class ScoreParseTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(breaking._parse_scores("here: [9, 15, \"x\"]", 4), [9, 10, None, None])
        self.assertEqual(breaking._parse_scores("nonsense", 2), [None, None])


if __name__ == "__main__":
    unittest.main()
