import os
import tempfile
import unittest
from unittest import mock

from googleapiclient.errors import HttpError

from cybershorts import youtube


class Cfg:
    def get(self, k, d=None):
        return d


def http_err(status):
    return HttpError(mock.Mock(status=status, reason="x"), b"{}")


class ThumbTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".jpg")
        os.write(fd, b"\xff\xd8jpg")
        os.close(fd)
        self.yt = mock.Mock()
        self.addCleanup(os.unlink, self.path)

    def run_set(self, side_effect):
        self.yt.thumbnails.return_value.set.return_value.execute.side_effect = side_effect
        with mock.patch.object(youtube, "get_service", return_value=self.yt), \
                mock.patch.object(youtube.time, "sleep") as sl:
            return youtube.set_thumbnail(Cfg(), "VID", self.path), sl

    def test_ok(self):
        (ok, reason), _ = self.run_set([{}])
        self.assertTrue(ok)
        self.assertEqual(self.yt.thumbnails.return_value.set.call_args.kwargs["videoId"], "VID")

    def test_retry_once_on_5xx(self):
        (ok, _), sl = self.run_set([http_err(503), {}])
        self.assertTrue(ok)
        sl.assert_called_once()

    def test_5xx_twice_fails_without_raising(self):
        (ok, reason), _ = self.run_set([http_err(500), http_err(500)])
        self.assertFalse(ok)
        self.assertIn("500", reason)

    def test_403_maps_to_verify(self):
        (ok, reason), sl = self.run_set([http_err(403)])
        self.assertFalse(ok)
        self.assertIn("verify phone", reason)
        sl.assert_not_called()

    def test_never_raises(self):
        with mock.patch.object(youtube, "get_service", side_effect=RuntimeError("token")):
            ok, reason = youtube.set_thumbnail(Cfg(), "V", self.path)
        self.assertFalse(ok)
        self.assertIn("RuntimeError", reason)

    def test_missing_file(self):
        ok, reason = youtube.set_thumbnail(Cfg(), "V", "/nonexistent.jpg")
        self.assertFalse(ok)

    def test_upload_category_override(self):
        yt = mock.Mock()
        req = yt.videos.return_value.insert
        req.return_value.next_chunk.return_value = (None, {"id": "X"})
        with mock.patch.object(youtube, "get_service", return_value=yt), \
                mock.patch("googleapiclient.http.MediaFileUpload"):
            youtube.upload(Cfg(), self.path, "t", "d", ["a"], category_id="27")
        self.assertEqual(req.call_args.kwargs["body"]["snippet"]["categoryId"], "27")
        with mock.patch.object(youtube, "get_service", return_value=yt), \
                mock.patch("googleapiclient.http.MediaFileUpload"):
            youtube.upload(Cfg(), self.path, "t", "d")
        self.assertEqual(req.call_args.kwargs["body"]["snippet"]["categoryId"], "28")


if __name__ == "__main__":
    unittest.main()
