"""OpenRouter LLM: story picking and script writing."""
from __future__ import annotations

import json
import logging
import re
import time

import requests

from .config import Config

log = logging.getLogger("cybershorts")

API_URL = "https://openrouter.ai/api/v1/chat/completions"


def chat(cfg: Config, messages: list[dict], temperature: float | None = None,
         model: str | None = None) -> str:
    if not cfg.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    payload = {
        "model": model or cfg.get("llm.model", "anthropic/claude-haiku-4.5"),
        "temperature": cfg.get("llm.temperature", 0.5) if temperature is None else temperature,
        "messages": messages,
    }
    routing = cfg.get("llm.provider") or {}
    if routing:
        # OpenRouter provider routing, e.g. {"sort": "price"} = always use the cheapest host
        payload["provider"] = dict(routing)
    headers = {
        "Authorization": f"Bearer {cfg.openrouter_api_key}",
        "X-Title": "cybershorts",
        "Content-Type": "application/json",
    }
    last_err = "unknown error"
    for attempt in range(3):
        try:
            r = requests.post(API_URL, headers=headers, json=payload, timeout=90)
        except requests.RequestException as e:
            last_err = f"{type(e).__name__}: {e}"
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"OpenRouter request failed: {last_err}") from e
        if r.status_code == 429 or r.status_code >= 500:
            last_err = f"HTTP {r.status_code}"
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"OpenRouter error after retries: {last_err} {r.text[:200]}")
        if not r.ok:
            raise RuntimeError(f"OpenRouter error: HTTP {r.status_code} {r.text[:300]}")
        try:
            body = r.json()
            usage = body.get("usage") or {}
            log.info("OpenRouter: %s via %s, %s in / %s out tokens%s",
                     body.get("model", payload["model"]), body.get("provider", "?"),
                     usage.get("prompt_tokens", "?"), usage.get("completion_tokens", "?"),
                     f", ${usage['cost']:.5f}" if isinstance(usage.get("cost"), (int, float)) else "")
            return body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, ValueError) as e:
            raise RuntimeError(f"Unexpected OpenRouter response: {r.text[:300]}") from e
    raise RuntimeError(f"OpenRouter error: {last_err}")


