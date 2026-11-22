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
    "llm": {"model": "anthropic/claude-haiku-4.5", "temperature": 0.6, "target_words": [65, 95], "style_notes": "", "provider": {"sort": "price"}, "fact_check": True, "fact_check_model": "", "hooks": 3, "hook_model": ""},
    "voice": {
        "mode": "tts",
        "engine": "kokoro",
        "kokoro_voice": "am_michael",
        "speed": 1.0,
        "piper_voice": "en_US-ryan-high",
        "length_scale": 0.92,
        "fallback_engine": "kokoro",
        "elevenlabs": {
            "voice_id": "JBFqnCBsd6RMkjVDRZzb",
            "model": "eleven_multilingual_v2",
            "stability": 0.45,
            "similarity_boost": 0.8,
            "style": 0.15,
            "speaker_boost": True,
            "speed": 1.0,
            "tag_models": ["eleven_v3"],
            "audio_tags": True,
            "allowed_tags": ["curious", "excited", "whispers", "sighs", "serious", "laughs"],
            "max_tags": 3,
        },
        "chunking": "paragraph",
        "room_tone_db": -62,
        "sentence_pause": 0.22,
        "paragraph_pause": 0.45,
        "polish": True,
        "pronunciations": {"CISA": "Sisa", "SQL": "sequel", "SaaS": "sass", "macOS": "mac O S", "iOS": "eye O S", "2FA": "two factor", "MFA": "M F A", "NIST": "nist", "OAuth": "oh auth", "SSO": "S S O", "DDoS": "D DOS", "LLM": "L L M", "LLMs": "L L Ms", "APT": "A P T"},
    },
    "captions": {"whisper_model": "base.en", "words_per_chunk": 3, "font": "DejaVu Sans", "font_size": 92, "style": "karaoke"},
    "video": {"width": 1080, "height": 1920, "fps": 30, "crf": 23, "preset": "veryfast", "darken": 0.35, "hook_seconds": 1.6},
    "branding": {
        "accent": "#3DDCFF",
        "keyword_color": "#FFD60A",
        "watermark": "brand/logo.png",
        "watermark_size": 150,
        "watermark_opacity": 0.85,
        "progress_bar": True,
        "end_card": True,
    },
    "broll": {
        "provider": "pixabay",
        "local_dir": "broll",
        "max_clips": 5,
        "ken_burns": True,
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
        "first_comment": True,
    },
    "tiktok": {"enabled": True},
    "music": {"enabled": True, "folder": "music", "volume_db": -20, "duck": True, "on_tiktok": False, "prefer_kit": True},
    "sound": {
        "enabled": True,
        "folder": "brand/sound",
        "sfx_on_tiktok": True,
        "sfx_model": "eleven_text_to_sound_v3",
        "music_model": "music_v2",
        "output_format": "mp3_44100_192",
        "prompt_influence": 0.5,
        "whoosh_lead": 0.12,
        "min_gap": 0.3,
        "limit": 0.95,
        "gain_db": {"hook": -10, "whoosh": -16, "breaking": -12, "card": -18, "outro": -10},
        "recap": {"theme_db": -12, "bed_db": -22, "duck": True},
        "credits": {"sfx_per_second": 40, "music_per_minute": 900},
        "kit": [
            {"name": "hook", "kind": "sfx", "prompt": "short digital glitch impact, punchy opening hit for a cybersecurity news video", "seconds": 1.0, "loop": False},
            {"name": "whoosh", "kind": "sfx", "prompt": "fast airy transition whoosh, clean, digital", "seconds": 0.6, "loop": False},
            {"name": "breaking", "kind": "sfx", "prompt": "urgent alert tone, two-note digital warning, breaking news", "seconds": 1.5, "loop": False},
            {"name": "card", "kind": "sfx", "prompt": "UI data blip, soft digital notification tick", "seconds": 0.5, "loop": False},
            {"name": "outro", "kind": "sfx", "prompt": "signature sting, short dark synth resolve with a soft digital tail", "seconds": 2.0, "loop": False},
            {"name": "theme", "kind": "music", "prompt": "dark synth cyber signature intro, short, memorable, instrumental, no vocals", "seconds": 8, "loop": False},
            {"name": "bed1", "kind": "music", "prompt": "instrumental, dark minimal techno bed to sit under narration, steady pulse, sparse, no vocals, no lead melody, no drops, low dynamics", "seconds": 75, "loop": True},
            {"name": "bed2", "kind": "music", "prompt": "instrumental, subtle synthwave underscore for tech news, warm pads, light arpeggio, steady, no vocals, no drops, low dynamics", "seconds": 90, "loop": True},
            {"name": "bed3", "kind": "music", "prompt": "instrumental, tense low-key electronic underscore, cybersecurity documentary feel, soft ticking percussion, no vocals, no drops, low dynamics", "seconds": 60, "loop": True},
        ],
    },
    "cleanup": {"keep_days": 7},
    "alerts": {"enabled": True, "repeat_minutes": 180, "log_lines": 25},
    "backup": {"keep": 8, "telegram": True, "max_telegram_mb": 45},
    "series": {
        "enabled": True,
        "items": [
            {
                "name": "Patch This Now", "hashtag": "PatchThisNow", "badge": "PATCH THIS NOW",
                "per_week": None, "match": "patch, update, fixed, actively exploited, KEV",
                "guidance": "Name the product, the fix and any deadline.",
            },
            {
                "name": "Scam of the Week", "hashtag": "ScamOfTheWeek", "badge": "SCAM OF THE WEEK",
                "per_week": 1, "match": "scam, phishing, fraud, smishing, impersonation",
                "guidance": "How the scam works + the one tell.",
            },
            {
                "name": "CVE in 30s", "hashtag": "CVEin30s", "badge": "CVE IN 30S",
                "per_week": None, "match": "a single named CVE",
                "guidance": "What, who, how bad, what to do.",
            },
        ],
    },
    "data_card": {"enabled": True, "nvd": True, "seconds": 4.5},
    "breaking": {
        "enabled": True,
        "kev": True,
        "rss_urgency": True,
        "urgency_threshold": 8,
        "rss_max_age_minutes": 120,
        "max_score_items": 15,
        "urgency_model": "",
        "daily_cap": 2,
        "quiet_hours": ["22:00", "07:00"],
        "timezone": "America/New_York",
        "kev_max_age_days": 2,
        "badge": "BREAKING",
        "badge_color": "#FF3B30",
        "title_prefix": "\U0001F6A8 ",
    },
    "analytics": {
        "enabled": True,
        "lookback_days": 90,
        "min_age_hours": 48,
        "min_videos": 6,
        "top_n": 5,
        "learnings_max_chars": 800,
        "use_learnings": True,
        "report": True,
        "model": "",
    },
    "recap": {
        "enabled": True,
        "stories": 5,
        "min_stories": 3,
        "lookback_days": 7,
        "timezone": "America/New_York",
        "model": "",
        "target_words": [600, 800],
        "fetch_article": True,
        "max_article_chars": 2500,
        "max_audio_tags": 5,
        "title_prefix": "This Week in Cyber: ",
        "subscribe_line": "Subscribe for a new recap every Sunday.",
        "category_id": "28",
        "width": 1920,
        "height": 1080,
        "fps": 30,
        "crf": 22,
        "preset": "veryfast",
        "render_timeout": 3600,
        "broll_per_part": 2,
        "max_clips": 12,
        "caption_font_size": 52,
        "words_per_chunk": 6,
        "title_card_seconds": 3.5,
        "lock_timeout": 1800,
        "preview": {"width": 854, "height": 480, "max_mb": 45, "audio_kbps": 64, "max_kbps": 1500},
    },
}

SECRET_KEYS = [
    "OPENROUTER_API_KEY",
    "PIXABAY_API_KEY",
    "PEXELS_API_KEY",
    "ELEVENLABS_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "TIKTOK_CLIENT_KEY",
    "TIKTOK_CLIENT_SECRET",
    "TIKTOK_REDIRECT_URI",
    "HEALTHCHECK_URL",
    "NVD_API_KEY",
    "BACKUP_PASSPHRASE",
]

DATA_SUBDIRS = ("videos", "voices", "models", "work", "broll", "brand", "music")


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
