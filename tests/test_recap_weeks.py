"""Recap week number, no repeated Shorts, and the since-last-recap window."""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone

from cybershorts import config, db, recap


def _setup():
    cfg = config.Config(tempfile.mkdtemp(), copy.deepcopy(config.DEFAULTS), {})
    conn = db.connect(cfg)
    db.init(conn)
    return cfg, conn


def _short(conn, url, created, yt=None, status="published"):
    did = db.create_draft(conn, url, status, json.dumps({"script": "s", "description": "d"}))
    conn.execute("UPDATE drafts SET created_at=?, updated_at=?, youtube_id=? WHERE id=?", (created, created, yt, did))
    conn.commit()
    return did


def _recap(conn, week, created, draft_ids, status="published"):
    rid = db.create_draft(conn, f"recap:{week}", status,
                          json.dumps({"stories": [{"draft_id": d} for d in draft_ids]}), kind="recap")
    conn.execute("UPDATE drafts SET created_at=? WHERE id=?", (created, rid))
    conn.commit()
    return rid


NOW = datetime(2026, 11, 22, 18, 0, tzinfo=timezone.utc)  # Sunday, ISO week 47


class AccountWeekTests(unittest.TestCase):
    def test_counts_from_first_posted_short_only(self):
        cfg, conn = _setup()
        self.assertEqual(recap.account_week(cfg, conn, NOW), 1)
        _short(conn, "u0", "2026-11-03T12:00:00", yt=None, status="rejected")  # never posted: ignored
        _short(conn, "u1", "2026-11-12T15:00:00", yt="y1")  # week 46 (Mon Nov 9)
        self.assertEqual(recap.account_week(cfg, conn, NOW), 2)
        # recaps and unposted drafts never change it
        _recap(conn, "2026-W47", "2026-11-22T10:00:00", [])
        _recap(conn, "2026-W47", "2026-11-22T12:00:00", [])
        self.assertEqual(recap.account_week(cfg, conn, NOW), 2)


class SelectionTests(unittest.TestCase):
    def test_never_reuses_shorts_from_kept_recaps_and_window(self):
        cfg, conn = _setup()
        a = _short(conn, "a", "2026-11-17T12:00:00", yt="ya")
        b = _short(conn, "b", "2026-11-18T12:00:00", yt="yb")
        c = _short(conn, "c", "2026-11-21T12:00:00", yt="yc")
        _recap(conn, "2026-W46", "2026-11-19T00:00:00", [a])          # older recap covered a
        got = {s["draft_id"] for s in recap.select_stories(cfg, conn, NOW, week="2026-W47")}
        self.assertEqual(got, {c})  # a covered; b is before the last recap -> outside the window
        self.assertNotIn(b, got)

    def test_rejected_recap_frees_its_shorts(self):
        cfg, conn = _setup()
        a = _short(conn, "a", "2026-11-20T12:00:00", yt="ya")
        _recap(conn, "2026-W46", "2026-11-19T00:00:00", [a], status="rejected")
        got = {s["draft_id"] for s in recap.select_stories(cfg, conn, NOW, week="2026-W47")}
        self.assertEqual(got, {a})

    def test_remake_reuses_unposted_same_week_but_not_posted(self):
        cfg, conn = _setup()
        a = _short(conn, "a", "2026-11-20T12:00:00", yt="ya")
        b = _short(conn, "b", "2026-11-21T12:00:00", yt="yb")
        _recap(conn, "2026-W47", "2026-11-22T09:00:00", [a, b], status="rendered")
        got = {s["draft_id"] for s in recap.select_stories(cfg, conn, NOW, week="2026-W47")}
        self.assertEqual(got, {a, b})
        conn.execute("UPDATE drafts SET status='published' WHERE kind='recap'")
        conn.commit()
        self.assertEqual(recap.select_stories(cfg, conn, NOW, week="2026-W47"), [])


if __name__ == "__main__":
    unittest.main()