def extract_json(text: str) -> dict:
    """Tolerantly extract the first JSON object from LLM output."""
    t = (text or "").strip()
    t = re.sub(r"```(?:json|JSON)?", "", t)
    start = t.find("{")
    if start < 0:
        raise ValueError("No JSON object found in text")
    decoder = json.JSONDecoder()
    # try each '{' until one decodes
    pos = start
    while pos >= 0:
        try:
            obj, _ = decoder.raw_decode(t[pos:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        pos = t.find("{", pos + 1)
    raise ValueError("No valid JSON object found in text")


def pick_story(cfg: Config, candidates: list[dict], recent_titles: list[str]) -> int:
    if len(candidates) <= 1:
        return 0
    lines = [f"{i}. [{c.get('source','')}] {c['title']}" for i, c in enumerate(candidates)]
    recent = "\n".join(f"- {t}" for t in recent_titles) or "(none)"
    prompt = (
        "You are the editor of a short-form IT and cybersecurity news channel for IT professionals.\n"
        "Pick the ONE story that is most significant and actionable for IT pros. Avoid stories "
        "that repeat the topic of recently covered ones.\n\n"
        f"Recently covered:\n{recent}\n\nCandidates:\n" + "\n".join(lines) +
        '\n\nRespond with JSON only: {"index": <number>}'
    )
    try:
        data = extract_json(chat(cfg, [{"role": "user", "content": prompt}], temperature=0.2))
        idx = int(data.get("index", 0))
        if 0 <= idx < len(candidates):
            return idx
        log.warning("pick_story index %s out of range; using 0", idx)
    except Exception as e:
        log.warning("pick_story failed (%s); using 0", e)
    return 0


def _truncate(s: str, n: int) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[: n - 1].rstrip() + "…"


CVE_RE = re.compile(r"\bCVE-\d{4}-\d{4,}\b", re.I)
_URL_RE = re.compile(r"https?://|www\.|\b[\w-]+\.(?:com|org|net|io|gov|co|me|ly)\b", re.I)
MAX_HOOK_WORDS = 20
MAX_COMMENT_CHARS = 150


def _tags_mod():
    from . import tags

    return tags


def _story_has_cve(story: dict) -> bool:
    text = " ".join(str(story.get(k) or "") for k in ("title", "summary", "article"))
    return bool(CVE_RE.search(text))


def _plain(text) -> str:
    """Single-line, tag-free text."""
    return " ".join(_tags_mod().strip_tags(str(text or "")).split())


def _clean_alt_hooks(raw) -> list[str]:
    """Alternative hooks: one paragraph each, <= 20 words, no tags."""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for h in raw:
        if isinstance(h, dict):
            h = h.get("hook") or h.get("text") or ""
        text = str(h or "").strip()
        if not text or re.search(r"\n\s*\n", text):
            continue
        text = _plain(text)
        if text and len(text.split()) <= MAX_HOOK_WORDS:
            out.append(text)
    return out


def _clean_first_comment(raw) -> str:
    text = _plain(raw)
    if not text or len(text) > MAX_COMMENT_CHARS or not text.endswith("?") or _URL_RE.search(text):
        return ""
    return text


def _clean_data_card(raw) -> dict | None:
    """Pass the LLM's raw card through as a plain dict; pipeline confirms it against the sources."""
    if not isinstance(raw, dict) or not raw:
        return None
    return dict(raw)


def series_name_for(cfg: Config, name, options) -> dict | None:
    """The offered series option matching `name` (case-insensitive), else None."""
    want = str(name or "").strip().lower()
    if not want:
        return None
    for opt in options or []:
        if str(opt.get("name", "")).strip().lower() == want:
            return opt
    return None


def clean_broll_queries(raw) -> list[str]:
    """Per-paragraph stock-footage queries: 2-4 plain words each, at most 8."""
    if isinstance(raw, str):
        raw = re.split(r"[,;\n]+", raw)
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for q in raw:
        words = re.sub(r"[^A-Za-z0-9 \-]", " ", str(q)).split()[:4]
        q = " ".join(words).lower()
        if len(q) >= 3:
            out.append(q[:40])
    return out[:8]


def validate_script(data: dict, story: dict, cfg: Config, series_options: list[dict] | None = None) -> dict:
    """Validate + coerce LLM script JSON. Raises ValueError if essential fields are missing."""
    for key in ("on_screen_title", "script"):
        if not str(data.get(key, "")).strip():
            raise ValueError(f"Script JSON missing '{key}'")
    sign_off = cfg.get("channel.sign_off", "")
    script = _tags_mod().normalize_paragraphs(str(data["script"]))
    if sign_off:
        if sign_off.lower().rstrip(".!") not in script.lower():
            script = f"{script}\n\n{sign_off}"
        elif script.lower().endswith(sign_off.lower()):
            head = script[: len(script) - len(sign_off)]
            if head.strip() and not head.endswith("\n\n"):
                script = head.rstrip() + "\n\n" + sign_off
    tags_raw = data.get("hashtags") or []
    if isinstance(tags_raw, str):
        tags_raw = re.split(r"[\s,]+", tags_raw)
    tags: list[str] = []
    for t in tags_raw:
        t = re.sub(r"[^A-Za-z0-9_]", "", str(t).lstrip("#"))
        if t and t.lower() not in [x.lower() for x in tags]:
            tags.append(t)
    tags = tags[:6]
    for fallback in ("cybersecurity", "infosec", "itnews"):
        if len(tags) >= 3:
            break
        if fallback not in [x.lower() for x in tags]:
            tags.append(fallback)
    series_opt = series_name_for(cfg, data.get("series"), series_options)
    if series_opt:
        stag = re.sub(r"[^A-Za-z0-9_]", "", str(series_opt.get("hashtag") or "").lstrip("#"))
        if stag:
            tags = [stag] + [t for t in tags if t.lower() != stag.lower()]
            tags = tags[:6]
    yt_title = _plain(data.get("youtube_title") or data["on_screen_title"])
    yt_title = re.sub(r"\s*#Shorts\s*$", "", yt_title, flags=re.I)
    yt_title = _truncate(yt_title, 95 - len(" #Shorts")) + " #Shorts"
    desc = _tags_mod().strip_tags(str(data.get("description") or "")).strip()
    url = story["url"]
    if url not in desc:
        desc = f"{desc}\nSource: {url}".strip()
    hashtag_line = " ".join(f"#{t}" for t in tags)
    if hashtag_line not in desc:
        desc = f"{desc}\n{hashtag_line}"
    query = str(data.get("pexels_query") or "").strip() or "technology"
    query = " ".join(query.split()[:4])
    kw_raw = data.get("keywords") or []
    if isinstance(kw_raw, str):
        kw_raw = re.split(r"[,;\n]+", kw_raw)
    if not isinstance(kw_raw, (list, tuple)):
        kw_raw = []
    keywords: list[str] = []
    for k in kw_raw:
        k = " ".join(str(k).split())
        if k and len(k) <= 24 and k.lower() not in [x.lower() for x in keywords]:
            keywords.append(k)
    out = {
        "on_screen_title": _truncate(_plain(data["on_screen_title"]), 42),
        "script": script,
        "youtube_title": yt_title,
        "description": desc,
        "hashtags": tags,
        "pexels_query": query,
        "keywords": keywords[:6],
        "broll_queries": clean_broll_queries(data.get("broll_queries")),
    }
    # tags live only in voice_script (TTS); script is always tag-free
    tagmod = _tags_mod()
    if tagmod.tags_supported(cfg):
        allowed = cfg.get("voice.elevenlabs.allowed_tags", None)
        max_tags = cfg.get("voice.elevenlabs.max_tags", None)
        voice = tagmod.sanitize_tags(script, allowed, max_tags)
    else:
        voice = tagmod.strip_tags(script)
    voice = tagmod.normalize_paragraphs(voice)
    out["script"] = tagmod.normalize_paragraphs(tagmod.strip_tags(voice))
    if voice != out["script"]:
        out["voice_script"] = voice
    n_hooks = int(cfg.get("llm.hooks", 3) or 1)
    if n_hooks > 1:
        first = (tagmod.split_paragraphs(out["script"]) or [""])[0]
        hooks = [first] if first else []
        for h in _clean_alt_hooks(data.get("alt_hooks")):
            if h.lower() not in [x.lower() for x in hooks]:
                hooks.append(h)
        hooks = hooks[:n_hooks]
        if len(hooks) > 1:
            out["hooks"] = hooks
    if series_opt:
        out["series"] = str(series_opt["name"])
    if cfg.get("youtube.first_comment", True):
        comment = _clean_first_comment(data.get("first_comment"))
        if comment:
            out["first_comment"] = comment
    if cfg.get("data_card.enabled", True) and _story_has_cve(story):
        card = _clean_data_card(data.get("data_card"))
        if card:
            out["data_card"] = card
    return out


def _learnings(cfg: Config) -> str:
    """Style guidance distilled from past performance (analytics); empty when absent/disabled."""
    if not cfg.get("analytics.use_learnings", True):
        return ""
    from . import analytics  # late import: analytics imports writer

    return analytics.read_learnings(cfg)


def write_script(cfg: Config, story: dict, *, series_options: list[dict] | None = None,
                 breaking: bool = False) -> dict:
    lo, hi = cfg.get("llm.target_words", [65, 95])
    sign_off = cfg.get("channel.sign_off", "")
    style_notes = str(cfg.get("llm.style_notes", "") or "").strip()
    use_tags = _tags_mod().tags_supported(cfg)
    n_hooks = int(cfg.get("llm.hooks", 3) or 1)
    want_comment = bool(cfg.get("youtube.first_comment", True))
    want_card = bool(cfg.get("data_card.enabled", True)) and _story_has_cve(story)
    options = list(series_options or [])
    learnings = _learnings(cfg)
    system = (
        "You write the narration for 30-45 second vertical news videos about IT and security, "
        "read aloud by a text-to-speech voice. Write for the ear, the way a sharp friend who works "
        "in IT would explain the story over coffee: conversational, confident, never stiff.\n"
        "Facts: use ONLY what is in the provided title, summary and article text. Never invent "
        "details, numbers, names, CVE ids, dates or attributions. If something is unknown, leave it out. "
        "Write in your own words; never copy sentences from the article."
    )
    user = (
        f"Story title: {story['title']}\nSource: {story.get('source','')}\n"
        f"URL: {story['url']}\nSummary: {story.get('summary','')}\n\n"
        + (f"Article text (for facts and specifics; pick the 2-3 that matter most to viewers):\n"
           f"{story['article']}\n\n" if story.get("article") else "")
        +
        "How the narration must sound:\n"
        "- Open with a hook a viewer can't scroll past: the stakes in plain words, not the headline.\n"
        "- Short sentences, mostly under 15 words. One idea per sentence. Use contractions. "
        "Vary the rhythm: mix a few very short sentences with medium ones.\n"
        "- Write it in 3 to 5 beats, one per paragraph, separated by a blank line: hook, "
        "what happened, why it matters, what to do, sign-off. The voice pauses between beats.\n"
        "- Use commas and em dashes where a speaker would breathe. No ellipses.\n"
        "- Then: what happened, who's affected, and what to do about it, as a natural flow, "
        "not a list. Use spoken transitions like 'Here's the thing', 'So what do you do?'.\n"
        "- No headline-speak, no stacked nouns ('critical unauthenticated remote code execution flaw'), "
        "no filler ('In today's news', 'It's worth noting').\n"
        "- Written to be spoken by a robot voice: no parentheses, slashes, symbols or URLs. "
        "Write numbers the way people say them. Spell out uncommon acronyms "
        "('remote code execution', not 'RCE'); common ones like VPN or AI are fine. "
        "Don't read CVE ids or version strings aloud; put them in the description instead.\n"
        f"- {lo}-{hi} words, ending with exactly: \"{sign_off}\"\n"
        + (f"- Channel style notes: {style_notes}\n" if style_notes else "")
        + ("- This is breaking news that just happened: open with what is happening right now and "
           "why it can't wait.\n" if breaking else "")
        + (_tag_instructions(cfg) if use_tags else "")
        + (f"\nStyle guidance learned from which past videos performed best (a style hint only, "
           f"never a source of facts):\n{learnings}\n" if learnings else "")
        + (_series_instructions(options) if options else "")
        + "\nReturn JSON only with these keys:\n"
        '- "on_screen_title": punchy headline for the top of the video, max 42 characters\n'
        '- "script": the narration\n'
        '- "youtube_title": max 95 characters, ending with " #Shorts"\n'
        '- "description": 2-3 sentences (include any CVE ids or versions here), then "Source: <url>", '
        'then a line of hashtags\n'
        '- "hashtags": list of 3-6 tags without the # symbol\n'
        '- "pexels_query": 2-4 word generic visual search query (e.g. "server room")\n'
        + '- "broll_queries": one stock-video search query per paragraph of the script, in order, 2-4 words '
        'each: a concrete, filmable scene that matches what that paragraph is about (e.g. "hospital hallway", '
        '"phone text message", "credit card payment", "office laptop typing", "car dashboard", "server room", '
        '"padlock keyboard"). No brand names, logos, real people, violence or text on screen\n'
        '- "keywords": 3-6 short words or phrases copied from your script (product, threat, action) '
        'to emphasise on screen, each max 24 characters\n'
        + (f'- "alt_hooks": list of {n_hooks - 1} different opening hooks for the same story, each one '
           f'paragraph of at most {MAX_HOOK_WORDS} words, same facts as the script, written to replace '
           f'the script\'s first paragraph\n' if n_hooks > 1 else "")
        + ('- "series": the exact name of the series this story fits, or "" if none fits\n'
           if options else "")
        + (f'- "first_comment": a question to pin as the first comment, max {MAX_COMMENT_CHARS} '
           f'characters, ending with "?", no URLs, no hashtags\n' if want_comment else "")
        + ('- "data_card": {"cve_id": "CVE-YYYY-NNNN", "cvss": number or null, "severity": string or null, '
           '"products": [up to 2 product names], "exploited": true/false/null, "patch_available": '
           'true/false/null}, using ONLY facts stated in the source; null for anything not stated\n'
           if want_card else "")
    )
    last: Exception | None = None
    for attempt in range(2):
        text = chat(cfg, [{"role": "system", "content": system}, {"role": "user", "content": user}])
        try:
            return validate_script(extract_json(text), story, cfg, series_options=options or None)
        except ValueError as e:
            last = e
            log.warning("Script parse/validate failed (attempt %d): %s", attempt + 1, e)
    raise RuntimeError(f"LLM did not return a valid script: {last}")


def _tag_instructions(cfg: Config) -> str:
    allowed = cfg.get("voice.elevenlabs.allowed_tags", None) or _tags_mod().DEFAULT_ALLOWED
    max_tags = cfg.get("voice.elevenlabs.max_tags", None) or _tags_mod().DEFAULT_MAX_TAGS
    names = ", ".join(f"[{a}]" for a in allowed)
    return (f"- You may add at most {max_tags} voice tags from this list, placed right before the "
            f"words they colour: {names}. Never put a tag on its own line, and never use any other "
            "tag. Use them sparingly.\n")


def _series_instructions(options: list[dict]) -> str:
    lines = []
    for o in options:
        line = f'- "{o.get("name", "")}": fits when the story is about {o.get("match", "")}.'
        if o.get("guidance"):
            line += f' If you pick it: {o["guidance"]}'
        lines.append(line)
    return ("\nThis channel runs recurring series. If the story clearly fits one, set the \"series\" "
            "key to its exact name and follow its guidance; otherwise leave it empty.\n"
            + "\n".join(lines) + "\n")


def pick_hook(cfg: Config, story: dict, hooks: list[str]) -> int:
    """Index of the strongest hook; 0 on any error or when there is nothing to choose."""
    if not hooks or len(hooks) < 2:
        return 0
    try:
        lines = "\n".join(f"{i}. {h}" for i, h in enumerate(hooks))
        prompt = (
            "You edit a short-form IT and cybersecurity news channel. Pick the opening hook most "
            "likely to stop a viewer from scrolling. All hooks state the same facts; prefer the one "
            "with the clearest stakes and no clickbait or exaggeration.\n\n"
            f"Story: {story.get('title', '')}\n\nHooks:\n{lines}\n\n"
            'Respond with JSON only: {"index": <number>}'
        )
        model = (str(cfg.get("llm.hook_model", "") or "").strip()
                 or str(cfg.get("llm.fact_check_model", "") or "").strip())
        text = chat(cfg, [{"role": "user", "content": prompt}], temperature=0, model=model or None)
        idx = int(extract_json(text).get("index", 0))
        if 0 <= idx < len(hooks):
            return idx
        log.warning("pick_hook index %s out of range; using 0", idx)
    except Exception as e:  # noqa: BLE001
        log.warning("pick_hook failed (%s); using 0", type(e).__name__)
    return 0


def fact_check(cfg: Config, story: dict, data: dict) -> dict:
    """Check the script against the source. Returns {"ok": bool, "issues": [...]}.
    Never raises: any failure gives {"ok": True, "issues": [], "skipped": reason}."""
    try:
        source = f"{story.get('title', '')}\n{story.get('summary', '')}".strip()
        article = str(story.get("article") or "").strip()
        if article:
            source = f"{source}\n\n{article}"
        sign_off = str(cfg.get("channel.sign_off", "") or "").strip()
        claims = (f"on_screen_title: {data.get('on_screen_title', '')}\n"
                  f"script: {data.get('script', '')}\n"
                  f"description: {data.get('description', '')}")
        active = data.get("hook_index") if isinstance(data.get("hook_index"), int) else 0
        alts = [str(h) for i, h in enumerate(data.get("hooks") or []) if i != active and str(h).strip()]
        if alts:
            claims += "\nalternative opening hooks: " + " | ".join(alts)
        prompt = (
            "You are a strict fact-checker for a short IT/security news video.\n"
            "List each claim in the script, on_screen_title, description or alternative opening hooks that the source does not support. "
            "Rephrasing, simplification and summary are supported; only flag facts (numbers, names, "
            "versions, dates, who/what is affected, exploitation status) that the source does not state "
            "or that it contradicts. Ignore tone, the hook's framing and the sign-off"
            + (f' (the sign-off is: "{sign_off}"; ignore it entirely)' if sign_off else "") + ".\n\n"
            f"SOURCE:\n{source}\n\nVIDEO TEXT:\n{claims}\n\n"
            'Return JSON only: {"issues": ["short description of each unsupported claim", ...]} '
            "with at most 5 issues, each under 140 characters. Use an empty list if everything is supported."
        )
        model = str(cfg.get("llm.fact_check_model", "") or "").strip()
        text = chat(cfg, [{"role": "user", "content": prompt}], temperature=0, model=model or None)
        raw = extract_json(text).get("issues") or []
        if isinstance(raw, str):
            raw = [raw]
        issues = [_truncate(" ".join(str(i).split()), 140) for i in raw if str(i).strip()][:5]
        return {"ok": not issues, "issues": issues}
    except Exception as e:  # noqa: BLE001
        log.warning("fact_check skipped: %s", type(e).__name__)
        return {"ok": True, "issues": [], "skipped": f"{type(e).__name__}"}
