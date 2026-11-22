"""Golden regression for the ffmpeg command lines that render_draft builds today.

Every ffmpeg argv produced by a Shorts render (render + optional music mix) is captured with
subprocess mocked out, normalized (temp APP_DIR -> "<APP>") and compared to
tests/fixtures/ffmpeg_golden/<case>.json. Regenerate with REGEN_GOLDEN=1.

Use run_case(name, tweak=...) to build further cases (e.g. sound disabled, empty kit):
`tweak` receives the loaded Config before render_draft runs.
"""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

FIX = Path(__file__).parent / "fixtures" / "ffmpeg_golden"
SCRIPT = "Researchers found a flaw today.\n\nAttackers exploit it now.\n\nPatch and stay safe."
CARD = {"cve_id": "CVE-2025-12345", "cvss": 9.8, "severity": "Critical", "products": ["Acme VPN", "Acme Edge"],
        "exploited": True, "patch_available": True}
BASE_DATA = {"script": SCRIPT, "on_screen_title": "Flaw exploited", "keywords": ["flaw"],
             "youtube_title": "T #Shorts", "description": "d", "hashtags": ["a"],
             "fact_check": {"ok": True, "issues": []}, "pexels_query": "technology"}
CASES = {
    "regular_beats": {"data": {}, "music": False},
    "breaking_card": {"data": {"breaking": True, "data_card": CARD, "on_screen_title": "Big VPN flaw"},
                      "music": False},
    "regular_music": {"data": {}, "music": True},
    "regular_no_music": {"data": {}, "music": False},
}
DURATION = 6.0


def _words():
    import re
    tokens = [t for t in SCRIPT.split() if re.sub(r"[^a-z0-9]", "", t.lower())]
    per = DURATION / len(tokens)
    return [(i * per, (i + 1) * per, t) for i, t in enumerate(tokens)]


def _fake_run(calls):
    """Stand-in for subprocess.run: records ffmpeg argvs, answers ffprobe, touches outputs."""
    def run(cmd, *args, **kwargs):
        if cmd and cmd[0] == "ffprobe":
            stdout = f"{DURATION:.6f}\n" if "format=duration" in cmd else ""
            return subprocess.CompletedProcess(cmd, 0, stdout, "")
        if cmd and cmd[0] == "ffmpeg":
            calls.append(list(cmd))
            out = Path(cmd[-1])
            if out.suffix in (".mp4", ".wav"):
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"fake")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        raise AssertionError(f"unexpected subprocess call: {cmd!r}")
    return run


def run_case(name, tweak=None, *, music=None, data=None):
    """Run render_draft for CASES[name] with everything external mocked.

    Returns {"argv": normalized ffmpeg argv lists, "ass": the normalized ASS subtitle file}.

    music: override whether a bed track is placed in the music folder.
    data: override/extend draft data fields.
    """
    spec = CASES[name]
    use_music = spec["music"] if music is None else music
    draft_data = {**BASE_DATA, **spec["data"], **(data or {})}
    with tempfile.TemporaryDirectory() as tmp:
        old = os.environ.get("APP_DIR")
        os.environ["APP_DIR"] = tmp
        try:
            from cybershorts import config, db, pipeline

            cfg = config.load()
            cfg.telegram_bot_token, cfg.telegram_chat_id = "t", "1"
            if use_music:
                (cfg.path(str(cfg.get("music.folder", "music"))) / "bed.mp3").write_bytes(b"fake")
            if tweak is not None:
                tweak(cfg)
            work = cfg.path("work")
            wav = work / "draft_audio.wav"
            wav.write_bytes(b"fake")
            conn = db.connect(cfg)
            db.init(conn)
            did = db.create_draft(conn, "http://x", "awaiting_review", json.dumps(draft_data),
                                  audio_path=str(wav))
            conn.close()

            def fake_broll(cfg_, n, query, prefix, **kw):
                clips = []
                for i in range(n):
                    p = Path(f"{prefix}_{i}.mp4")
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b"fake")
                    clips.append(p)
                return clips

            tg = mock.Mock()
            tg.send_video.return_value = {"result": {"message_id": 7}}
            calls: list[list[str]] = []
            with mock.patch("subprocess.run", side_effect=_fake_run(calls)), \
                 mock.patch("subprocess.Popen", side_effect=AssertionError("Popen not expected")), \
                 mock.patch("cybershorts.captions.transcribe_words", return_value=_words()), \
                 mock.patch("cybershorts.visuals.fetch_broll_set", side_effect=fake_broll), \
                 mock.patch("cybershorts.pipeline._telegram", return_value=tg):
                pipeline.render_draft(cfg, did)
            roots = {tmp, os.path.realpath(tmp)}
            ass = (cfg.path("work") / f"draft_{did}.ass").read_text(encoding="utf-8")
            return {"argv": [[_normalize(a, roots) for a in argv] for argv in calls],
                    "ass": _normalize(ass, roots)}
        finally:
            if old is None:
                os.environ.pop("APP_DIR", None)
            else:
                os.environ["APP_DIR"] = old


def _normalize(arg: str, roots) -> str:
    for r in sorted(roots, key=len, reverse=True):
        arg = arg.replace(r, "<APP>")
    return arg


class SoundRegressionGolden(unittest.TestCase):
    def test_golden_ffmpeg_argv(self):
        regen = os.environ.get("REGEN_GOLDEN") == "1"
        for name in CASES:
            with self.subTest(case=name):
                got = run_case(name)
                path = FIX / f"{name}.json"
                payload = {"case": name, **got}
                if regen:
                    FIX.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
                self.assertTrue(path.exists(), f"missing golden {path}; run with REGEN_GOLDEN=1")
                want = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(got["argv"], want["argv"])
                self.assertEqual(got["ass"], want["ass"])

    def _assert_golden(self, tweak, label):
        for name in CASES:
            with self.subTest(case=name, variant=label):
                got = run_case(name, tweak)
                want = json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))
                self.assertEqual(got["argv"], want["argv"])
                self.assertEqual(got["ass"], want["ass"])

    def test_sound_disabled_matches_golden(self):
        def off(cfg):
            cfg.raw.setdefault("sound", {})["enabled"] = False
            kit = cfg.path("brand", "sound")  # even with a kit present, disabled means untouched
            kit.mkdir(parents=True, exist_ok=True)
            for n in ("hook", "whoosh", "breaking", "card", "outro"):
                (kit / f"{n}.mp3").write_bytes(b"fake")
        self._assert_golden(off, "disabled")

    def test_empty_kit_matches_golden(self):
        def empty(cfg):
            cfg.raw.setdefault("sound", {})["enabled"] = True
            cfg.path("brand", "sound").mkdir(parents=True, exist_ok=True)
        self._assert_golden(empty, "empty kit")

    def test_cases_do_what_they_say(self):
        """Sanity: the golden cases actually exercise the paths they are named for."""
        regular = run_case("regular_beats")
        self.assertEqual(len(regular["argv"]), 1)
        self.assertIn("-filter_complex", regular["argv"][0])
        self.assertEqual(len(run_case("regular_no_music")["argv"]), 1)
        self.assertEqual(len(run_case("regular_music")["argv"]), 2)
        breaking = run_case("breaking_card")
        self.assertEqual(len(breaking["argv"]), 1)
        self.assertIn("BREAKING", breaking["ass"])
        self.assertIn("CVE-2025-12345", breaking["ass"])
        self.assertNotIn("CVE-2025-12345", regular["ass"])


if __name__ == "__main__":
    unittest.main()
