import json
import os
import tempfile
import unittest
from unittest import mock


class WriterV3Tests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("APP_DIR")
        os.environ["APP_DIR"] = self._tmp.name
        from cybershorts import config, writer

        self.w = writer
        self.cfg = config.load()
        self.story = {"title": "Flaw CVE-2026-1234 exploited", "summary": "S", "url": "http://x/a",
                      "source": "Src"}
        self.opts = [{"name": "Patch This Now", "hashtag": "PatchThisNow", "badge": "PATCH THIS NOW",
                      "match": "patch", "guidance": "Name the fix."}]

    def tearDown(self):
        if self._old is None:
            os.environ.pop("APP_DIR", None)
        else:
            os.environ["APP_DIR"] = self._old
        self._tmp.cleanup()

    def base(self, **kw):
        d = {"on_screen_title": "T", "script": "Hook one here.\n\nBody text.\n\nFollow for daily cyber news.",
             "hashtags": ["a", "b", "c"]}
        d.update(kw)
        return d

    def validate(self, d, opts=None):
        return self.w.validate_script(d, self.story, self.cfg, series_options=opts)

    # ---- tags
    def test_tags_only_in_voice_script(self):
        raw = "[curious] Hook one here.\n\nBody [sighs] text.\n\nFollow for daily cyber news."
        with mock.patch("cybershorts.tags.tags_supported", return_value=True):
            out = self.validate(self.base(script=raw))
        self.assertNotIn("[", out["script"])
        self.assertIn("[curious]", out["voice_script"])
        self.assertEqual(out["script"].count("\n\n"), 2)

    def test_tags_unsupported_strips_everything(self):
        raw = "[curious] Hook one here.\n\nBody text.\n\nFollow for daily cyber news."
        with mock.patch("cybershorts.tags.tags_supported", return_value=False):
            out = self.validate(self.base(script=raw, alt_hooks=["[sighs] Alt hook."]))
        self.assertNotIn("voice_script", out)
        self.assertNotIn("[", out["script"])
        self.assertTrue(all("[" not in h for h in out.get("hooks", [])))

    def test_tag_prompt_only_when_supported(self):
        prompts = []

        def fake_chat(cfg, messages, **kw):
            prompts.append(messages[-1]["content"])
            return json.dumps(self.base())

        with mock.patch.object(self.w, "chat", fake_chat):
            with mock.patch("cybershorts.tags.tags_supported", return_value=False):
                self.w.write_script(self.cfg, self.story)
            with mock.patch("cybershorts.tags.tags_supported", return_value=True):
                self.w.write_script(self.cfg, self.story)
        self.assertNotIn("voice tags", prompts[0])
        self.assertIn("voice tags", prompts[1])

    # ---- hooks
    def test_hooks_dedup_cap_and_validation(self):
        alts = ["Alt one is short.", "alt one is short.", "word " * 30, "Alt two works.\n\nTwo paras."]
        out = self.validate(self.base(alt_hooks=alts))
        self.assertEqual(out["hooks"], ["Hook one here.", "Alt one is short."])
        out = self.validate(self.base(alt_hooks=["A one.", "A two.", "A three."]))
        self.assertEqual(len(out["hooks"]), 3)  # llm.hooks = 3 caps first + 2 alts

    def test_hooks_off(self):
        self.cfg.raw["llm"]["hooks"] = 1
        out = self.validate(self.base(alt_hooks=["Alt one."]))
        self.assertNotIn("hooks", out)

    def test_no_alt_hooks_no_key(self):
        self.assertNotIn("hooks", self.validate(self.base()))

    def test_pick_hook(self):
        hooks = ["a", "b", "c"]
        calls = []

        def fake(cfg, messages, temperature=None, model=None):
            calls.append((temperature, model))
            return '{"index": 2}'

        self.cfg.raw["llm"]["fact_check_model"] = "fc/model"
        with mock.patch.object(self.w, "chat", fake):
            self.assertEqual(self.w.pick_hook(self.cfg, self.story, hooks), 2)
            self.cfg.raw["llm"]["hook_model"] = "hook/model"
            self.w.pick_hook(self.cfg, self.story, hooks)
        self.assertEqual(calls, [(0, "fc/model"), (0, "hook/model")])

    def test_pick_hook_errors_give_zero(self):
        with mock.patch.object(self.w, "chat", side_effect=RuntimeError("x")):
            self.assertEqual(self.w.pick_hook(self.cfg, self.story, ["a", "b"]), 0)
        with mock.patch.object(self.w, "chat", return_value='{"index": 9}'):
            self.assertEqual(self.w.pick_hook(self.cfg, self.story, ["a", "b"]), 0)
        with mock.patch.object(self.w, "chat") as c:
            self.assertEqual(self.w.pick_hook(self.cfg, self.story, ["a"]), 0)
            c.assert_not_called()

    # ---- series
    def test_series_hashtag_first_and_in_description(self):
        out = self.validate(self.base(series="patch this now", hashtags=["a", "b", "c", "d", "e", "f"]),
                            self.opts)
        self.assertEqual(out["series"], "Patch This Now")
        self.assertEqual(out["hashtags"][0], "PatchThisNow")
        self.assertEqual(len(out["hashtags"]), 6)
        self.assertIn("#PatchThisNow", out["description"])

    def test_series_must_be_offered(self):
        out = self.validate(self.base(series="Scam of the Week"), self.opts)
        self.assertNotIn("series", out)
        out = self.validate(self.base(series="Patch This Now"), None)
        self.assertNotIn("series", out)

    def test_series_prompt_lists_options_only(self):
        prompts = []

        def fake_chat(cfg, messages, **kw):
            prompts.append(messages[-1]["content"])
            return json.dumps(self.base())

        with mock.patch.object(self.w, "chat", fake_chat):
            self.w.write_script(self.cfg, self.story, series_options=self.opts)
            self.w.write_script(self.cfg, self.story)
        self.assertIn("Patch This Now", prompts[0])
        self.assertIn('"series"', prompts[0])
        self.assertNotIn('"series"', prompts[1])

    # ---- first comment
    def test_first_comment_rules(self):
        self.assertEqual(self.validate(self.base(first_comment="Did you patch yet?"))["first_comment"],
                         "Did you patch yet?")
        for bad in ("No question mark", "See https://x.com now?", "x" * 150 + "?", "Visit example.com?"):
            self.assertNotIn("first_comment", self.validate(self.base(first_comment=bad)))

    def test_first_comment_off(self):
        self.cfg.raw["youtube"]["first_comment"] = False
        self.assertNotIn("first_comment", self.validate(self.base(first_comment="Patched yet?")))

    # ---- data card
    def test_data_card_only_with_cve_and_enabled(self):
        card = {"cve_id": "CVE-2026-1234", "cvss": 9.8}
        self.assertEqual(self.validate(self.base(data_card=card))["data_card"], card)
        self.story = dict(self.story, title="No id here")
        self.assertNotIn("data_card", self.validate(self.base(data_card=card)))
        self.story = dict(self.story, title="CVE-2026-1234")
        self.cfg.raw["data_card"]["enabled"] = False
        self.assertNotIn("data_card", self.validate(self.base(data_card=card)))
        self.cfg.raw["data_card"]["enabled"] = True
        self.assertNotIn("data_card", self.validate(self.base(data_card=None)))

    def test_single_newline_script_normalised_so_hooks_match_beats(self):
        raw = "Hook line.\nSecond line.\nThird line.\nFollow for daily cyber news."
        out = self.validate(self.base(script=raw, alt_hooks=["Alt hook."]))
        self.assertEqual(out["script"], "Hook line.\n\nSecond line.\n\nThird line.\n\nFollow for daily cyber news.")
        self.assertEqual(out["hooks"][0], "Hook line.")

    def test_voice_script_paragraphs_normalised(self):
        raw = "[curious] Hook line.\nBody [sighs] text.\nFollow for daily cyber news."
        with mock.patch("cybershorts.tags.tags_supported", return_value=True):
            out = self.validate(self.base(script=raw))
        self.assertEqual(out["voice_script"].count("\n\n"), 2)
        self.assertNotIn("\n\n\n", out["voice_script"])

    # ---- learnings / breaking
    def test_learnings_appended_and_capped(self):
        self.cfg.path("learnings.md").write_text("- short hooks win\n" + "z" * 2000)
        prompts = []

        def fake_chat(cfg, messages, **kw):
            prompts.append(messages[-1]["content"])
            return json.dumps(self.base())

        with mock.patch.object(self.w, "chat", fake_chat):
            self.w.write_script(self.cfg, self.story, breaking=True)
        self.assertIn("short hooks win", prompts[0])
        self.assertIn("never a source of facts", prompts[0])
        self.assertLess(prompts[0].count("z"), 801)
        self.assertIn("breaking news", prompts[0])

    # ---- fact check
    def test_fact_check_includes_alt_hooks(self):
        data = self.validate(self.base(alt_hooks=["Alt hook claims 99 victims."]))
        with mock.patch.object(self.w, "chat", return_value='{"issues": []}') as c:
            self.w.fact_check(self.cfg, self.story, data)
        self.assertIn("Alt hook claims 99 victims.", c.call_args.args[1][0]["content"])


if __name__ == "__main__":
    unittest.main()
