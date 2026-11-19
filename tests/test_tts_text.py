import unittest

from cybershorts.captions import align_to_script
from cybershorts.tts import apply_pronunciations, clean_for_speech, split_beats


class SpeechTextTests(unittest.TestCase):
    def test_split_beats(self):
        text = "Big bug. Patch now!\n\nHere's why. It's bad.\nFollow for more."
        self.assertEqual(split_beats(text), [["Big bug.", "Patch now!"], ["Here's why.", "It's bad."], ["Follow for more."]])

    def test_split_keeps_version_numbers(self):
        self.assertEqual(split_beats("Version 2.4 is fixed. Update."), [["Version 2.4 is fixed.", "Update."]])

    def test_pronunciations_whole_word(self):
        t = apply_pronunciations("CISA warns about SQL and NoSQL; CISA's list", {"CISA": "Sisa", "SQL": "sequel"})
        self.assertEqual(t, "Sisa warns about sequel and NoSQL; Sisa's list")

    def test_clean_for_speech(self):
        self.assertEqual(clean_for_speech("Read (more) at https://x.y/z & 50% off"), "Read more at and 50 percent off")


class AlignTests(unittest.TestCase):
    def test_script_spelling_wins(self):
        words = [(0.0, 0.4, "Sisa"), (0.4, 0.8, "warns"), (0.8, 1.2, "admins.")]
        out = align_to_script(words, "CISA warns admins.")
        self.assertEqual([w[2] for w in out], ["CISA", "warns", "admins."])
        self.assertEqual(out[0][:2], (0.0, 0.4))

    def test_split_span(self):
        words = [(0.0, 1.0, "mac-os"), (1.0, 1.5, "update")]
        out = align_to_script(words, "macOS 15 update", min_ratio=0.0)
        self.assertEqual([w[2] for w in out], ["macOS", "15", "update"])
        starts = [w[0] for w in out]
        self.assertEqual(starts, sorted(starts))

    def test_low_match_keeps_whisper(self):
        words = [(0.0, 0.5, "totally"), (0.5, 1.0, "different")]
        self.assertEqual(align_to_script(words, "nothing alike here at all"), words)
