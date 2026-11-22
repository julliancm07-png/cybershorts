"""Word-timed captions -> ASS subtitle file. Pure helpers import without faster-whisper."""
from __future__ import annotations

import logging
import re
from pathlib import Path

from .config import Config

log = logging.getLogger("cybershorts")

Word = tuple[float, float, str]
Chunk = tuple[float, float, str]

_BREAK_CHARS = (".", "!", "?", ",")


_MODEL_CACHE: dict = {}


def _model(cfg: Config):
    from faster_whisper import WhisperModel  # lazy

    name = cfg.get("captions.whisper_model", "base.en")
    if name not in _MODEL_CACHE:
        _MODEL_CACHE[name] = WhisperModel(name, device="cpu", compute_type="int8",
                                          download_root=str(cfg.path("models")))
    return _MODEL_CACHE[name]


def transcribe_words(cfg: Config, wav: Path | str, prompt: str = "") -> list[Word]:
    model = _model(cfg)
    segments, _info = model.transcribe(
        str(wav), word_timestamps=True, initial_prompt=(prompt or "")[:800] or None
    )
    words: list[Word] = []
    for seg in segments:
        for w in seg.words or []:
            text = w.word.strip()
            if text:
                words.append((float(w.start), float(w.end), text))
    return words


_NORM = __import__("re").compile(r"[^a-z0-9]+")


def _norm(w: str) -> str:
    return _NORM.sub("", w.lower())


def align_to_script(words: list[Word], script: str, min_ratio: float = 0.5) -> list[Word]:
    """Put the script's exact words on Whisper's timings.

    Whisper hears the spoken text (which may use pronunciation spellings like "Sisa"),
    so captions would otherwise show its guesses. Matched words keep their timing;
    mismatched runs are spread across the Whisper span they replaced. If the audio
    differs too much from the script (e.g. an ad-libbed voice memo), Whisper's own
    words are kept.
    """
    import difflib

    script_words = [w for w in (script or "").split() if _norm(w)]
    if not words or not script_words:
        return words
    a = [_norm(w[2]) for w in words]
    b = [_norm(w) for w in script_words]
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    if sm.ratio() < min_ratio:
        log.info("Script/audio match %.2f too low; keeping Whisper words", sm.ratio())
        return words
    out: list[Word] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out.extend((words[i][0], words[i][1], script_words[j]) for i, j in zip(range(i1, i2), range(j1, j2)))
        elif tag in ("replace", "insert"):
            if i2 > i1:
                start, end = words[i1][0], words[i2 - 1][1]
            else:  # script words with no audio match: squeeze between neighbours
                start = out[-1][1] if out else (words[0][0] if words else 0.0)
                end = words[i1][0] if i1 < len(words) else start + 0.3 * (j2 - j1)
                end = max(end, start + 0.05)
            n = j2 - j1
            step = (end - start) / n
            out.extend((start + k * step, start + (k + 1) * step, script_words[j1 + k]) for k in range(n))
        # "delete": audio words not in the script (rare) are dropped
    return out


def _paragraphs(script: str) -> list[str]:
    import re
    return [p.strip() for p in re.split(r"\n\s*\n|\n", script or "") if p.strip()]


def spread_words(text: str, start: float, end: float) -> list[Word]:
    """Even timing for a paragraph's words across a known span, weighted by word length."""
    toks = [t for t in (text or "").split() if _norm(t)]
    if not toks:
        return []
    end = max(end, start + 0.1 * len(toks))
    weights = [len(_norm(t)) + 2 for t in toks]
    total, t, out = sum(weights), start, []
    for tok, wgt in zip(toks, weights):
        d = (end - start) * wgt / total
        out.append((t, t + d * 0.9, tok))
        t += d
    return out


