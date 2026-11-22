import unittest

from cybershorts import tags


class Cfg:
    def __init__(self, raw=None):
        self.raw = raw or {}

    def get(self, dotted, default=None):
        cur = self.raw
        for p in dotted.split("."):
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                return default
        return cur


def paras(t):
    return [p for p in t.split("\n") if p.strip()]


class StripTests(unittest.TestCase):
    def test_strip_basic(self):
        self.assertEqual(tags.strip_tags("[sighs] Hello [whispers] world."), "Hello world.")

    def test_space_before_punctuation(self):
        self.assertEqual(tags.strip_tags("Wow [laughs]. Ok"), "Wow. Ok")

    def test_keeps_newlines_and_drops_empty_paragraphs(self):
        self.assertEqual(tags.strip_tags("A b.\n\n[sighs]\n\nC d."), "A b.\n\nC d.")
        self.assertEqual(tags.strip_tags("A\n[sighs]\nB"), "A\nB")

    def test_empty(self):
        self.assertEqual(tags.strip_tags(""), "")
        self.assertEqual(tags.strip_tags(None), "")

    def test_non_tags_untouched(self):
        self.assertEqual(tags.strip_tags("Array[0] is 5."), "Array[0] is 5.")


class SanitizeTests(unittest.TestCase):
    ALLOWED = ["sighs", "whispers", "serious", "laughs"]

    def test_lowercase_and_unknown_dropped(self):
        out = tags.sanitize_tags("[Sighs] Hi [bogus] there.", self.ALLOWED, 3)
        self.assertEqual(out, "[sighs] Hi there.")

    def test_max_tags_keeps_first(self):
        out = tags.sanitize_tags("[sighs] a [whispers] b [serious] c [laughs] d", self.ALLOWED, 2)
        self.assertEqual(out.count("["), 2)
        self.assertIn("[sighs]", out)
        self.assertIn("[whispers]", out)

    def test_tag_only_paragraph_merges_into_next(self):
        raw = "One two.\n\n[serious]\n\nThree four."
        out = tags.sanitize_tags(raw, self.ALLOWED, 3)
        self.assertEqual(out, "One two.\n\n[serious] Three four.")
        self.assertEqual(len(paras(tags.strip_tags(out))), len(paras(tags.strip_tags(raw))))

    def test_trailing_tag_only_paragraph_dropped(self):
        out = tags.sanitize_tags("One.\n\n[sighs]", self.ALLOWED, 3)
        self.assertEqual(out, "One.")

    def test_paragraph_count_matches_clean(self):
        raw = "[sighs] A.\n\n[bogus]\n\n[laughs] B.\n\nC [whispers] d."
        out = tags.sanitize_tags(raw, self.ALLOWED, 3)
        self.assertEqual(len(paras(tags.strip_tags(out))), 3)

    def test_defaults(self):
        self.assertIn("[sighs]", tags.sanitize_tags("[sighs] ok"))


class SupportedTests(unittest.TestCase):
    def cfg(self, **el):
        return Cfg({"voice": {"engine": "elevenlabs", "elevenlabs": {"model": "eleven_v3", **el}}})

    def test_v3(self):
        self.assertTrue(tags.tags_supported(self.cfg()))

    def test_default_model_not_supported(self):
        c = Cfg({"voice": {"engine": "elevenlabs"}})
        self.assertFalse(tags.tags_supported(c))

    def test_case_insensitive_model_and_explicit_args(self):
        c = Cfg({"voice": {"engine": "kokoro", "elevenlabs": {"tag_models": ["Eleven_V3"]}}})
        self.assertFalse(tags.tags_supported(c))
        self.assertTrue(tags.tags_supported(c, "ElevenLabs", "eleven_v3"))
        self.assertFalse(tags.tags_supported(c, "kokoro", "eleven_v3"))
        self.assertFalse(tags.tags_supported(c, "piper"))

    def test_audio_tags_off(self):
        self.assertFalse(tags.tags_supported(self.cfg(audio_tags=False)))


if __name__ == "__main__":
    unittest.main()
