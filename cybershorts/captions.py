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


def transcribe_words(cfg: Config, wav: Path | str, prompt: str = "") -> list[Word]:
    from faster_whisper import WhisperModel  # lazy

    model = WhisperModel(
        cfg.get("captions.whisper_model", "base.en"),
        device="cpu",
        compute_type="int8",
        download_root=str(cfg.path("models")),
    )
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


def build_ass(chunks: list[Chunk], cfg: Config, title: str, handle: str, duration: float, *,
              words: list[Word] | None = None, keywords: list[str] | None = None,
              beats: list[float] | None = None) -> str:
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
        f"Style: Bar,{font},20,{accent},{accent},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    dur = max(duration, 0.1)
    end_all = format_ass_time(dur)
    lines = [header]

    show_end_card = bool(end_card_on and beats and len(beats) >= 2 and 0 < beats[-1] < dur)
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
              keywords: list[str] | None = None, beats: list[float] | None = None) -> Path:
    out = Path(out_path)
    out.write_text(
        build_ass(chunks, cfg, title, handle, duration, words=words, keywords=keywords, beats=beats),
        encoding="utf-8",
    )
    return out
