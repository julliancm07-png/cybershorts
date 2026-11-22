"""ElevenLabs v3 audio tags ([sighs], [whispers], ...): parse, sanitize, strip.

`data["script"]` is always tag-free (captions, fact-check, TikTok); only the voice
script may carry tags, and only for engines/models that understand them.
"""
from __future__ import annotations

import re

TAG_RE = r"\[([A-Za-z][A-Za-z \-]{1,24})\]"
_TAG = re.compile(TAG_RE)

DEFAULT_TAG_MODELS = ["eleven_v3"]
DEFAULT_ALLOWED = ["curious", "excited", "whispers", "sighs", "serious", "laughs"]
DEFAULT_MAX_TAGS = 3


def _norm_lines(lines: list[str]) -> str:
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip("\n")


def strip_tags(text: str) -> str:
    """Remove every [tag]. Keeps newlines, collapses spaces, drops paragraphs left empty."""
    if not text:
        return text or ""
    out: list[str] = []
    dropped = False
    for line in text.split("\n"):
        had = bool(line.strip())
        # no space left before closing punctuation: "Hello [sighs]." -> "Hello."
        s = re.sub(r"[ \t]*" + TAG_RE + r"(?=[ \t]*[.,!?;:])", "", line)
        s = _TAG.sub(" ", s)
        s = re.sub(r"[ \t]{2,}", " ", s).strip()
        if had and not s:
            dropped = True
            continue
        out.append(s)
    res = "\n".join(out)
    return _norm_lines(out) if dropped else res


def merge_tag_only(text: str) -> str:
    """A paragraph that holds only tags joins the next paragraph (dropped if none follows)."""
    out: list[str] = []
    pending = ""
    for line in (text or "").split("\n"):
        s = line.strip()
        if s and not strip_tags(s).strip():
            pending = (pending + " " + s).strip()
            continue
        if s and pending:
            line = pending + " " + s
            pending = ""
        out.append(line)
    return _norm_lines(out) if out else ""


def sanitize_tags(text: str, allowed=None, max_tags: int | None = None) -> str:
    """Lowercase tags, drop unknown ones, keep the first `max_tags`; merge tag-only paragraphs
    into the next one, so strip_tags(result) has the same paragraphs as the clean script."""
    allow = {str(a).strip().lower() for a in (DEFAULT_ALLOWED if allowed is None else allowed)}
    budget = DEFAULT_MAX_TAGS if max_tags is None else max(0, int(max_tags))
    kept = 0

    def sub(m: re.Match) -> str:
        nonlocal kept
        name = re.sub(r"\s+", " ", m.group(1).strip().lower())
        if name in allow and kept < budget:
            kept += 1
            return f"[{name}]"
        return ""

    lines = []
    for ln in (text or "").split("\n"):
        new = _TAG.sub(sub, ln)
        if new != ln:
            new = re.sub(r"[ \t]+([.,!?;:])", r"\1", new) if new.count("[") < ln.count("[") else new
        lines.append(re.sub(r"[ \t]{2,}", " ", new).strip())
    return merge_tag_only("\n".join(lines))


def _cfg_models(cfg) -> set[str]:
    models = cfg.get("voice.elevenlabs.tag_models", DEFAULT_TAG_MODELS)
    if models is None:
        models = DEFAULT_TAG_MODELS
    if isinstance(models, str):
        models = [models]
    return {str(m).strip().lower() for m in models}


def is_tag_model(cfg, model: str | None = None) -> bool:
    model = model if model is not None else cfg.get("voice.elevenlabs.model", "eleven_multilingual_v2")
    return str(model or "").strip().lower() in _cfg_models(cfg)


def tags_supported(cfg, engine: str | None = None, model: str | None = None) -> bool:
    """True only for engine elevenlabs AND audio_tags enabled AND a model in tag_models."""
    engine = str(engine if engine is not None else cfg.get("voice.engine", "kokoro") or "").lower()
    if engine != "elevenlabs" or not cfg.get("voice.elevenlabs.audio_tags", True):
        return False
    return is_tag_model(cfg, model)


def split_paragraphs(text: str) -> list[str]:
    """Paragraphs: blank-line or single-newline separated (the rule tts.split_beats uses)."""
    return [p.strip() for p in re.split(r"\n\s*\n|\n", text or "") if p.strip()]


def normalize_paragraphs(text: str) -> str:
    """Re-join paragraphs with blank lines so "\\n\\n" splits agree with TTS/caption beats."""
    return "\n\n".join(split_paragraphs(text))
