import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

KEY = "xi_SECRETKEY_0123456789"
AUDIO = b"ID3" + b"\x01" * 3000


class Resp:
    def __init__(self, status=200, content=AUDIO, body=None, ok=None):
        self.status_code = status
        self.content = content
        self._body = body
        self.ok = (status < 400) if ok is None else ok
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def balance(remaining=1_000_000):
    return Resp(200, b"", {"character_limit": remaining, "character_count": 0})


def fake_pp(raw, out, item):
    Path(out).write_bytes(b"MP3" + bytes(raw)[:20] + b"x" * 600)
    return Path(out)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("APP_DIR")
        os.environ["APP_DIR"] = self._tmp.name
        from cybershorts import config, soundkit
        self.sk = soundkit
        self.cfg = config.load()
        self.cfg.elevenlabs_api_key = KEY
        self.cfg.raw["sound"] = {"kit": [
            {"name": "hook", "kind": "sfx", "prompt": "glitch", "seconds": 1.0, "loop": False},
            {"name": "whoosh", "kind": "sfx", "prompt": "air", "seconds": 0.6, "loop": False},
            {"name": "bed1", "kind": "music", "prompt": "techno", "seconds": 60, "loop": True},
        ]}
        self.dir = self.sk.kit_dir(self.cfg)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("APP_DIR", None)
        else:
            os.environ["APP_DIR"] = self._old
        self._tmp.cleanup()

    def run_gen(self, post, get=None, **kw):
        get = get or mock.Mock(return_value=balance())
        with mock.patch.object(self.sk.requests, "post", side_effect=post) as p, \
             mock.patch.object(self.sk.requests, "get", get), \
             mock.patch.object(self.sk, "postprocess", side_effect=fake_pp), \
             mock.patch.object(self.sk, "_probe_duration", return_value=1.0), \
             mock.patch.object(self.sk.time, "sleep"):
            res = self.sk.generate(self.cfg, **kw)
        return res, p