def transcribe_by_spans(cfg: Config, wav: Path | str, spans: list[list[float]], script: str) -> list[Word] | None:
    """Caption timings when the narration's beat spans are known exactly (our own TTS).

    Each beat is transcribed on its own short clip, so a Whisper slip can't shift later
    beats, and a beat Whisper fumbles gets evenly spread words inside its known span.
    Returns None if the spans don't match the script's paragraphs.
    """
    import subprocess
    import tempfile

    paras = _paragraphs(script)
    if not spans or len(spans) != len(paras):
        log.info("Beat spans (%d) don't match script paragraphs (%d); using full transcription",
                 len(spans or []), len(paras))
        return None
    out: list[Word] = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, ((s, e), text) in enumerate(zip(spans, paras)):
            want = [t for t in text.split() if _norm(t)]
            if not want:
                continue
            pad_s = max(0.0, s - 0.15)
            clip = Path(tmp) / f"beat_{i}.wav"
            words: list[Word] = []
            try:
                subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{pad_s:.3f}",
                                "-t", f"{(e - pad_s) + 0.25:.3f}", "-i", str(wav), "-ar", "16000", "-ac", "1",
                                str(clip)], check=True, capture_output=True, timeout=120)
                raw = transcribe_words(cfg, clip, text)
                words = [(a + pad_s, b + pad_s, t) for a, b, t in raw]
            except Exception as ex:  # noqa: BLE001
                log.warning("Beat %d transcription failed (%s); spreading words evenly", i, ex)
            aligned = align_to_script(words, text) if words else []
            ok = len(aligned) == len(want) and all(
                s - 0.3 <= a <= e + 0.3 for a, _b, _t in aligned)
            if not ok:
                log.info("Beat %d: using even word timing inside its known span", i)
                aligned = spread_words(text, s, e)
            # keep inside the beat and monotonic
            fixed: list[Word] = []
            prev = out[-1][1] if out else 0.0
            for a, b, t in aligned:
                a = min(max(a, s - 0.05, prev), e)
                b = max(min(b, e + 0.1), a + 0.05)
                fixed.append((a, b, t))
                prev = a
            out.extend(fixed)
    return out


def chunk_words(words: list[Word], n: int = 3) -> list[Chunk]:
    """Group words into chunks of at most n; also break after words ending in . ! ? ,"""
    n = max(1, int(n))
    chunks: list[Chunk] = []
    cur: list[Word] = []

    def flush() -> None:
        if cur:
            chunks.append((cur[0][0], cur[-1][1], " ".join(w[2] for w in cur)))
            cur.clear()

    for w in words:
        cur.append(w)
        if len(cur) >= n or w[2].rstrip().endswith(_BREAK_CHARS):
            flush()
    flush()
    return chunks


