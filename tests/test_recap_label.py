"""Recap label uses the channel's own count, never the ISO week."""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from cybershorts import config, db, recap


def _cfg(tmp):
    return config.Config(tmp, copy.deepcopy(config.DEFAULTS), {})


class LabelTests(unittest.TestCase):
    def test_label_template(self):
        cfg = _cfg(tempfile.mkdtemp())
        self.assertEqual(recap.episode_label(cfg, 1), "Week 1")
        cfg.raw["recap"]["label"] = "Recap #{n}"
        self.assertEqual(recap.episode_label(cfg, 3), "Recap #3")
        cfg.raw["recap"]["label"] = "Bad {x}"
        self.assertEqual(recap.episode_label(cfg, 2), "Week 2")

    def test_date_range(self):
        cfg = _cfg(tempfile.mkdtemp())
        now = datetime(2026, 11, 22, 15, 0, tzinfo=timezone.utc)
        self.assertEqual(recap.date_range(cfg, now), "Nov 16 to Nov 22, 2026")

    def test_count_published_recaps(self):
        cfg = _cfg(tempfile.mkdtemp())
        conn = db.connect(cfg)
        db.init(conn)
        self.assertEqual(db.count_published_recaps(conn), 0)
        a = db.create_draft(conn, "recap:2026-W47", "rendered", "{}", kind="recap")
        db.create_draft(conn, "recap:2026-W48", "rendered", "{}", kind="recap")
        db.update_draft(conn, a, status="published")
        self.assertEqual(db.count_published_recaps(conn), 1)
        conn.close()

    def test_prompt_has_label_not_iso_week(self):
        cfg = _cfg(tempfile.mkdtemp())
        stories = [{"title": f"T{i}", "summary": "s", "script": "x", "description": "d", "url": f"u{i}",
                    "data": {}} for i in range(3)]
        seen = {}

        def fake_chat(cfg_, messages, **kw):
            seen["text"] = "\n".join(m["content"] for m in messages)
            raise ValueError("stop")

        with mock.patch.object(recap.writer, "chat", side_effect=fake_chat), \
                mock.patch.object(recap, "source_text", return_value="material"):
            with self.assertRaises(Exception):
                recap.write_recap_script(cfg, stories, "2026-W47", label="Week 1", dates="Nov 16 to Nov 22, 2026")
        self.assertIn("Week 1", seen["text"])
        self.assertIn("Nov 16 to Nov 22, 2026", seen["text"])
        self.assertNotIn("2026-W47", seen["text"])


if __name__ == "__main__":
    unittest.main()