class ClientTests(Base):
    def test_sfx_request_shape(self):
        with mock.patch.object(self.sk.requests, "post", return_value=Resp()) as p:
            data = self.sk.SoundClient(self.cfg).sfx("glitch", 1.0)
        self.assertEqual(data, AUDIO)
        args, kw = p.call_args
        self.assertEqual(args[0], "https://api.elevenlabs.io/v1/sound-generation")
        self.assertEqual(kw["headers"]["xi-api-key"], KEY)
        self.assertEqual(kw["params"], {"output_format": "mp3_44100_192"})
        self.assertEqual(kw["json"], {"text": "glitch", "model_id": "eleven_text_to_sound_v3",
                                      "duration_seconds": 1.0, "prompt_influence": 0.5})
        self.assertEqual(kw["timeout"], 120)

    def test_sfx_loop_flag(self):
        with mock.patch.object(self.sk.requests, "post", return_value=Resp()) as p:
            self.sk.SoundClient(self.cfg).sfx("x", 40, loop=True)
        self.assertTrue(p.call_args[1]["json"]["loop"])
        self.assertEqual(p.call_args[1]["json"]["duration_seconds"], 30.0)

    def test_music_request_shape(self):
        with mock.patch.object(self.sk.requests, "post", return_value=Resp()) as p:
            self.sk.SoundClient(self.cfg).music("techno", 75000)
        args, kw = p.call_args
        self.assertEqual(args[0], "https://api.elevenlabs.io/v1/music")
        self.assertEqual(kw["json"], {"prompt": "techno", "model_id": "music_v2",
                                      "music_length_ms": 75000, "force_instrumental": True})
        self.assertEqual(kw["timeout"], 300)

    def test_music_422_retries_without_instrumental(self):
        seq = [Resp(422, b"", {"detail": [{"msg": "bad force_instrumental"}]}), Resp()]
        with mock.patch.object(self.sk.requests, "post", side_effect=seq) as p:
            self.sk.SoundClient(self.cfg).music("t", 5000)
        self.assertEqual(p.call_count, 2)
        self.assertNotIn("force_instrumental", p.call_args[1]["json"])

    def test_balance(self):
        with mock.patch.object(self.sk.requests, "get", return_value=Resp(200, b"", {
                "character_limit": 1000, "character_count": 250})) as g:
            self.assertEqual(self.sk.SoundClient(self.cfg).balance(), 750)
        self.assertEqual(g.call_args[0][0], "https://api.elevenlabs.io/v1/user/subscription")
        with mock.patch.object(self.sk.requests, "get", side_effect=requests.ConnectionError("x")):
            self.assertIsNone(self.sk.SoundClient(self.cfg).balance())

    def test_fatal_statuses(self):
        for status, body in ((401, {"detail": {"status": "invalid_api_key"}}), (402, {"detail": "pay"}),
                             (403, {"detail": "no"}), (400, {"detail": "quota_exceeded"})):
            with mock.patch.object(self.sk.requests, "post", return_value=Resp(status, b"", body)) as p:
                with self.assertRaises(self.sk.KitFatal):
                    self.sk.SoundClient(self.cfg).sfx("x", 1)
            self.assertEqual(p.call_count, 1)

    def test_422_is_item_error(self):
        with mock.patch.object(self.sk.requests, "post",
                               return_value=Resp(422, b"", {"detail": [{"loc": ["body"], "msg": "bad"}]})):
            with self.assertRaises(self.sk.KitItemError) as cm:
                self.sk.SoundClient(self.cfg).sfx("x", 1)
        self.assertIn("bad", str(cm.exception))

    def test_429_retried_then_success(self):
        seq = [Resp(429, b"", {"detail": "slow"}), Resp(429, b"", {"detail": "slow"}), Resp()]
        with mock.patch.object(self.sk.requests, "post", side_effect=seq) as p, \
             mock.patch.object(self.sk.time, "sleep") as sl:
            self.assertEqual(self.sk.SoundClient(self.cfg).sfx("x", 1), AUDIO)
        self.assertEqual(p.call_count, 3)
        self.assertEqual(sl.call_count, 2)

    def test_non_audio_200(self):
        for content in (b'{"detail": "nope"}' + b" " * 2000, b"short"):
            with mock.patch.object(self.sk.requests, "post", return_value=Resp(200, content)):
                with self.assertRaises(self.sk.KitItemError):
                    self.sk.SoundClient(self.cfg).sfx("x", 1)

    def test_key_never_in_errors_or_logs(self):
        leak = {"detail": {"message": f"bad key {KEY} for request"}}
        with mock.patch.object(self.sk.requests, "post", return_value=Resp(401, b"", leak)):
            with self.assertRaises(self.sk.KitFatal) as cm:
                self.sk.SoundClient(self.cfg).sfx("x", 1)
        self.assertNotIn(KEY, str(cm.exception))
        with mock.patch.object(self.sk.requests, "post", return_value=Resp(422, b"", {
                "detail": [{"msg": f"echo {KEY}"}]})), \
             self.assertLogs("cybershorts.soundkit", level="INFO") as logs:
            res, _ = self.run_gen(lambda *a, **k: Resp(422, b"", {"detail": [{"msg": f"echo {KEY}"}]}))
        self.assertTrue(all(r["status"] == "error" for r in res if r["status"] != "skipped"))
        self.assertNotIn(KEY, "\n".join(logs.output))
        self.assertNotIn(KEY, json.dumps(res))

    def test_network_error_item_error(self):
        with mock.patch.object(self.sk.requests, "post", side_effect=requests.ConnectionError(KEY)), \
             mock.patch.object(self.sk.time, "sleep"):
            with self.assertRaises(self.sk.KitItemError) as cm:
                self.sk.SoundClient(self.cfg).sfx("x", 1)
        self.assertNotIn(KEY, str(cm.exception))


