"""Failure alerts (cybershorts.alerts) and weekly backups (cybershorts.backup)."""
import copy
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cybershorts import alerts, backup, config

REPO = Path(__file__).resolve().parent.parent
TOKEN = "123456789:AAH" + "x" * 32


def make_cfg(app: Path, **env) -> config.Config:
    base = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42", "OPENROUTER_API_KEY": "sk-or-v1-" + "a" * 40}
    base.update(env)
    return config.Config(app, copy.deepcopy(config.DEFAULTS), base)


class ScrubTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = make_cfg(self.tmp, ELEVENLABS_API_KEY="elkey_abcdef123")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_configured_secrets_removed(self):
        text = f"url https://api.telegram.org/bot{TOKEN}/x key={self.cfg.openrouter_api_key} el elkey_abcdef123"
        out = alerts.scrub(self.cfg, text)
        self.assertNotIn(TOKEN, out)
        self.assertNotIn("sk-or-v1", out)
        self.assertNotIn("elkey_abcdef123", out)

    def test_unknown_tokens_removed(self):
        out = alerts.scrub(self.cfg, "Bearer ya29.abcdefghijklmnopqrstuvwxyz refresh 1//0gabcdefghijklmnopqrstuv api_key=zzz")
        self.assertNotIn("ya29.abc", out)
        self.assertNotIn("1//0gabc", out)
        self.assertIn("api_key=***", out)

    def test_chat_id_kept(self):
        self.assertIn("chat 42", alerts.scrub(self.cfg, "chat 42"))


class PickLinesTests(unittest.TestCase):
    def test_traceback_from_last_one(self):
        lines = ["start", "old line", "Traceback (most recent call last):", "  old", "E1",
                 "make_draft failed", "Traceback (most recent call last):", '  File "x"', "ValueError: boom"]
        out = alerts.pick_lines(lines, 25)
        self.assertEqual(out[0], "make_draft failed")
        self.assertEqual(out[-1], "ValueError: boom")
        self.assertNotIn("E1", out)

    def test_long_traceback_keeps_end(self):
        lines = ["Traceback (most recent call last):"] + [f"  frame {i}" for i in range(100)] + ["KeyError: x"]
        out = alerts.pick_lines(lines, 10)
        self.assertLessEqual(len(out), 10)
        self.assertEqual(out[-1], "KeyError: x")
        self.assertEqual(out[0], "Traceback (most recent call last):")

    def test_no_traceback_last_n(self):
        out = alerts.pick_lines([f"l{i}" for i in range(50)] + ["", "  "], 5)
        self.assertEqual(out, ["l45", "l46", "l47", "l48", "l49"])


class HintTests(unittest.TestCase):
    def test_hints(self):
        self.assertIn("disk", alerts.hint_for("OSError: [Errno 28] No space left on device"))
        self.assertIn("OpenRouter", alerts.hint_for("OpenRouter error 402: Insufficient credits"))
        self.assertIn("auth_youtube", alerts.hint_for("google.auth.exceptions.RefreshError: invalid_grant"))
        self.assertIn("internet", alerts.hint_for("NameResolutionError"))
        self.assertIn("time limit", alerts.hint_for("anything", "timeout"))
        self.assertIn("memory", alerts.hint_for("", "oom-kill"))
        self.assertEqual(alerts.hint_for("ValueError: something else"), "")


class ParseInstanceTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(alerts.parse_instance("fail:cybershorts-draft"), ("fail", "cybershorts-draft.service"))
        self.assertEqual(alerts.parse_instance("ok:cybershorts-bot.service"), ("ok", "cybershorts-bot.service"))
        for bad in ("cybershorts-draft", "boom:x", "fail:"):
            with self.assertRaises(ValueError):
                alerts.parse_instance(bad)


class HandleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = make_cfg(self.tmp)
        self.sent = []
        p1 = mock.patch.object(alerts, "_send", side_effect=lambda cfg, text: self.sent.append(text) or True)
        p2 = mock.patch.object(alerts, "unit_info",
                               return_value={"Result": "exit-code", "ExecMainStatus": "1", "InvocationID": "abc"})
        p3 = mock.patch.object(alerts, "recent_logs", return_value=[
            "make_draft failed", "Traceback (most recent call last):",
            f"RuntimeError: Telegram failed for bot{TOKEN}"])
        for p in (p1, p2, p3):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_first_failure_alerts_with_logs_and_no_secret(self):
        self.assertTrue(alerts.handle(self.cfg, "fail", "cybershorts-draft", now=1000))
        self.assertEqual(len(self.sent), 1)
        msg = self.sent[0]
        self.assertIn("cybershorts-draft failed", msg)
        self.assertIn("exit-code, exit 1", msg)
        self.assertIn("RuntimeError", msg)
        self.assertNotIn(TOKEN, msg)
        self.assertIn("journalctl -u cybershorts-draft", msg)

    def test_repeat_muted_then_allowed(self):
        alerts.handle(self.cfg, "fail", "cybershorts-breaking", now=1000)
        alerts.handle(self.cfg, "fail", "cybershorts-breaking", now=1000 + 30 * 60)
        self.assertEqual(len(self.sent), 1)
        alerts.handle(self.cfg, "fail", "cybershorts-breaking", now=1000 + 181 * 60)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("Failure #3", self.sent[1])

    def test_units_muted_independently(self):
        alerts.handle(self.cfg, "fail", "cybershorts-breaking", now=1000)
        alerts.handle(self.cfg, "fail", "cybershorts-draft", now=1001)
        self.assertEqual(len(self.sent), 2)

    def test_ok_after_failure_sends_recovery_once(self):
        alerts.handle(self.cfg, "fail", "cybershorts-draft", now=1000)
        alerts.handle(self.cfg, "fail", "cybershorts-draft", now=1100)
        alerts.handle(self.cfg, "ok", "cybershorts-draft", now=1200)
        self.assertIn("working again", self.sent[-1])
        self.assertIn("failed 2 times", self.sent[-1])
        n = len(self.sent)
        alerts.handle(self.cfg, "ok", "cybershorts-draft", now=1300)
        self.assertEqual(len(self.sent), n)

    def test_ok_without_failure_is_silent(self):
        self.assertTrue(alerts.handle(self.cfg, "ok", "cybershorts-analytics", now=1000))
        self.assertEqual(self.sent, [])

    def test_send_failure_retries_next_time(self):
        alerts._send.side_effect = lambda cfg, text: False
        self.assertFalse(alerts.handle(self.cfg, "fail", "cybershorts-draft", now=1000))
        alerts._send.side_effect = lambda cfg, text: self.sent.append(text) or True
        alerts.handle(self.cfg, "fail", "cybershorts-draft", now=1060)
        self.assertEqual(len(self.sent), 1)

    def test_disabled(self):
        self.cfg.raw["alerts"]["enabled"] = False
        alerts.handle(self.cfg, "fail", "cybershorts-draft", now=1000)
        self.assertEqual(self.sent, [])

    def test_bot_note(self):
        alerts.handle(self.cfg, "fail", "cybershorts-bot", now=1000)
        self.assertIn("restarts the bot", self.sent[0])


