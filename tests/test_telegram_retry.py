import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

from cybershorts import telegram


def ok(result=None):
    r = mock.Mock()
    r.json.return_value = {"ok": True, "result": result or {"message_id": 7}}
    return r


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.tg = telegram.Telegram("123:SECRET", 42)

    def test_retries_then_succeeds(self):
        with mock.patch.object(telegram.requests, "post",
                               side_effect=[requests.ConnectionError("x"), ok()]) as post, \
             mock.patch.object(telegram.time, "sleep") as sleep:
            self.assertEqual(self.tg.send_message("hi")["message_id"], 7)
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(telegram.RETRY_DELAYS[0])

    def test_gives_up_without_leaking_token(self):
        err = requests.ConnectionError("Max retries exceeded with url: /bot123:SECRET/sendMessage")
        with mock.patch.object(telegram.requests, "post", side_effect=err), \
             mock.patch.object(telegram.time, "sleep"):
            with self.assertRaises(RuntimeError) as cm:
                self.tg.send_message("hi")
        self.assertNotIn("SECRET", str(cm.exception))
        self.assertIn("Max retries", str(cm.exception))

    def test_video_reopened_each_attempt(self):
        with tempfile.TemporaryDirectory() as d:
            vid = Path(d) / "v.mp4"
            vid.write_bytes(b"0123456789")
            seen = []

            def fake_post(url, data=None, files=None, timeout=None):
                seen.append(files["video"][1].read())
                if len(seen) == 1:
                    raise requests.ConnectionError("reset")
                return ok()

            with mock.patch.object(telegram.requests, "post", side_effect=fake_post), \
                 mock.patch.object(telegram.time, "sleep"):
                self.tg.send_video(vid, "cap")
        self.assertEqual(seen, [b"0123456789", b"0123456789"])

    def test_get_updates_does_not_retry(self):
        with mock.patch.object(telegram.requests, "post", side_effect=requests.ConnectionError("x")) as post, \
             mock.patch.object(telegram.time, "sleep") as sleep:
            with self.assertRaises(RuntimeError):
                self.tg.get_updates(0)
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()
