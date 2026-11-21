import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _run(*args):
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args], check=True)


class MusicBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("APP_DIR")
        os.environ["APP_DIR"] = self._tmp.name
        from cybershorts import config, music
        self.music = music
        self.cfg = config.load()

    def tearDown(self):
        if self._old is None:
            os.environ.pop("APP_DIR", None)
        else:
            os.environ["APP_DIR"] = self._old
        self._tmp.cleanup()


class PickTests(MusicBase):
    def test_no_tracks(self):
        self.assertIsNone(self.music.pick_track(self.cfg))
        out, why = self.music.add_music(self.cfg, 1, Path("x.mp4"))
        self.assertIsNone(out)
        self.assertIn("no tracks", why)

    def test_never_same_twice_in_a_row(self):
        d = self.music.music_dir(self.cfg)
        for n in ("a.mp3", "b.m4a", "notes.txt"):
            (d / n).write_bytes(b"x")
        picks = [self.music.pick_track(self.cfg).name for _ in range(20)]
        self.assertTrue(all(p in ("a.mp3", "b.m4a") for p in picks))
        self.assertTrue(all(x != y for x, y in zip(picks, picks[1:])))

    def test_disabled(self):
        self.cfg.raw["music"]["enabled"] = False
        self.assertEqual(self.music.add_music(self.cfg, 1, Path("x.mp4")), (None, "off"))


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class MixTests(MusicBase):
    def test_mix_copies_video_and_ducks(self):
        work = Path(self._tmp.name)
        clean = work / "clean.mp4"
        # 4 s video; voice = tone during 0-2 s, silence 2-4 s
        _run("-f", "lavfi", "-i", "testsrc2=s=320x568:r=30:d=4",
             "-f", "lavfi", "-i", "sine=f=200:d=2,apad=pad_dur=2",
             "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(clean))
        track = self.music.music_dir(self.cfg) / "bed.mp3"
        _run("-f", "lavfi", "-i", "sine=f=2000:d=1.5", "-c:a", "libmp3lame", str(track))  # shorter: must loop
        out, name = self.music.add_music(self.cfg, 7, clean)
        self.assertIsNotNone(out)
        self.assertEqual(name, "bed")
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name",
                                "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
                               capture_output=True, text=True).stdout
        self.assertIn("h264", probe)
        self.assertIn("aac", probe)
        dur = float(probe.strip().splitlines()[-1])
        self.assertAlmostEqual(dur, 4.0, delta=0.3)

        def rms(start):
            r = subprocess.run(["ffmpeg", "-hide_banner", "-ss", str(start), "-t", "0.6", "-i", str(out),
                                "-af", "bandpass=f=2000:width_type=q:w=8,bandpass=f=2000:width_type=q:w=8,astats=metadata=0", "-f", "null", "-"],
                               capture_output=True, text=True).stderr
            line = [x for x in r.splitlines() if "RMS level dB" in x][-1]
            return float(line.split(":")[-1])
        # music (2 kHz, isolated by the band-pass) is quieter while the voice talks than after it stops
        self.assertLess(rms(1.0), rms(2.3) - 3)


class PublishRoutingTests(unittest.TestCase):
    def setUp(self):
        from tests.test_pipeline import PipelineTests
        self.t = PipelineTests("test_both_ok")
        self.t.setUp()

    def tearDown(self):
        self.t.tearDown()

    def test_music_version_to_youtube_clean_to_tiktok(self):
        from cybershorts import music
        mv = music.music_version(self.t.cfg, self.t.id)
        mv.write_bytes(b"music")
        _, yt, tt = self.t._publish({"yt", "tt"})
        self.assertEqual(Path(yt.call_args.args[1]), mv)
        self.assertEqual(Path(tt.call_args.args[1]), self.t.video)

    def test_on_tiktok_true(self):
        from cybershorts import music
        self.t.cfg.raw["music"]["on_tiktok"] = True
        mv = music.music_version(self.t.cfg, self.t.id)
        mv.write_bytes(b"music")
        _, _, tt = self.t._publish({"tt"})
        self.assertEqual(Path(tt.call_args.args[1]), mv)

    def test_no_music_file_uses_clean(self):
        _, yt, _ = self.t._publish({"yt"})
        self.assertEqual(Path(yt.call_args.args[1]), self.t.video)