class UnitFileTests(unittest.TestCase):
    def test_jobs_have_failure_hooks(self):
        sysd = REPO / "systemd"
        for svc in sysd.glob("cybershorts-*.service"):
            text = svc.read_text()
            if svc.name.startswith("cybershorts-alert@"):
                self.assertNotIn("OnFailure", text)  # never alert on the alerter (loop)
                self.assertIn("SupplementaryGroups=systemd-journal", text)
                continue
            self.assertIn("OnFailure=cybershorts-alert@fail:%N.service", text, svc.name)
            if "Type=oneshot" in text:
                self.assertIn("OnSuccess=cybershorts-alert@ok:%N.service", text, svc.name)
            else:
                self.assertNotIn("OnSuccess", text, svc.name)

    def test_backup_timer_exists(self):
        self.assertIn("Unit=cybershorts-backup.service", (REPO / "systemd" / "cybershorts-backup.timer").read_text())


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.app = Path(tempfile.mkdtemp())
        (self.app / ".env").write_text("TELEGRAM_BOT_TOKEN=x\n")
        (self.app / "config.yaml").write_text("channel: test\n")
        self.cfg = make_cfg(self.app)
        data = self.cfg.path()
        (data / "youtube_token.json").write_text('{"t": 1}')
        (data / "brand" / "avatar.png").write_bytes(b"png")
        (data / "videos" / "draft_1.mp4").write_bytes(b"video")
        conn = sqlite3.connect(data / "cybershorts.db")
        conn.execute("create table t (x)")
        conn.execute("insert into t values (7)")
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.app)

    def _members(self, tar_path):
        with tarfile.open(tar_path) as tar:
            return set(tar.getnames()), tar

    def test_plain_backup_contents_and_no_telegram(self):
        with mock.patch("cybershorts.telegram.Telegram") as tg:
            out = backup.run(self.cfg)
        tg.assert_not_called()  # no passphrase -> never leaves the box
        self.assertTrue(out.name.endswith(".tar.gz"))
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)
        with tarfile.open(out) as tar:
            names = set(tar.getnames())
            for want in ("cybershorts/.env", "cybershorts/config.yaml", "cybershorts/data/youtube_token.json",
                         "cybershorts/data/brand/avatar.png", "cybershorts/data/cybershorts.db"):
                self.assertIn(want, names)
            self.assertFalse(any("videos" in n for n in names))
            dbf = Path(tempfile.mkdtemp()) / "db"
            dbf.write_bytes(tar.extractfile("cybershorts/data/cybershorts.db").read())
        self.assertEqual(sqlite3.connect(dbf).execute("select x from t").fetchone()[0], 7)

    @unittest.skipUnless(shutil.which("openssl"), "openssl not installed")
    def test_encrypted_backup_decrypts_and_is_sent(self):
        cfg = make_cfg(self.app, BACKUP_PASSPHRASE="correct horse battery")
        with mock.patch("cybershorts.telegram.Telegram") as tg:
            out = backup.run(cfg)
        self.assertTrue(out.name.endswith(".tar.gz.enc"))
        self.assertNotIn(b"TELEGRAM_BOT_TOKEN", out.read_bytes())
        tg.return_value.send_document.assert_called_once()
        self.assertNotIn("correct horse", str(tg.mock_calls))
        dec = out.with_name("dec.tar.gz")
        subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", str(backup.ITER),
                        "-in", str(out), "-out", str(dec), "-pass", "pass:correct horse battery"], check=True)
        with tarfile.open(dec) as tar:
            self.assertIn("cybershorts/.env", tar.getnames())

    @unittest.skipUnless(shutil.which("openssl"), "openssl not installed")
    def test_no_telegram_flag(self):
        cfg = make_cfg(self.app, BACKUP_PASSPHRASE="pw123456")
        with mock.patch("cybershorts.telegram.Telegram") as tg:
            backup.run(cfg, telegram=False)
        tg.assert_not_called()

    def test_prune_keeps_newest(self):
        folder = self.cfg.path("backups")
        folder.mkdir(exist_ok=True)
        for i in range(1, 6):
            (folder / f"cybershorts-2026010{i}-000000.tar.gz").write_bytes(b"x")
        (folder / "unrelated.txt").write_text("keep me")
        backup.prune(folder, 2)
        left = sorted(p.name for p in folder.iterdir())
        self.assertEqual(left, ["cybershorts-20260104-000000.tar.gz", "cybershorts-20260105-000000.tar.gz",
                                "unrelated.txt"])

    def test_cleanup_never_touches_backups(self):
        from cybershorts import pipeline

        out = backup.run(self.cfg)
        pipeline.cleanup(self.cfg)
        self.assertTrue(out.exists())


if __name__ == "__main__":
    unittest.main()
