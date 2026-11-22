"""Weekly backup of the files that are hard to recreate: secrets, logins, settings and the database.

Videos, b-roll, music, voice models and work files are not included (they are re-downloaded or
re-rendered). With BACKUP_PASSPHRASE set, the archive is encrypted with openssl (AES-256, PBKDF2)
and also sent to your Telegram chat as an off-site copy. Without it, only a local copy is kept.

Restore:
  openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 -in FILE.tar.gz.enc -out restore.tar.gz
  tar xzf restore.tar.gz      # -> cybershorts/ with .env, config.yaml, data/...
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

from .config import Config

log = logging.getLogger(__name__)

APP_FILES = (".env", "config.yaml", "client_secret.json")
DATA_FILES = ("youtube_token.json", "tiktok_token.json", "learnings.md")
DATA_DIRS = ("brand",)
MAX_FILE_BYTES = 20 * 1024 * 1024  # skip anything huge that wandered into brand/
ITER = 600000
PREFIX = "cybershorts-"


def _snapshot_db(src: Path, dst: Path) -> None:
    """Consistent copy of a live SQLite db (safe while the bot is writing)."""
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def collect(cfg: Config, staging: Path) -> list[str]:
    """Copy everything to back up into staging/cybershorts/. Returns archive-relative names."""
    root = staging / "cybershorts"
    (root / "data").mkdir(parents=True)
    names = []

    def _copy(src: Path, rel: str) -> None:
        if not src.is_file() or src.is_symlink():
            return
        if src.stat().st_size > MAX_FILE_BYTES:
            log.warning("Skipping %s (larger than %d MB)", rel, MAX_FILE_BYTES // 2**20)
            return
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        names.append(rel)

    for f in APP_FILES:
        _copy(cfg.app_dir / f, f)
    data = cfg.path()
    for f in DATA_FILES:
        _copy(data / f, f"data/{f}")
    for d in DATA_DIRS:
        base = data / d
        if base.is_dir():
            for p in sorted(base.rglob("*")):
                _copy(p, f"data/{p.relative_to(data).as_posix()}")
    db_file = data / "cybershorts.db"
    if db_file.exists():
        _snapshot_db(db_file, root / "data" / "cybershorts.db")
        names.append("data/cybershorts.db")
    return names


def _encrypt(plain: Path, out: Path, passphrase: str) -> None:
    if not shutil.which("openssl"):
        raise RuntimeError("openssl is not installed (apt install openssl)")
    env = dict(os.environ, CS_BACKUP_PASS=passphrase)  # via env, never argv (visible in ps)
    r = subprocess.run(
        ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", str(ITER), "-salt",
         "-in", str(plain), "-out", str(out), "-pass", "env:CS_BACKUP_PASS"],
        capture_output=True, text=True, env=env, timeout=300)
    if r.returncode != 0:
        out.unlink(missing_ok=True)
        raise RuntimeError(f"openssl failed: {r.stderr.strip()[:200]}")


def prune(folder: Path, keep: int) -> list[Path]:
    files = sorted((p for p in folder.glob(PREFIX + "*.tar.gz*") if p.is_file()),
                   key=lambda p: p.name, reverse=True)
    gone = []
    for p in files[max(keep, 1):]:
        try:
            p.unlink()
            gone.append(p)
        except OSError as e:
            log.warning("Could not delete old backup %s: %s", p, e)
    return gone


def run(cfg: Config, telegram: bool = True) -> Path:
    """Make one backup. Returns its path. Raises on failure (so systemd marks the job failed)."""
    out_dir = cfg.path("backups")
    out_dir.mkdir(exist_ok=True)
    os.chmod(out_dir, 0o700)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    passphrase = getattr(cfg, "backup_passphrase", "") or ""
    old_umask = os.umask(0o077)
    try:
        with tempfile.TemporaryDirectory(prefix="csbackup-") as tmp:
            tmp = Path(tmp)
            names = collect(cfg, tmp)
            if not names:
                raise RuntimeError("nothing to back up (is APP_DIR right?)")
            plain = tmp / f"{PREFIX}{stamp}.tar.gz"
            with tarfile.open(plain, "w:gz") as tar:
                tar.add(tmp / "cybershorts", arcname="cybershorts")
            if passphrase:
                final = out_dir / (plain.name + ".enc")
                _encrypt(plain, final, passphrase)
            else:
                final = out_dir / plain.name
                shutil.copy2(plain, final)
        os.chmod(final, 0o600)
    finally:
        os.umask(old_umask)

    removed = prune(out_dir, int(cfg.get("backup.keep", 8)))
    size_kb = final.stat().st_size / 1024
    log.info("Backup %s (%.0f KB, %d files); pruned %d old", final.name, size_kb, len(names), len(removed))

    if telegram and cfg.get("backup.telegram", True):
        if not passphrase:
            log.warning("BACKUP_PASSPHRASE not set: backup kept locally only, not sent to Telegram")
        elif not (cfg.telegram_bot_token and cfg.telegram_chat_id):
            log.warning("Telegram not configured: backup kept locally only")
        elif final.stat().st_size > int(cfg.get("backup.max_telegram_mb", 45)) * 2**20:
            log.warning("Backup is larger than backup.max_telegram_mb: kept locally only")
        else:
            from .telegram import Telegram

            Telegram(cfg.telegram_bot_token, cfg.telegram_chat_id).send_document(
                final, f"\U0001f5c4 Weekly backup ({size_kb:.0f} KB, {len(names)} files). Encrypted with your "
                       "BACKUP_PASSPHRASE. Restore steps are in the README.")
    return final
