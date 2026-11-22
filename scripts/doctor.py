#!/usr/bin/env python3
"""Environment/config checks. Use --offline to skip network checks."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import logging
import shutil
import subprocess
import time

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

FAILS = 0


def report(ok: bool, name: str, detail: str = "") -> None:
    global FAILS
    if not ok:
        FAILS += 1
    print("%s %s%s" % ("✓" if ok else "✗", name, (" - " + detail) if detail else ""))


def run(cmd, timeout=20) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="skip network checks")
    args = ap.parse_args()

    try:
        from cybershorts.config import load

        cfg = load()
        report(True, "config loads", str(cfg.app_dir))
    except Exception as e:  # noqa: BLE001
        report(False, "config loads", str(e))
        return 1

    for attr in ("openrouter_api_key", "pixabay_api_key", "telegram_bot_token", "telegram_chat_id",
                 "tiktok_client_key", "tiktok_client_secret", "tiktok_redirect_uri"):
        report(bool(getattr(cfg, attr, "")), "env " + attr.upper())
    print("  (HEALTHCHECK_URL is optional: %s)" % ("set" if cfg.healthcheck_url else "not set"))
    if cfg.healthcheck_url and not args.offline:
        from cybershorts import health
        ok, why = health.check(cfg.healthcheck_url, "up", "doctor test",
                               verify=bool(cfg.get("health.verify_tls", True)))
        report(ok, "Uptime Kuma push", why)
    print("  (BACKUP_PASSPHRASE is optional: %s)" % (
        "set, backups are encrypted and sent to Telegram" if cfg.backup_passphrase
        else "not set, backups stay in data/backups only"))
    if cfg.backup_passphrase:
        report(shutil.which("openssl") is not None, "openssl present (backup encryption)")

    for tool in ("ffmpeg", "ffprobe"):
        report(shutil.which(tool) is not None, tool + " present")
    if shutil.which("ffmpeg"):
        try:
            out = run(["ffmpeg", "-hide_banner", "-filters"])
            report(" ass " in out, "ffmpeg has 'ass' filter")
        except Exception as e:  # noqa: BLE001
            report(False, "ffmpeg has 'ass' filter", str(e))
    font = cfg.get("captions.font", "DejaVu Sans")
    if shutil.which("fc-list"):
        try:
            out = run(["fc-list", ":family=" + font])
            report(bool(out.strip()), "font '%s'" % font)
        except Exception as e:  # noqa: BLE001
            report(False, "font '%s'" % font, str(e))
        try:
            out = run(["fc-list", ":family=DejaVu Sans Mono"])
            report(bool(out.strip()), "font 'DejaVu Sans Mono' (recap newsroom terminal)")
        except Exception as e:  # noqa: BLE001
            report(False, "font 'DejaVu Sans Mono'", str(e))
    else:
        report(False, "fc-list present (fontconfig)")

    if str(cfg.get("voice.engine", "kokoro")).lower() == "kokoro":
        from cybershorts import tts as _tts
        model, voices = _tts._kokoro_paths(cfg)
        report(model.exists() and voices.exists(), "kokoro voice " + str(cfg.get("voice.kokoro_voice", "am_michael")),
               "will be downloaded on first use (~120 MB)" if cfg.get("voice.mode") == "tts" else "")
    else:
        voice = cfg.get("voice.piper_voice", "en_US-ryan-high")
        report(Path(cfg.path("voices", voice + ".onnx")).exists(), "piper voice " + voice,
               "will be downloaded on first use" if cfg.get("voice.mode") == "tts" else "")

    yt_tok = cfg.path("youtube_token.json")
    report(yt_tok.exists(), "youtube token file", str(yt_tok))
    if yt_tok.exists():
        try:
            from cybershorts import youtube as _yt

            if _yt.granted_scopes(cfg):
                for scope, what in ((_yt.SCOPE_FORCE_SSL, "first comments and analytics"),
                                    (_yt.SCOPE_ANALYTICS, "analytics retention data")):
                    if not _yt.has_scope(cfg, scope):
                        print("! youtube token lacks %s: %s disabled; re-run scripts/auth_youtube.py"
                              % (scope.rsplit("/", 1)[-1], what))
            else:
                print("! youtube token records no scopes; re-run scripts/auth_youtube.py for comments/analytics")
        except Exception as e:  # noqa: BLE001
            print("! could not read youtube scopes: %s" % type(e).__name__)
    tt_tok = cfg.path("tiktok_token.json")
    report(tt_tok.exists(), "tiktok token file", str(tt_tok))
    if tt_tok.exists():
        try:
            import json

            t = json.loads(tt_tok.read_text())
            left = t.get("refresh_expires_at", 0) - time.time()
            report(left > 0, "tiktok refresh token valid", "%.0f days left" % (left / 86400))
        except Exception as e:  # noqa: BLE001
            report(False, "tiktok token readable", str(e))

    if cfg.get("sound.enabled", True) is False:
        print("  (Sound kit: off)")
    else:
        kit_names = [it["name"] for it in (cfg.get("sound.kit") or []) if isinstance(it, dict) and it.get("name")]
        folder = str(cfg.get("sound.folder", "brand/sound"))
        sound_dir = cfg.path(folder)
        have = sum(1 for n in kit_names
                   if any((sound_dir / (n + ext)).is_file() and (sound_dir / (n + ext)).stat().st_size > 0
                          for ext in (".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac")))
        print("  (Sound kit: %d/%d files in data/%s)" % (have, len(kit_names), folder))
    if args.offline:
        print("\nOffline mode: network checks skipped.")
        return 0 if FAILS == 0 else 1

    import requests

    if yt_tok.exists():
        try:
            from cybershorts import youtube

            youtube.get_service(cfg)
            report(True, "youtube token refreshes")
        except Exception as e:  # noqa: BLE001
            report(False, "youtube token refreshes", str(e)[:150])
    if tt_tok.exists() and cfg.tiktok_client_key:
        try:
            from cybershorts import tiktok

            tiktok.get_token(cfg)
            report(True, "tiktok token usable")
        except Exception as e:  # noqa: BLE001
            report(False, "tiktok token usable", str(e)[:150])
    if cfg.openrouter_api_key:
        try:
            r = requests.get("https://openrouter.ai/api/v1/key", timeout=15,
                             headers={"Authorization": "Bearer " + cfg.openrouter_api_key})
            report(r.ok, "OpenRouter key valid", "HTTP %d" % r.status_code)
        except requests.RequestException as e:
            report(False, "OpenRouter key valid", type(e).__name__)
    if cfg.telegram_bot_token:
        try:
            from cybershorts.telegram import Telegram

            me = Telegram(cfg.telegram_bot_token, cfg.telegram_chat_id).get_me()
            report(True, "Telegram getMe", "@%s" % me.get("username"))
        except Exception as e:  # noqa: BLE001
            report(False, "Telegram getMe", str(e)[:150])
    try:
        from cybershorts import visuals
        d = visuals.local_dir(cfg)
        n = len([f for f in d.iterdir() if f.suffix.lower() in visuals.VIDEO_EXTS]) if d.is_dir() else 0
        print("  b-roll provider: %s; local clips in %s: %d" % (cfg.get("broll.provider", "pixabay"), d, n))
    except Exception:  # noqa: BLE001
        pass
    if getattr(cfg, "elevenlabs_api_key", ""):
        try:
            r = requests.get("https://api.elevenlabs.io/v1/user/subscription", timeout=15,
                             headers={"xi-api-key": cfg.elevenlabs_api_key})
            if r.ok:
                j = r.json()
                used, limit = j.get("character_count", 0), j.get("character_limit", 0)
                report(True, "ElevenLabs key", f"{j.get('tier', '?')} plan, {used:,}/{limit:,} credits used this period")
            else:
                report(False, "ElevenLabs key", "HTTP %d" % r.status_code)
        except requests.RequestException as e:
            report(False, "ElevenLabs key", type(e).__name__)
    elif str(cfg.get("voice.engine", "kokoro")).lower() == "elevenlabs":
        report(False, "ElevenLabs key", "voice.engine is elevenlabs but ELEVENLABS_API_KEY is empty")
    if cfg.pixabay_api_key:
        try:
            r = requests.get("https://pixabay.com/api/videos/", timeout=15,
                             params={"key": cfg.pixabay_api_key, "q": "technology", "per_page": 3})
            report(r.ok, "Pixabay test query", "HTTP %d" % r.status_code)
        except requests.RequestException as e:
            report(False, "Pixabay test query", type(e).__name__)
    if cfg.pexels_api_key:
        try:
            r = requests.get("https://api.pexels.com/videos/search", timeout=15,
                             params={"query": "technology", "per_page": 1},
                             headers={"Authorization": cfg.pexels_api_key})
            report(r.ok, "Pexels test query", "HTTP %d" % r.status_code)
        except requests.RequestException as e:
            report(False, "Pexels test query", type(e).__name__)
    return 0 if FAILS == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
