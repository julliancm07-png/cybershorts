import os
import tempfile
import unittest


class CaptionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_app_dir = os.environ.get("APP_DIR")
        os.environ["APP_DIR"] = self._tmp.name
        from cybershorts.config import load
        from cybershorts import captions

        self.captions = captions
        self.cfg = load()

    def tearDown(self):
        if self._old_app_dir is None:
            os.environ.pop("APP_DIR", None)
        else:
            os.environ["APP_DIR"] = self._old_app_dir
        self._tmp.cleanup()

    def test_chunk_respects_max_words(self):
        words = [(0.0, 0.3, "Hello"), (0.3, 0.6, "world"), (0.6, 0.9, "again"),
                 (0.9, 1.2, "today"), (1.2, 1.5, "friends")]
        chunks = self.captions.chunk_words(words, 3)
        self.assertTrue(chunks)
        for _start, _end, text in chunks:
            self.assertLessEqual(len(text.split()), 3)
        self.assertEqual(sum(len(t.split()) for _, _, t in chunks), 5)
        self.assertEqual(chunks[0][0], 0.0)
        self.assertEqual(chunks[0][1], 0.9)
        self.assertEqual(chunks[0][2].lower(), "hello world again")

    def test_chunk_breaks_after_full_stop(self):
        words = [(0.0, 0.2, "Big"), (0.2, 0.4, "news."), (0.4, 0.6, "Now"), (0.6, 0.8, "more")]
        chunks = self.captions.chunk_words(words, 3)
        self.assertEqual(len(chunks), 2)
        self.assertTrue(chunks[0][2].lower().endswith("news."))
        self.assertEqual(chunks[1][2].lower(), "now more")

    def test_chunk_empty_input(self):
        self.assertEqual(self.captions.chunk_words([], 3), [])

    def test_format_ass_time(self):
        self.assertEqual(self.captions.format_ass_time(3.5), "0:00:03.50")
        self.assertEqual(self.captions.format_ass_time(65.25), "0:01:05.25")
        self.assertEqual(self.captions.format_ass_time(0), "0:00:00.00")

    def test_build_ass_header_and_dialogue(self):
        chunks = [(0.0, 1.0, "hello world"), (1.0, 2.0, "second chunk")]
        ass = self.captions.build_ass(chunks, self.cfg, "Test title", "@test", 5.0)
        self.assertIn("[Script Info]", ass)
        self.assertIn("PlayResX: 1080", ass)
        self.assertIn("PlayResY: 1920", ass)
        self.assertIn("Dialogue:", ass)

    def test_build_ass_does_not_leak_raw_braces_from_text(self):
        chunks = [(0.0, 1.0, "a{b}c")]
        ass = self.captions.build_ass(chunks, self.cfg, "", "", 2.0)
        dialogue = [line for line in ass.splitlines() if "a" in line and line.startswith("Dialogue: 1")]
        self.assertEqual(len(dialogue), 1)
        self.assertNotIn("{b}", dialogue[0])


if __name__ == "__main__":
    unittest.main()
