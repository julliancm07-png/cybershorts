import os
import tempfile
import unittest


class ExtractJsonTests(unittest.TestCase):
    def setUp(self):
        # writer imports config indirectly; keep APP_DIR isolated anyway
        self._tmp = tempfile.TemporaryDirectory()
        self._old_app_dir = os.environ.get("APP_DIR")
        os.environ["APP_DIR"] = self._tmp.name
        from cybershorts.writer import extract_json

        self.extract_json = extract_json

    def tearDown(self):
        if self._old_app_dir is None:
            os.environ.pop("APP_DIR", None)
        else:
            os.environ["APP_DIR"] = self._old_app_dir
        self._tmp.cleanup()

    def test_fenced_json_block(self):
        text = '```json\n{"script": "Hello there.", "hashtags": ["cyber"]}\n```'
        self.assertEqual(self.extract_json(text), {"script": "Hello there.", "hashtags": ["cyber"]})

    def test_fenced_block_without_language_tag(self):
        self.assertEqual(self.extract_json('```\n{"a": 2}\n```'), {"a": 2})

    def test_leading_and_trailing_prose(self):
        text = 'Sure! Here is the JSON you asked for:\n{"youtube_title": "Patch now #Shorts"}\nLet me know if you need more.'
        self.assertEqual(self.extract_json(text), {"youtube_title": "Patch now #Shorts"})

    def test_nested_objects(self):
        text = 'Result: {"outer": {"inner": {"k": [1, 2]}}, "flag": true}'
        data = self.extract_json(text)
        self.assertEqual(data["outer"]["inner"]["k"], [1, 2])
        self.assertIs(data["flag"], True)

    def test_no_json_raises(self):
        with self.assertRaises(ValueError):
            self.extract_json("There is no object in this reply.")


if __name__ == "__main__":
    unittest.main()
