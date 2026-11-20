"""Configuration: .env secrets + config.yaml merged over defaults."""
from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("cybershorts")

DEFAULTS: dict[str, Any] = {
    "channel": {"name": "Cyber Shorts", "handle": "@yourhandle", "sign_off": "Follow for daily cyber news."},
    "feeds": [
        {"name": "BleepingComputer", "url": "https://www.bleepingcomputer.com/feed/"},
        {"name": "The Hacker News", "url": "https://feeds.feedburner.com/TheHackersNews"},
        {"name": "Krebs on Security", "url": "https://krebsonsecurity.com/feed/"},
        {"name": "SecurityWeek", "url": "https://www.securityweek.com/feed/"},
        {"name": "Dark Reading", "url": "https://www.darkreading.com/rss.xml"},
        {"name": "CISA", "url": "https://www.cisa.gov/cybersecurity-advisories/all.xml"},
    ],
    "news": {"max_age_hours": 36, "max_candidates": 25, "fetch_full_text": True, "max_article_chars": 6000},
    "llm": {"model": "anthropic/claude-haiku-4.5", "temperature": 0.6, "target_words": [80, 110], "style_notes": ""},
    "voice": {
        "mode": "tts",
        "engine": "kokoro",
        "kokoro_voice": "am_michael",
        "speed": 1.05,
        "piper_voice": "en_US-ryan-high",
        "length_scale": 0.92,
        "sentence_pause": 0.22,
        "paragraph_pause": 0.45,
        "polish": True,
        "pronunciations": {"CISA": "Sisa", "SQL": "sequel", "SaaS": "sass", "macOS": "mac O S", "iOS": "eye O S", "2FA": "two factor", "MFA": "M F A", "NIST": "nist", "OAuth": "oh auth", "SSO": "S S O", "DDoS": "D DOS", "LLM": "L L M", "LLMs": "L L Ms", "APT": "A P T"},
    },
    "captions": {"whisper_model": "base.en", "words_per_chunk": 3, "font": "DejaVu Sans", "font_size": 92},
    "video": {"width": 1080, "height": 1920, "fps": 30, "crf": 23, "preset": "veryfast", "darken": 0.35},
    "broll": {
        "provider": "pixabay",
        "local_dir": "broll",
        "match_story": False,
        "queries": [
            "cyber security", "hacker code", "binary code", "matrix code", "data network",
            "digital security", "computer code", "circuit board", "server data center",
            "encryption", "programming code", "cyber technology",
        ],
        "require_tags": [
            "cyber", "hacker", "hacking", "security", "code", "coding", "binary", "matrix",
            "data", "digital", "network", "computer", "technology", "circuit", "server",
            "programming", "encryption", "internet", "software", "virus", "malware",
        ],
        "exclude_tags": [
            "nature", "flower", "animal", "food", "beach", "ocean", "sea", "forest", "landscape",
            "mountain", "sky", "sunset", "woman", "girl", "baby", "child", "family", "wedding",
            "christmas", "dog", "cat", "bird", "money", "coin", "bitcoin", "crypto",
        ],
    },
    "youtube": {
        "enabled": True,
        "privacy": "public",
        "category_id": "28",
        "contains_synthetic_media": False,
        "made_for_kids": False,
    },
    "tiktok": {"enabled": True},
    "cleanup": {"keep_days": 7},
}

SECRET_KEYS = [
    "OPENROUTER_API_KEY",
    "PIXABAY_API_KEY",
    "PEXELS_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "TIKTOK_CLIENT_KEY",
    "TIKTOK_CLIENT_SECRET",
    "TIKTOK_REDIRECT_URI",
    "HEALTHCHECK_URL",
]

DATA_SUBDIRS = ("videos", "voices", "models", "work", "broll")


def deep_merge(base: dict, override: dict) -> dict:
    """Return base merged with override (dicts merged recursively, other values replaced)."""
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Config:
    def __init__(self, app_dir: Path, raw: dict[str, Any], env: dict[str, str]):
        self.app_dir = Path(app_dir)
        self.raw = raw
        for key in SECRET_KEYS:
            setattr(self, key.lower(), env.get(key, "") or "")

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.raw
        for part in dotted.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return default
        return cur

    def path(self, *parts: str) -> Path:
        """Path under APP_DIR/data. Creates the standard data subdirs."""
        data = self.app_dir / "data"
        data.mkdir(parents=True, exist_ok=True)
        for sub in DATA_SUBDIRS:
            (data / sub).mkdir(exist_ok=True)
        p = data.joinpath(*parts) if parts else data
        return p


def _app_dir() -> Path:
    env = os.environ.get("APP_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def load() -> Config:
    from dotenv import load_dotenv

    app_dir = _app_dir()
    load_dotenv(app_dir / ".env")
    raw = copy.deepcopy(DEFAULTS)
    cfg_file = app_dir / "config.yaml"
    if cfg_file.exists():
        import yaml

        try:
            loaded = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            raise RuntimeError(f"Invalid config.yaml: {e}") from e
        if not isinstance(loaded, dict):
            raise RuntimeError("config.yaml must contain a mapping at top level")
        raw = deep_merge(raw, loaded)
    else:
        log.warning("config.yaml not found in %s; using defaults", app_dir)
    cfg = Config(app_dir, raw, dict(os.environ))
    cfg.path()  # ensure data dirs exist
    return cfg