class GenerateTests(Base):
    def ok_post(self, *a, **k):
        return Resp()

    def test_estimates(self):
        it = {"kind": "sfx", "seconds": 5.6}
        self.assertEqual(self.sk.estimate(it), 224)
        self.assertEqual(self.sk.estimate({"kind": "music", "seconds": 60}, 2), 1800)

    def test_dry_run_no_http_no_files(self):
        boom = mock.Mock(side_effect=AssertionError("HTTP in dry run"))
        with mock.patch.object(self.sk.requests, "post", boom), mock.patch.object(self.sk.requests, "get", boom):
            res = self.sk.generate(self.cfg, dry_run=True, variants=2)
        boom.assert_not_called()
        self.assertFalse(self.dir.exists())
        self.assertEqual({r["name"]: r["estimate"] for r in res}, {"hook": 80, "whoosh": 48, "bed1": 1800})
        self.assertTrue(all(r["status"] == "dry-run" for r in res))

    def test_default_kit_when_config_empty(self):
        self.cfg.raw.pop("sound")
        res = self.sk.generate(self.cfg, dry_run=True)
        self.assertEqual(len(res), 9)
        self.assertEqual(next(r for r in res if r["name"] == "hook")["estimate"], 40)

    def test_generates_manifest_and_telegram(self):
        tg = mock.Mock()
        res, p = self.run_gen(self.ok_post, variants=2, tg=tg)
        self.assertEqual([r["status"] for r in res], ["ok"] * 3)
        for f in ("hook.mp3", "hook.v2.mp3", "whoosh.mp3", "bed1.v2.mp3"):
            self.assertTrue((self.dir / f).exists(), f)
        self.assertEqual(tg.send_audio.call_count, 6)
        self.assertEqual(p.call_count, 6)
        man = json.loads((self.dir / "manifest.json").read_text())
        self.assertEqual([m["name"] for m in man], ["hook", "whoosh", "bed1"])
        self.assertEqual(man[0]["model"], "eleven_text_to_sound_v3")
        self.assertEqual(man[2]["model"], "music_v2")
        self.assertEqual(man[0]["variants"], ["hook.mp3", "hook.v2.mp3"])
        self.assertFalse(list(self.dir.glob("*.tmp*")))

    def test_telegram_failure_only_warns(self):
        tg = mock.Mock()
        tg.send_audio.side_effect = RuntimeError("boom")
        with self.assertLogs("cybershorts.soundkit", level="WARNING"):
            res, _ = self.run_gen(self.ok_post, tg=tg)
        self.assertEqual([r["status"] for r in res], ["ok"] * 3)

    def test_credits_used_from_balance_diff(self):
        seq = iter([100000, 99960, 99920, 99000])
        get = mock.Mock(side_effect=lambda *a, **k: Resp(200, b"", {
            "character_limit": next(seq), "character_count": 0}))
        res, _ = self.run_gen(self.ok_post, get=get)
        self.assertEqual([r["credits_used"] for r in res][:2], [40, 40])

    def test_skips_existing_and_only_keeps_others(self):
        self.dir.mkdir(parents=True)
        for n in ("hook", "whoosh"):
            (self.dir / f"{n}.mp3").write_bytes(b"OLD" + n.encode())
        os.utime(self.dir / "hook.mp3", (1_000_000, 1_000_000))
        res, p = self.run_gen(self.ok_post)  # only bed1 missing
        self.assertEqual([r["status"] for r in res], ["skipped", "skipped", "ok"])
        self.assertEqual(p.call_count, 1)
        self.assertEqual((self.dir / "hook.mp3").read_bytes(), b"OLDhook")
        self.assertEqual(int((self.dir / "hook.mp3").stat().st_mtime), 1_000_000)
        before = (self.dir / "whoosh.mp3").stat().st_mtime_ns
        res, p = self.run_gen(self.ok_post, only=["hook"])
        self.assertEqual([r["status"] for r in res], ["ok", "skipped", "skipped"])
        self.assertNotEqual((self.dir / "hook.mp3").read_bytes(), b"OLDhook")
        self.assertEqual((self.dir / "whoosh.mp3").read_bytes(), b"OLDwhoosh")
        self.assertEqual((self.dir / "whoosh.mp3").stat().st_mtime_ns, before)

    def test_failed_only_keeps_old_file(self):
        self.dir.mkdir(parents=True)
        (self.dir / "hook.mp3").write_bytes(b"OLD")
        res, _ = self.run_gen(lambda *a, **k: Resp(422, b"", {"detail": "bad"}), only=["hook"])
        self.assertEqual(res[0]["status"], "error")
        self.assertEqual((self.dir / "hook.mp3").read_bytes(), b"OLD")
        self.assertFalse(list(self.dir.glob("*.tmp*")))

    def test_postprocess_failure_keeps_old_file(self):
        self.dir.mkdir(parents=True)
        (self.dir / "hook.mp3").write_bytes(b"OLD")
        with mock.patch.object(self.sk.requests, "post", return_value=Resp()), \
             mock.patch.object(self.sk.requests, "get", return_value=balance()), \
             mock.patch.object(self.sk, "postprocess", side_effect=self.sk.KitItemError("ffmpeg failed")):
            res = self.sk.generate(self.cfg, only=["hook"])
        self.assertEqual(res[0]["status"], "error")
        self.assertEqual((self.dir / "hook.mp3").read_bytes(), b"OLD")

    def test_422_skips_item_and_continues(self):
        def post(url, **kw):
            return Resp(422, b"", {"detail": "bad"}) if kw["json"].get("text") == "glitch" else Resp()
        res, _ = self.run_gen(post)
        self.assertEqual([r["status"] for r in res], ["error", "ok", "ok"])
        self.assertFalse((self.dir / "hook.mp3").exists())
        self.assertTrue((self.dir / "whoosh.mp3").exists())

    def test_auth_error_stops_run_but_keeps_manifest(self):
        def post(url, **kw):
            return Resp(402, b"", {"detail": "payment"}) if kw["json"].get("text") == "air" else Resp()
        with self.assertRaises(self.sk.KitFatal):
            self.run_gen(post)
        self.assertTrue((self.dir / "hook.mp3").exists())
        self.assertFalse((self.dir / "bed1.mp3").exists())
        man = json.loads((self.dir / "manifest.json").read_text())
        self.assertEqual([m["name"] for m in man], ["hook"])

    def test_balance_refusal(self):
        get = mock.Mock(return_value=balance(100))
        with self.assertRaises(self.sk.KitFatal) as cm:
            self.run_gen(self.ok_post, get=get)
        self.assertIn("credits", str(cm.exception))
        self.assertFalse(self.dir.exists())

    def test_balance_unreadable_warns_and_continues(self):
        get = mock.Mock(side_effect=requests.ConnectionError("x"))
        with self.assertLogs("cybershorts.soundkit", level="WARNING"):
            res, _ = self.run_gen(self.ok_post, get=get)
        self.assertEqual([r["status"] for r in res], ["ok"] * 3)
        self.assertIsNone(res[0]["credits_used"])

    def test_unknown_only_name(self):
        with self.assertRaises(self.sk.KitFatal):
            self.sk.generate(self.cfg, only=["nope"], dry_run=True)

    def test_use_swaps_variant(self):
        self.dir.mkdir(parents=True)
        (self.dir / "hook.mp3").write_bytes(b"ONE")
        (self.dir / "hook.v2.mp3").write_bytes(b"TWO")
        boom = mock.Mock(side_effect=AssertionError("HTTP"))
        with mock.patch.object(self.sk.requests, "post", boom), mock.patch.object(self.sk.requests, "get", boom):
            res = self.sk.generate(self.cfg, use={"hook": 2})
        self.assertEqual(res[0]["status"], "swapped")
        self.assertEqual((self.dir / "hook.mp3").read_bytes(), b"TWO")
        self.assertEqual((self.dir / "hook.v2.mp3").read_bytes(), b"ONE")
        with self.assertRaises(self.sk.KitFatal):
            self.sk.generate(self.cfg, use={"hook": 5})