def format_ass_time(t: float) -> str:
    """Seconds -> H:MM:SS.cc"""
    cs = int(round(max(0.0, t) * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def _escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\r", "").replace("\n", "\\N")
    )


def ass_color(hex_color: str, default: str = "#FFFFFF") -> str:
    """'#RRGGBB' -> ASS '&H00BBGGRR'. Invalid input falls back to `default`."""
    for cand in (hex_color, default):
        m = re.fullmatch(r"#?([0-9a-fA-F]{6})", str(cand or "").strip())
        if m:
            rr, gg, bb = m.group(1)[0:2], m.group(1)[2:4], m.group(1)[4:6]
            return f"&H00{bb}{gg}{rr}".upper()
    return "&H00FFFFFF"


def _beat_paragraphs(script: str) -> list[int]:
    counts = []
    for para in re.split(r"\n+", script or ""):
        n = sum(1 for tok in para.split() if _norm(tok))
        if n:
            counts.append(n)
    return counts


def beat_ranges(script: str) -> list[int]:
    """Word counts per beat (paragraphs split on newlines), counted like align_to_script."""
    return _beat_paragraphs(script)


def beat_times(words: list[Word], script: str, duration: float) -> list[float]:
    """Start time (s) of each beat; first is 0.0; strictly increasing; at least one entry."""
    counts = beat_ranges(script)
    if len(counts) <= 1:
        return [0.0]
    total = sum(counts)
    if words and len(words) == total:
        times, idx = [], 0
        for c in counts:
            times.append(float(words[idx][0]))
            idx += c
    else:
        d = max(float(duration), 0.0)
        times, acc = [], 0
        for c in counts:
            times.append(d * acc / total)
            acc += c
    times[0] = 0.0
    for i in range(1, len(times)):
        if times[i] <= times[i - 1]:
            times[i] = times[i - 1] + 0.05
    return times


def _plain_text(text: str) -> str:
    return _escape(text.upper())


def _fit_events(evs: list[tuple[float, float, str]], limit: float | None = None) -> list[str]:
    """Clamp each event to the next one's start (no overlap on a layer) and to `limit`;
    drop events that are empty after centisecond rounding. Returns Dialogue lines."""
    out: list[str] = []
    for i, (start, end, body) in enumerate(evs):
        if limit is not None:
            if start >= limit:
                continue
            end = min(end, limit)
        if i + 1 < len(evs):
            end = min(end, evs[i + 1][0] - 0.01)  # too tight to fit: dropped below
        if round(end * 100) <= round(start * 100):
            continue
        out.append(f"Dialogue: 1,{format_ass_time(start)},{format_ass_time(end)},Caption,,0,0,0,,{body}\n")
    return out


def _karaoke_events(chunks: list[Chunk], words: list[Word], accent: str, kw_color: str,
                    keywords: set[str], pos: str, limit: float | None = None) -> list[str] | None:
    """One Dialogue per word; None if words can't be mapped onto chunks."""
    sizes = [len(c[2].split()) for c in chunks]
    if sum(sizes) != len(words):
        return None
    out: list[tuple[float, float, str]] = []
    idx = 0
    for (c_start, c_end, _text), n in zip(chunks, sizes):
        cw = words[idx: idx + n]
        idx += n
        for k, (ws, _we, _wt) in enumerate(cw):
            end = cw[k + 1][0] if k + 1 < len(cw) else c_end
            start = ws if k else c_start
            if end <= start:
                end = start + 0.05
            parts = []
            for j, (_s, _e, wt) in enumerate(cw):
                cur = j == k
                is_kw = _norm(wt) in keywords
                col = accent if cur else (kw_color if is_kw else "&H00FFFFFF")
                scale = 112 if cur else 100
                tag = f"\\c{col}&\\b1"
                if k == 0:
                    tag += f"\\fscx85\\fscy85\\t(0,90,\\fscx{scale}\\fscy{scale})"
                else:
                    tag += f"\\fscx{scale}\\fscy{scale}"
                sep = " " if j else ""
                parts.append(f"{sep}{{{tag}}}{_plain_text(wt)}")
            out.append((start, end, f"{{{pos}}}{''.join(parts)}"))
    return _fit_events(out, limit)


# ---------- v3 overlays: badge pill + CVE data card ----------
def _rrect(w: float, h: float, r: float) -> str:
    """ASS drawing of a rounded rectangle with its top-left at (0, 0)."""
    w, h = int(round(w)), int(round(h))
    r = int(max(0, min(r, w // 2, h // 2)))
    k = int(round(r * 0.5523))
    return (f"m {r} 0 l {w - r} 0 b {w - r + k} 0 {w} {r - k} {w} {r} l {w} {h - r} "
            f"b {w} {h - r + k} {w - r + k} {h} {w - r} {h} l {r} {h} "
            f"b {r - k} {h} 0 {h - r + k} 0 {h - r} l 0 {r} b 0 {r - k} {r - k} 0 {r} 0")


def _est_width(text: str, size: float, factor: float = 0.72) -> float:
    return len(text) * size * factor


def _sev_color(cvss, severity) -> str:
    try:
        v = float(cvss)
    except (TypeError, ValueError):
        v = None
    if v is not None:
        return "#FF3B30" if v >= 9 else "#FF9500" if v >= 7 else "#FFD60A" if v >= 4 else "#34C759"
    sev = str(severity or "").strip().lower()
    return {"critical": "#FF3B30", "high": "#FF9500", "medium": "#FFD60A", "low": "#34C759"}.get(sev, "#FFD60A")


def _card_rows(card: dict) -> list[tuple[str, str, str]]:
    """(kind, text, colour) rows for the truthy values only, in display order."""
    rows: list[tuple[str, str, str]] = []
    cve = str(card.get("cve_id") or "").strip()
    if cve:
        rows.append(("id", cve.upper(), "#FFFFFF"))
    cvss, sev = card.get("cvss"), str(card.get("severity") or "").strip()
    score = ""
    if cvss not in (None, "", False, 0, 0.0):
        try:
            score = f"{float(cvss):.1f}"
        except (TypeError, ValueError):
            score = str(cvss).strip()
    if score or sev:
        label = " ".join(x for x in ((f"CVSS {score}" if score else ""), sev.upper()) if x)
        rows.append(("cvss", label, _sev_color(cvss if score else None, sev)))
    products = card.get("products")
    if isinstance(products, (list, tuple)):
        products = ", ".join(str(x).strip() for x in products if str(x).strip())
    products = str(products or "").strip()
    if products:
        txt = f"Affects: {products}"
        if len(txt) > 40:
            txt = txt[:39].rstrip(" ,") + "\u2026"
        rows.append(("affects", txt, "#FFFFFF"))
    if card.get("exploited"):
        rows.append(("exploited", "ACTIVELY EXPLOITED", "#FF3B30"))
    if card.get("patch_available"):
        rows.append(("patch", "PATCH AVAILABLE \u2713", "#34C759"))
    return rows


def _badge_events(text: str, color: str, w: int, h: int, hook_title: str, hook_size: int, end: float) -> list[str]:
    text = (text or "").strip()
    if not text or end <= 0:
        return []
    fs = int(w * 0.05)
    tw = _est_width(text.upper(), fs, 0.74)
    pw, ph = int(tw + 2 * 0.5 * fs + 10), int(fs * 1.55)
    pw = min(pw, int(w * 0.9))
    cx, hook_cy = w // 2, int(h * 0.36)
    per_line = max(4, int((w - 140) / (hook_size * 0.74)))
    lines = max(1, -(-len(hook_title or "") // per_line)) if hook_title else 1
    top = hook_cy - int(lines * hook_size * 1.2 / 2) - int(fs * 0.7) - ph
    top = max(top, int(h * 0.08))
    x0, t0, t1 = format_ass_time(0), format_ass_time(0), format_ass_time(end)
    col = ass_color(color, "#FF3B30")
    return [
        f"Dialogue: 4,{t0},{t1},Card,,0,0,0,,{{\\an7\\pos({cx - pw // 2},{top})\\fad(120,200)\\p1\\c{col}&\\bord0\\shad0}}"
        f"{_rrect(pw, ph, ph // 2)}{{\\p0}}\n",
        f"Dialogue: 5,{x0},{t1},Card,,0,0,0,,{{\\an5\\q0\\pos({cx},{top + ph // 2})\\fad(120,200)"
        f"\\fs{fs}\\b1\\bord0\\shad0\\c&H00000000&}}{_escape(text.upper())}\n",
    ]


def _card_events(card: dict, accent: str, w: int, h: int, start: float, end: float,
                 title_bottom: float) -> list[str]:
    rows = _card_rows(card)
    if not rows or end - start < 1.0:
        return []
    sc = w / 1080.0
    pad, box_w = int(34 * sc), int(920 * sc)
    heights = {"id": 86, "cvss": 84, "affects": 62, "exploited": 62, "patch": 62}
    box_h = 2 * pad + int(sum(heights[k] for k, _t, _c in rows) * sc)
    cx = w // 2
    cy = max(int(h * 0.21), int(title_bottom + 24 * sc + box_h / 2))
    top = cy - box_h // 2
    t0, t1 = format_ass_time(start), format_ass_time(end)
    mv = lambda x, y: f"\\move({x},{y + int(60 * sc)},{x},{y},0,260)"  # noqa: E731
    out = [
        f"Dialogue: 4,{t0},{t1},Card,,0,0,0,,{{\\an7\\q0{mv(cx - box_w // 2, top)}\\fad(220,220)\\p1"
        f"\\c&H00141414&\\1a&H18&\\3c{accent}&\\bord{max(2, int(4 * sc))}\\shad0}}"
        f"{_rrect(box_w, box_h, int(28 * sc))}{{\\p0}}\n"
    ]
    y = top + pad
    for i, (kind, text, color) in enumerate(rows):
        rh = int(heights[kind] * sc)
        ry = y + rh // 2
        y += rh
        a0 = 160 + 120 * i
        reveal = f"\\alpha&HFF&\\t({a0},{a0 + 120},\\alpha&H00&)"
        fs = {"id": 66, "cvss": 46, "affects": 42, "exploited": 46, "patch": 46}[kind]
        fs = int(fs * sc)
        avail = box_w - 2 * pad
        est = _est_width(text, fs, 0.74 if kind != "affects" else 0.66)
        if est > avail:
            fs = max(int(fs * avail / est), int(24 * sc))
        if kind == "cvss":
            ph = int(64 * sc)
            pw = min(int(_est_width(text, fs, 0.74) + fs * 1.2), avail)
            px = cx - pw // 2
            out.append(
                f"Dialogue: 4,{t0},{t1},Card,,0,0,0,,{{\\an7\\q0{mv(px, ry - ph // 2)}\\fad(0,220){reveal}"
                f"\\p1\\c{ass_color(color)}&\\bord0\\shad0}}{_rrect(pw, ph, ph // 2)}{{\\p0}}\n")
            out.append(
                f"Dialogue: 5,{t0},{t1},Card,,0,0,0,,{{\\an5\\q0{mv(cx, ry)}\\fad(0,220){reveal}\\fs{fs}\\b1"
                f"\\bord0\\shad0\\c&H00000000&}}{_escape(text)}\n")
            continue
        col = accent if kind == "id" else ass_color(color)
        out.append(
            f"Dialogue: 5,{t0},{t1},Card,,0,0,0,,{{\\an5\\q0{mv(cx, ry)}\\fad(0,220){reveal}\\fs{fs}\\b1"
            f"\\bord0\\shad0\\c{col}&}}{_escape(text)}\n")
    return out


def build_ass(chunks: list[Chunk], cfg: Config, title: str, handle: str, duration: float, *,
              words: list[Word] | None = None, keywords: list[str] | None = None,
              beats: list[float] | None = None, badge: str | None = None,
              badge_color: str | None = None, card: dict | None = None) -> str:
    w = int(cfg.get("video.width", 1080))
    h = int(cfg.get("video.height", 1920))
    font = cfg.get("captions.font", "DejaVu Sans")
    size = int(cfg.get("captions.font_size", 92))
    style = str(cfg.get("captions.style", "karaoke")).lower()
    hook_s = float(cfg.get("video.hook_seconds", 1.6) or 0)
    accent = ass_color(cfg.get("branding.accent", "#3DDCFF"), "#3DDCFF")
    kw_color = ass_color(cfg.get("branding.keyword_color", "#FFD60A"), "#FFD60A")
    bar_on = bool(cfg.get("branding.progress_bar", True))
    end_card_on = bool(cfg.get("branding.end_card", True))
    card_rows = _card_rows(card) if card else []
    card_style = (f"Style: Card,{font},46,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1\n"
                  if (badge or card_rows) else "")
    # ASS colours are &HAABBGGRR
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {w}\nPlayResY: {h}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Caption,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,0,5,60,60,0,1\n"
        f"Style: Title,{font},{int(size * 0.62)},&H0000E5FF,&H0000E5FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,5,0,8,70,70,260,1\n"
        f"Style: Handle,{font},{int(size * 0.4)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,0,2,60,60,150,1\n"
        f"Style: Hook,{font},{int(size * 1.15)},{kw_color},{kw_color},&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,8,0,5,70,70,0,1\n"
        f"Style: Bar,{font},20,{accent},{accent},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1\n"
        f"{card_style}\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    dur = max(duration, 0.1)
    end_all = format_ass_time(dur)
    lines = [header]

    show_end_card = bool(end_card_on and beats and len(beats) >= 2 and 0 < beats[-1] < dur
                         and dur - beats[-1] <= 4.5)
    title_start = min(hook_s, dur) if (title and hook_s > 0) else 0.0
    title_end = beats[-1] if show_end_card else dur  # type: ignore[index]
    if title and hook_s > 0:
        he = format_ass_time(title_start)
        lines.append(
            f"Dialogue: 2,{format_ass_time(0)},{he},Hook,,0,0,0,,"
            f"{{\\an5\\q0\\pos({w // 2},{int(h * 0.36)})\\fad(0,180)"
            f"\\fscx70\\fscy70\\t(0,220,\\fscx100\\fscy100)}}{_plain_text(title)}\n"
        )
    if title and title_end > title_start:
        lines.append(
            f"Dialogue: 0,{format_ass_time(title_start)},{format_ass_time(title_end)},Title,,0,0,0,,"
            f"{_escape(title.upper())}\n"
        )
    if handle:
        handle_end = format_ass_time(beats[-1]) if show_end_card else end_all
        lines.append(f"Dialogue: 0,{format_ass_time(0)},{handle_end},Handle,,0,0,0,,{_escape(handle)}\n")
    # Caption centred around y ~1150: Alignment 5 (middle-centre) with \pos
    cx, cy = w // 2, int(h * 0.6)
    pos = f"\\pos({cx},{cy})"
    limit = beats[-1] if show_end_card else None  # type: ignore[index]
    events = None
    if style == "karaoke" and words and chunks:
        kws = {n for k in (keywords or []) for t in str(k).split() if (n := _norm(t))}
        events = _karaoke_events(chunks, words, accent, kw_color, kws, pos, limit)
    if events is None:
        events = _fit_events([(a, b, f"{{{pos}}}{_plain_text(t)}") for a, b, t in chunks], limit)
    lines.extend(events)
    if show_end_card:
        start = format_ass_time(beats[-1])  # type: ignore[index]
        lines.append(
            f"Dialogue: 2,{start},{end_all},Caption,,0,0,0,,"
            f"{{\\an5\\q0\\pos({w // 2},{int(h * 0.42)})\\fad(150,0)}}"
            f"{{\\c{accent}&\\fscx100\\fscy100}}FOLLOW"
            + (f"\\N{{\\c&H00FFFFFF&\\fscx120\\fscy120}}{_escape(handle)}" if handle else "")
            + "\n"
        )
    ev_limit = beats[-1] if show_end_card else dur  # type: ignore[index]
    if badge and str(badge).strip():
        b_end = min(max(hook_s, 1.6), dur)
        lines.extend(_badge_events(str(badge), badge_color or cfg.get("breaking.badge_color", "#FF3B30"),
                                   w, h, title or "", int(size * 1.15), b_end))
    if card_rows:
        bs = list(beats or [])
        c_start = float(bs[1]) if len(bs) >= 2 else 2.0
        c_end = min(c_start + float(cfg.get("data_card.seconds", 4.5) or 4.5), ev_limit)
        if len(bs) >= 3:
            c_end = min(c_end, float(bs[2]))
        per_line = max(4, int((w - 140) / (int(size * 0.62) * 0.74)))
        t_lines = max(1, -(-len(title or "") // per_line)) if title else 0
        title_bottom = 260 + t_lines * int(size * 0.62) * 1.2 if t_lines else 0
        lines.extend(_card_events(card or {}, accent, w, h, c_start, c_end, title_bottom))
    if bar_on:
        dur_ms = int(round(dur * 1000))
        lines.append(
            f"Dialogue: 3,{format_ass_time(0)},{end_all},Bar,,0,0,0,,"
            f"{{\\an7\\pos(0,{h - 14})\\p1\\fscx0\\t(0,{dur_ms},\\fscx100)\\c{accent}&"
            f"\\bord0\\shad0}}m 0 0 l {w} 0 {w} 14 0 14{{\\p0}}\n"
        )
    return "".join(lines)


def write_ass(chunks: list[Chunk], cfg: Config, title: str, handle: str, duration: float,
              out_path: Path | str, *, words: list[Word] | None = None,
              keywords: list[str] | None = None, beats: list[float] | None = None,
              badge: str | None = None, badge_color: str | None = None, card: dict | None = None) -> Path:
    out = Path(out_path)
    out.write_text(
        build_ass(chunks, cfg, title, handle, duration, words=words, keywords=keywords, beats=beats,
                  badge=badge, badge_color=badge_color, card=card),
        encoding="utf-8",
    )
    return out
