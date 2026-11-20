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


def validate_script(data: dict, story: dict, cfg: Config) -> dict:
    """Validate + coerce LLM script JSON. Raises ValueError if essential fields are missing."""
    for key in ("on_screen_title", "script"):
        if not str(data.get(key, "")).strip():
            raise ValueError(f"Script JSON missing '{key}'")
    sign_off = cfg.get("channel.sign_off", "")
    script = str(data["script"]).strip()
    if sign_off and sign_off.lower().rstrip(".!") not in script.lower():
        script = f"{script}\n\n{sign_off}"
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
    yt_title = str(data.get("youtube_title") or data["on_screen_title"]).strip()
    yt_title = re.sub(r"\s*#Shorts\s*$", "", yt_title, flags=re.I)
    yt_title = _truncate(yt_title, 95 - len(" #Shorts")) + " #Shorts"
    desc = str(data.get("description") or "").strip()
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
    return {
        "on_screen_title": _truncate(str(data["on_screen_title"]), 42),
        "script": script,
        "youtube_title": yt_title,
        "description": desc,
        "hashtags": tags,
        "pexels_query": query,
        "keywords": keywords[:6],
    }


def write_script(cfg: Config, story: dict) -> dict:
    lo, hi = cfg.get("llm.target_words", [65, 95])
    sign_off = cfg.get("channel.sign_off", "")
    style_notes = str(cfg.get("llm.style_notes", "") or "").strip()
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
        + "\nReturn JSON only with these keys:\n"
        '- "on_screen_title": punchy headline for the top of the video, max 42 characters\n'
        '- "script": the narration\n'
        '- "youtube_title": max 95 characters, ending with " #Shorts"\n'
        '- "description": 2-3 sentences (include any CVE ids or versions here), then "Source: <url>", '
        'then a line of hashtags\n'
        '- "hashtags": list of 3-6 tags without the # symbol\n'
        '- "pexels_query": 2-4 word generic visual search query (e.g. "server room")\n'
        '- "keywords": 3-6 short words or phrases copied from your script (product, threat, action) '
        'to emphasise on screen, each max 24 characters\n'
    )
    last: Exception | None = None
    for attempt in range(2):
        text = chat(cfg, [{"role": "system", "content": system}, {"role": "user", "content": user}])
        try:
            return validate_script(extract_json(text), story, cfg)
        except ValueError as e:
            last = e
            log.warning("Script parse/validate failed (attempt %d): %s", attempt + 1, e)
    raise RuntimeError(f"LLM did not return a valid script: {last}")


def fact_check(cfg: Config, story: dict, data: dict) -> dict:
    """Check the script against the source. Returns {"ok": bool, "issues": [...]}.
    Never raises: any failure gives {"ok": True, "issues": [], "skipped": reason}."""
    try:
        source = str(story.get("article") or "").strip()
        if not source:
            source = f"{story.get('title', '')}\n{story.get('summary', '')}".strip()
        claims = (f"on_screen_title: {data.get('on_screen_title', '')}\n"
                  f"script: {data.get('script', '')}\n"
                  f"description: {data.get('description', '')}")
        prompt = (
            "You are a strict fact-checker for a short IT/security news video.\n"
            "List each claim in the script, on_screen_title or description that the source does not support. "
            "Rephrasing, simplification and summary are supported; only flag facts (numbers, names, "
            "versions, dates, who/what is affected, exploitation status) that the source does not state "
            "or that it contradicts. Ignore tone, the hook's framing and the sign-off.\n\n"
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
