import os
import tempfile
import unittest
from unittest import mock


class FactCheckTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("APP_DIR")
        os.environ["APP_DIR"] = self._tmp.name
        from cybershorts import config, writer

        self.w = writer
        self.cfg = config.load()
        self.story = {"title": "T", "summary": "S", "url": "http://x", "article": "Full article text"}
        self.data = {"on_screen_title": "t", "script": "s", "description": "d"}

    def tearDown(self):
        if self._old is None:
            os.environ.pop("APP_DIR", None)
        else:
            os.environ["APP_DIR"] = self._old
        self._tmp.cleanup()

    def test_ok(self):
        with mock.patch.object(self.w, "chat", return_value='```json\n{"issues": []}\n```') as c:
            r = self.w.fact_check(self.cfg, self.story, self.data)
        self.assertEqual(r, {"ok": True, "issues": []})
        self.assertEqual(c.call_args.kwargs["temperature"], 0)
        self.assertIn("Full article text", c.call_args.args[1][0]["content"])

    def test_checks_all_hooks_except_active(self):
        data = dict(self.data, hooks=["Zero hook.", "One hook.", "Two hook."], hook_index=1)
        with mock.patch.object(self.w, "chat", return_value='{"issues": []}') as c:
            self.w.fact_check(self.cfg, self.story, data)
        prompt = c.call_args.args[1][0]["content"]
        self.assertIn("Zero hook.", prompt)
        self.assertIn("Two hook.", prompt)
        self.assertNotIn("One hook.", prompt)

    def test_issues_capped_and_truncated(self):
        raw = '{"issues": ["' + '", "'.join(["x" * 300] + [f"i{n}" for n in range(8)]) + '"]}'
        with mock.patch.object(self.w, "chat", return_value="Here: " + raw):
            r = self.w.fact_check(self.cfg, self.story, self.data)
        self.assertFalse(r["ok"])
        self.assertEqual(len(r["issues"]), 5)
        self.assertLessEqual(len(r["issues"][0]), 140)

    def test_summary_fallback_and_model(self):
        self.cfg.raw.setdefault("llm", {})["fact_check_model"] = "other/model"
        story = {"title": "TTT", "summary": "SSS", "url": "u"}
        with mock.patch.object(self.w, "chat", return_value='{"issues": []}') as c:
            self.w.fact_check(self.cfg, story, self.data)
        self.assertEqual(c.call_args.kwargs["model"], "other/model")
        prompt = c.call_args.args[1][0]["content"]
        self.assertIn("TTT", prompt)
        self.assertIn("SSS", prompt)

    def test_prompt_wording(self):
        with mock.patch.object(self.w, "chat", return_value='{"issues": []}') as c:
            self.w.fact_check(self.cfg, self.story, self.data)
        prompt = c.call_args.args[1][0]["content"]
        self.assertIn("Rephrasing, simplification and summary are supported", prompt)
        self.assertIn("exploitation status", prompt)
        self.assertIn("Ignore tone, the hook's framing and the sign-off", prompt)

    def test_errors_skip(self):
        for bad in (RuntimeError("boom"), ValueError("x")):
            with mock.patch.object(self.w, "chat", side_effect=bad):
                r = self.w.fact_check(self.cfg, self.story, self.data)
            self.assertTrue(r["ok"])
            self.assertEqual(r["issues"], [])
            self.assertIn("skipped", r)
        with mock.patch.object(self.w, "chat", return_value="not json at all"):
            self.assertIn("skipped", self.w.fact_check(self.cfg, self.story, self.data))


class KeywordsTests(FactCheckTests):
    def _v(self, **kw):
        d = {"on_screen_title": "T", "script": "Hello there.", **kw}
        return self.w.validate_script(d, {"url": "http://x"}, self.cfg)

    def test_coercion(self):
        self.assertEqual(self._v()["keywords"], [])
        self.assertEqual(self._v(keywords="a, b ,a")["keywords"], ["a", "b"])
        long = self._v(keywords=["ok", "x" * 25, 5, "", "c", "d", "e", "f", "g"])["keywords"]
        self.assertEqual(long, ["ok", "5", "c", "d", "e", "f"])
        self.assertEqual(self._v(keywords={"a": 1})["keywords"], [])


if __name__ == "__main__":
    unittest.main()