class PostprocessTests(Base):
    def test_sfx_filters(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            if "volumedetect" in " ".join(cmd):
                return mock.Mock(returncode=0, stderr="max_volume: -7.5 dB\n", stdout="")
            Path(cmd[-1]).write_bytes(b"x" * 1000)
            return mock.Mock(returncode=0, stderr="", stdout="")

        out = Path(self._tmp.name) / "o.mp3"
        with mock.patch.object(self.sk.subprocess, "run", side_effect=run):
            self.sk.postprocess(b"raw", out, {"kind": "sfx"})
        self.assertIn("silenceremove=start_periods=1", calls[0][calls[0].index("-af") + 1])
        af = calls[1][calls[1].index("-af") + 1]
        self.assertIn("silenceremove", af)
        self.assertIn("volume=6.50dB", af)  # -1 - (-7.5)
        self.assertIn("192k", calls[1])
        self.assertTrue(out.exists())
        self.assertFalse(list(Path(self._tmp.name).glob("*.raw")))

    def test_music_loudnorm_and_loop(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            if cmd[0] == "ffprobe":
                return mock.Mock(returncode=0, stdout="75.0\n", stderr="")
            Path(cmd[-1]).write_bytes(b"x" * 1000)
            return mock.Mock(returncode=0, stderr="", stdout="")

        out = Path(self._tmp.name) / "m.mp3"
        with mock.patch.object(self.sk.subprocess, "run", side_effect=run):
            self.sk.postprocess(b"raw", out, {"kind": "music", "loop": False})
            self.assertIn("loudnorm=I=-16:TP=-1.5", calls[-1])
            self.sk.postprocess(b"raw", out, {"kind": "music", "loop": True})
        fc = calls[-1][calls[-1].index("-filter_complex") + 1]
        self.assertIn("afade=t=out:st=0:d=1.5", fc)
        self.assertIn("amix=inputs=2:duration=longest:normalize=0", fc)
        self.assertIn("loudnorm=I=-16:TP=-1.5", fc)
        self.assertIn("atrim=start=73.500:end=75.000", fc)

    def test_ffmpeg_failure_is_item_error(self):
        with mock.patch.object(self.sk.subprocess, "run",
                               return_value=mock.Mock(returncode=1, stderr="bad", stdout="")):
            with self.assertRaises(self.sk.KitItemError):
                self.sk.postprocess(b"raw", Path(self._tmp.name) / "o.mp3", {"kind": "music"})

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg not installed")
    def test_real_ffmpeg(self):
        src = Path(self._tmp.name) / "src.mp3"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=8", "-ar", "44100", str(src)], check=True)
        sfx = self.sk.postprocess(src.read_bytes(), Path(self._tmp.name) / "sfx.mp3", {"kind": "sfx"})
        self.assertGreater(sfx.stat().st_size, 1000)
        loop = self.sk.postprocess(src.read_bytes(), Path(self._tmp.name) / "loop.mp3",
                                   {"kind": "music", "loop": True})
        dur = self.sk._probe_duration(loop)
        self.assertAlmostEqual(dur, 6.5, delta=0.3)


if __name__ == "__main__":
    unittest.main()
