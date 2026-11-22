import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class FakeRun:
    """subprocess.run stand-in: records commands, creates the output file (last arg) for ffmpeg."""

    def __init__(self, fail_on=None, sizes=None):
        self.calls = []
        self.fail_on = fail_on  # predicate(cmd) -> bool
        self.sizes = list(sizes or [])

    def __call__(self, cmd, **kw):
        self.calls.append((list(cmd), kw))
        r = mock.Mock(returncode=0, stdout="", stderr="")
        if cmd[0] == "ffprobe":
            r.stdout = "100,100"
            return r
        if self.fail_on and self.fail_on(cmd):
            r.returncode = 1
            r.stderr = "x" * 3000 + "BOOM_TAIL"
            return r
        size = self.sizes.pop(0) if self.sizes else 10
        Path(cmd[-1]).write_bytes(b"0" * size)
        return r

    def ff(self):
        return [c for c, _ in self.calls if c[0] == "ffmpeg"]


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("APP_DIR")
        os.environ["APP_DIR"] = self._tmp.name
        from cybershorts import config, recap_render
        self.rr = recap_render
        self.cfg = config.load()
        self.d = Path(self._tmp.name)
        self.wav = self.d / "a.wav"
        self.wav.write_bytes(b"x")
        self.ass = self.d / "w" / "a.ass"
        self.ass.parent.mkdir()
        self.ass.write_text("x")
        self.clip = self.d / "c.mp4"
        self.clip.write_bytes(b"x")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("APP_DIR", None)
        else:
            os.environ["APP_DIR"] = self._old
        self._tmp.cleanup()

    def render(self, fake, parts=3):
        with mock.patch("subprocess.run", fake):
            return self.rr.render_recap(self.cfg, self.wav, self.ass, [[self.clip, self.clip]] + [[]] * (parts - 1),
                                        [10.0] * parts, self.d / "out.mp4", prefix=self.d / "draft_1_recap")


class RenderRecapTests(Base):
    def test_stage_counts_and_threads(self):
        fake = FakeRun()
        out = self.render(fake, parts=3)
        self.assertTrue(out.exists())
        cmds = fake.ff()
        self.assertEqual(len(cmds), 3 + 1 + 1)
        self.assertTrue(all("-threads" in c and c[c.index("-threads") + 1] == "2" for c in cmds))
        self.assertEqual(sum("concat" in c and "copy" in c for c in cmds), 1)
        finals = [c for c in cmds if "-filter_complex" in c and "apad" in c[c.index("-filter_complex") + 1]]
        self.assertEqual(len(finals), 1)
        self.assertIn("ass=a.ass", finals[0][finals[0].index("-filter_complex") + 1])
        self.assertEqual(fake.calls[[c for c, _ in fake.calls].index(finals[0])][1]["cwd"], str(self.ass.parent))
        parts = cmds[:3]
        self.assertTrue(all("-an" in c and "ultrafast" in c for c in parts))
        self.assertEqual(parts[0].count("-i"), 2)  # two clips in part 1
        self.assertIn("lavfi", parts[1])  # no clips -> colour bg

    def test_temp_files_removed_on_success(self):
        self.render(FakeRun())
        self.assertEqual(list(self.d.glob("draft_1_recap_*")), [])

    def test_temp_removed_and_stderr_tail_on_final_failure(self):
        fake = FakeRun(fail_on=lambda c: "apad" in " ".join(c))
        with self.assertRaises(RuntimeError) as cm:
            self.render(fake)
        self.assertIn("BOOM_TAIL", str(cm.exception))
        self.assertLess(len(str(cm.exception)), 1700)
        self.assertEqual(list(self.d.glob("draft_1_recap_*")), [])

    def test_temp_removed_on_part_failure(self):
        calls = {"n": 0}

        def fail(c):
            calls["n"] += 1
            return calls["n"] == 2
        with self.assertRaises(RuntimeError):
            self.render(FakeRun(fail_on=fail))
        self.assertEqual(list(self.d.glob("draft_1_recap_*")), [])


class PreviewTests(Base):
    def test_bitrate_fits_300s(self):
        k = self.rr.preview_bitrate_kbps(300, 45, 64, 1500)
        self.assertLessEqual((k + 64) * 300 / 8 / 1024, 45)
        self.assertGreater(k, 200)

    def test_bitrate_cap_and_floor(self):
        self.assertEqual(self.rr.preview_bitrate_kbps(60, 45, 64, 1500), 1500)
        self.assertEqual(self.rr.preview_bitrate_kbps(3000, 45, 64, 1500), 200)

    def test_preview_retries_when_too_big(self):
        src = self.d / "s.mp4"
        src.write_bytes(b"x")
        big = 46 * 1024 * 1024
        fake = FakeRun(sizes=[big, 1000])
        with mock.patch("subprocess.run", fake):
            self.rr.render_preview(self.cfg, src, self.d / "p.mp4", 300)
        cmds = fake.ff()
        self.assertEqual(len(cmds), 2)
        b = [int(c[c.index("-b:v") + 1][:-1]) for c in cmds]
        self.assertEqual(b[1], int(b[0] * 0.7))
        self.assertTrue(all(c[c.index("-threads") + 1] == "2" for c in cmds))

    def test_preview_no_retry_when_small(self):
        src = self.d / "s.mp4"
        src.write_bytes(b"x")
        fake = FakeRun(sizes=[1000])
        with mock.patch("subprocess.run", fake):
            self.rr.render_preview(self.cfg, src, self.d / "p.mp4", 300)
        self.assertEqual(len(fake.ff()), 1)


class StillThumbTests(Base):
    def test_thumbnail_retries_at_q6(self):
        still = self.d / "s.png"
        still.write_bytes(b"x")
        fake = FakeRun(sizes=[2 * 1024 * 1024 + 1, 1000])
        with mock.patch("subprocess.run", fake):
            self.rr.render_thumbnail(self.cfg, still, self.ass, self.d / "t.jpg")
        cmds = fake.ff()
        self.assertEqual([c[c.index("-q:v") + 1] for c in cmds], ["3", "6"])
        self.assertTrue(all(c[c.index("-threads") + 1] == "2" for c in cmds))

    def test_thumbnail_single_pass_when_small(self):
        still = self.d / "s.png"
        still.write_bytes(b"x")
        fake = FakeRun(sizes=[1000])
        with mock.patch("subprocess.run", fake):
            self.rr.render_thumbnail(self.cfg, still, self.ass, self.d / "t.jpg")
        self.assertEqual(len(fake.ff()), 1)

    def test_still_without_clip_uses_colour(self):
        fake = FakeRun()
        with mock.patch("subprocess.run", fake):
            self.rr.extract_still(None, self.d / "s.png")
        self.assertIn("lavfi", fake.ff()[0])


if __name__ == "__main__":
    unittest.main()
