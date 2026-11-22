"""Newsroom recap layout: schedules scenes/points/checklist per story and assembles the ASS files.

``build_chrome_ass`` is static (baked once into the background PNG by ``recap_render.render_background``);
``build_newsroom_ass`` holds everything that moves. Geometry follows design/recap-v2/mock.py (1920x1080 design;
libass scales PlayRes to the video size). Pure: no I/O except ``write_newsroom_ass``.
"""
from __future__ import annotations

import logging
import math
import re
from pathlib import Path
from urllib.parse import urlparse

from . import captions as C
from . import recap_scenes as S
from . import recap_visuals as V

log = logging.getLogger(__name__)

__all__ = ["schedule", "fit_chunks", "build_chrome_ass", "build_newsroom_ass", "write_newsroom_ass"]

W, H = 1920, 1080
BOX = S.Box(*S.PANEL_BOX)
PX, PY, PW, PH = 1260, 146, 600, 862            # right info panel
CAP = (90, 860, 1100, 92)                       # caption box x, y, w, h
TRACK = (60, 1040, 1800, 8)                     # progress track x, y, w, h
CAP_CX, CAP_CY = 640, 906
MIN_POINT_GAP = 1.2
CHECKLIST_GAP = 3.0
CHECKLIST_MIN = 4.0
CAP_FACTOR = 0.70       # average width/size of the bold upper-case caption font (DejaVu Sans Bold)
CAP_INNER_W = 1040      # usable caption width inside the box


# ---------------------------------------------------------------- config helpers
def _g(cfg, key: str, default):
    try:
        v = cfg.get("recap.newsroom." + key, default)
    except Exception:  # noqa: BLE001
        v = default
    return default if v is None else v


def _r(cfg, key: str, default):
    try:
        v = cfg.get("recap." + key, default)
    except Exception:  # noqa: BLE001
        v = default
    return default if v is None else v


def _num(v, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _tlist(times, key: str, n: int) -> list:
    raw = times.get(key) if isinstance(times, dict) else None
    out = []
    for i in range(n):
        v = raw[i] if isinstance(raw, (list, tuple)) and i < len(raw) else None
        try:
            f = float(v)
            out.append(f if math.isfinite(f) else None)
        except (TypeError, ValueError):
            out.append(None)
    return out


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _has_card(card) -> bool:
    return isinstance(card, dict) and bool(str(card.get("cve_id") or "").strip())


# ---------------------------------------------------------------- scheduling
def schedule(cfg, part: dict, vis: dict | None, times: dict | None) -> dict:
    """Timeline of one story part.

    ``times`` = {"scenes": [t|None..], "points": [..], "checklist": [..]} (aligned with the vis lists; None or
    missing -> evenly spaced inside the part). Returns {transition: (s, e), scenes: [(s, e, scene)],
    points: [(t, text)], checklist: (start, [tick times]) | None}; every time lies within [start, end].
    """
    ps, pe = float(part["start"]), float(part["end"])
    pe = max(pe, ps)
    n = pe - ps
    vis = vis if isinstance(vis, dict) else {}
    card = part.get("card")
    tsec = max(0.0, _num(_g(cfg, "transition_seconds", 3.0), 3.0))
    tr_end = ps + min(tsec, 0.4 * n)
    w0, w1 = tr_end, pe
    smin = max(1.0, _num(_g(cfg, "scene_min_seconds", 6), 6.0))
    smax = max(smin, _num(_g(cfg, "scene_max_seconds", 18), 18.0))
    sdef = _clamp(_num(_g(cfg, "scene_seconds", 12), 12.0), smin, smax)

    scenes = _schedule_scenes(vis, times, w0, w1, smin, smax, sdef, part, card)
    points = _schedule_points(cfg, vis, times, tr_end, pe, card)
    checklist = _schedule_checklist(cfg, vis, times, tr_end, pe, points)
    return {"transition": (ps, tr_end), "scenes": scenes, "points": points, "checklist": checklist}


def _schedule_scenes(vis, times, w0, w1, smin, smax, sdef, part, card):
    allsc = vis.get("scenes") if isinstance(vis.get("scenes"), list) else []
    raw = _tlist(times, "scenes", len(allsc))
    valid = [j for j, sc in enumerate(allsc) if isinstance(sc, dict) and str(sc.get("type") or "") in S.BUILDERS]
    timed = []
    for k, j in enumerate(valid):
        t = raw[j]
        if t is None:
            t = w0 + (w1 - w0) * k / max(1, len(valid))
        timed.append((_clamp(t, w0, w1), allsc[j]))
    timed.sort(key=lambda x: x[0])
    starts: list[float] = []
    keep: list[dict] = []
    for t, sc in timed:
        if starts:
            t = max(t, starts[-1] + smin)
        if t + smin > w1:
            t = w1 - smin                       # pull back so the scene still fits
            if t < w0 or (starts and t < starts[-1] + smin):
                continue
        starts.append(t)
        keep.append(sc)
    out: list[tuple[float, float, dict]] = []
    for i, (t, sc) in enumerate(zip(starts, keep)):
        nxt = starts[i + 1] if i + 1 < len(starts) else w1
        out.append((t, min(nxt, t + smax), sc))
    # auto scenes for gaps longer than scene_max_seconds
    gaps, cur = [], w0
    for s, e, _ in out:
        gaps.append((cur, s))
        cur = e
    gaps.append((cur, w1))
    autos: list[tuple[float, float, dict]] = []
    auto_n = 0
    for a, b in gaps:
        g = b - a
        if g <= smax:
            continue
        cnt = max(1, int(round(g / sdef)), int(math.ceil(g / smax)))
        while cnt > 1 and g / cnt < smin:
            cnt -= 1
        for q in range(cnt):
            kind = "cve" if (auto_n % 2 == 1 and _has_card(card)) else "headline"
            auto_n += 1
            if kind == "cve":
                sc = {"type": "cve", "card": card}
            else:
                sc = {"type": "headline", "title": str(part.get("headline") or part.get("title") or ""),
                      "source": str(part.get("source") or "")}
            autos.append((a + g * q / cnt, a + g * (q + 1) / cnt, sc))
    return sorted(out + autos, key=lambda x: x[0])


def _schedule_points(cfg, vis, times, tr_end, pe, card):
    if not _g(cfg, "key_points", True):
        return []
    kps = vis.get("key_points") if isinstance(vis.get("key_points"), list) else []
    pts = [(i, str(k.get("text") or "").strip()) for i, k in enumerate(kps) if isinstance(k, dict)]
    pts = [(i, t) for i, t in pts if t]
    if not pts:
        return []
    limit = max(1, int(_num(_g(cfg, "max_key_points", 4), 4)))
    if _has_card(card):
        limit = min(limit, 3)
    raw = _tlist(times, "points", len(kps))
    a, b = tr_end + 1.0, tr_end + 0.55 * (pe - tr_end)
    timed = []
    for q, (i, text) in enumerate(pts):
        t = raw[i]
        if t is None:
            t = a + (b - a) * q / len(pts)
        timed.append((_clamp(t, tr_end + 0.3, pe), text))
    timed.sort(key=lambda x: x[0])
    out: list[tuple[float, str]] = []
    for t, text in timed:
        if out:
            t = max(t, out[-1][0] + MIN_POINT_GAP)
        if t > pe - 1.5 or len(out) >= limit:
            continue
        out.append((t, text))
    return out


def _schedule_checklist(cfg, vis, times, tr_end, pe, points):
    if not _g(cfg, "checklist", True):
        return None
    cl = vis.get("checklist") if isinstance(vis.get("checklist"), list) else []
    n = len([c for c in cl if isinstance(c, dict) and str(c.get("text") or "").strip()])
    if not n:
        return None
    raw_all = _tlist(times, "checklist", len(cl))
    raw = [raw_all[i] for i, c in enumerate(cl) if isinstance(c, dict) and str(c.get("text") or "").strip()]
    floor = points[-1][0] + CHECKLIST_GAP if points else tr_end + 1.0
    known = [t for t in raw if t is not None]
    ideal = (min(known) - 1.0) if known else tr_end + 0.7 * (pe - tr_end)
    start = max(floor, min(ideal, pe - CHECKLIST_MIN))
    if pe - start < CHECKLIST_MIN:
        return None
    ticks: list[float] = []
    for i, t in enumerate(raw):
        if t is None:
            t = start + 1.0 + (pe - 1.0 - start - 1.0) * i / max(1, n)
        t = _clamp(t, start + 0.5, pe - 0.5)
        if ticks:
            t = max(t, ticks[-1])
        ticks.append(min(t, pe - 0.3))
    return (start, ticks)


# ---------------------------------------------------------------- captions
def _chunk_idx(words, max_words: int, max_chars: int, force_break: int | None = None) -> list[tuple[int, int]]:
    """Index ranges (i0, i1) of caption chunks: <= max_words, <= max_chars, break after . ! ? ,
    ``force_break``: a word index that always starts a new chunk (the first word after a transition card)."""
    max_words, max_chars = max(1, int(max_words)), max(8, int(max_chars))
    out, i0, chars = [], 0, 0
    for i, w in enumerate(words):
        wl = len(str(w[2]).strip())
        if i > i0 and (i - i0 >= max_words or chars + 1 + wl > max_chars or i == force_break):
            out.append((i0, i))
            i0, chars = i, 0
        chars += wl + (1 if i > i0 else 0)
        if str(w[2]).rstrip().endswith(C._BREAK_CHARS):
            out.append((i0, i + 1))
            i0, chars = i + 1, 0
    if i0 < len(words):
        out.append((i0, len(words)))
    return out


def fit_chunks(words, max_words: int, max_chars: int) -> list[tuple[float, float, str]]:
    """Caption chunks that respect both a word and a character limit (words are never split)."""
    return [(words[a][0], words[b - 1][1], " ".join(str(w[2]).strip() for w in words[a:b]))
            for a, b in _chunk_idx(words, max_words, max_chars)]


_CAP_RE = re.compile(r"^Dialogue: 1,([^,]*),([^,]*),Caption,")


def _caption_lines(cfg, words, chunk_ix, pe, supp, kws, th, cap_size, cx=CAP_CX) -> list[str]:
    kept_chunks, kept_words = [], []
    for a, b in chunk_ix:
        cw = words[a:b]
        if any(s <= cw[0][0] < e for s, e in supp):
            continue
        kept_chunks.append((cw[0][0], cw[-1][1], " ".join(str(w[2]).strip() for w in cw)))
        kept_words.extend((w[0], w[1], str(w[2]).strip()) for w in cw)
    if not kept_chunks:
        return []
    accent = C.ass_color(_cfgv(cfg, "branding.accent", "#3DDCFF"), "#3DDCFF")
    kw = C.ass_color(_cfgv(cfg, "branding.keyword_color", "#FFD60A"), "#FFD60A")
    pos = (f"\\an5\\pos({cx},{CAP_CY})\\fn{th.font}\\fs{cap_size}\\bord0\\shad0")
    evs = C._karaoke_events(kept_chunks, kept_words, accent, kw, kws, pos, pe)
    if evs is None:
        evs = C._fit_events([(a, b, f"{{{pos}}}{C._plain_text(t)}") for a, b, t in kept_chunks], pe)
    out = []
    for line in evs:
        m = _CAP_RE.match(line)
        if not m:
            continue
        line = _CAP_RE.sub(lambda mm: f"Dialogue: 30,{mm.group(1)},{mm.group(2)},Default,", line, count=1)
        out.append(line.rstrip("\n"))
    return out


def _cfgv(cfg, key, default):
    try:
        v = cfg.get(key, default)
    except Exception:  # noqa: BLE001
        v = default
    return default if v in (None, "") else v


# ---------------------------------------------------------------- chrome
def _header(font: str) -> str:
    return (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {W}\nPlayResY: {H}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font},40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")


# libass \fs is the full line height (ascent + descent = 1.164 em for DejaVu), so rendered widths are about
# 0.86 x the font's own advance widths: measured by rendering, bold caps average 0.60 x \fs (caps weight 1.0).
def _top_layout(cfg, week_label: str, handle: str, has_logo: bool) -> dict:
    title = str(_r(cfg, "show_title", "THIS WEEK IN CYBER") or "").strip().upper()
    tl, tsz = S.fit_text(title, 560, 40, min_size=26, factor=0.60, caps=1.0) if title else ([], 40)
    tw = S.est_w(tl[0], tsz, 0.60, 1.0) if tl else 0
    x = 60 + tw + 55
    wk = str(week_label or "").strip().upper()
    pill = None
    if wk:
        wl, wsz = S.fit_text(wk, 300, 28, min_size=18, factor=0.64, caps=1.0)
        pw = max(110.0, S.est_w(wl[0], wsz, 0.64, 1.0) + 44)
        pill = (x, pw, wl[0], wsz)
        x += pw + 30
    hx = W - 60 - (84 if has_logo else 0)
    hl = ""
    hw = 0.0
    hsz = 28
    if str(handle or "").strip():
        hls, hsz = S.fit_text(str(handle).strip(), 380, 28, min_size=18, factor=0.6)
        hl, hw = hls[0], S.est_w(hls[0], hsz, 0.6)
    return {"title": tl[0] if tl else "", "tsz": tsz, "pill": pill, "chap_x": x, "handle": hl, "hx": hx,
            "hsz": hsz, "chap_right": hx - hw - 40 if hl else hx}


def _tick_x(t: float, duration: float) -> float:
    x0, _, w, _ = TRACK
    return x0 + w * _clamp(t / max(duration, 0.1), 0.0, 1.0)


def _story_starts(parts, duration) -> list[float]:
    return [float(p.get("start", 0)) for p in parts or []
            if p.get("kind") == "story" and 0 <= float(p.get("start", 0)) < duration]


def build_chrome_ass(cfg, parts: list[dict], duration: float, *, week_label: str, handle: str,
                     has_logo: bool) -> str:
    """Static chrome (top bar, panel frame/fill, caption box, progress track + ticks) for the background PNG."""
    th = S.theme(cfg)
    dur = max(float(duration), 0.1)
    lay = _top_layout(cfg, week_label, handle, has_logo)
    E = 10.0
    L: list = []
    if lay["title"]:
        L.append(S.tx(60, 52, lay["title"], lay["tsz"], th.white, 0, E, font=th.font, layer=4))
    if lay["pill"]:
        px, pw, label, wsz = lay["pill"]
        L.append(S.rr(px, 50, pw, 48, 24, th.accent, 0, E, layer=3))
        L.append(S.tx(px + pw / 2, 74, label, wsz, th.ink, 0, E, an=5, layer=4, font=th.font))
    if lay["handle"]:
        L.append(S.tx(lay["hx"], 60, lay["handle"], lay["hsz"], th.dim, 0, E, bold=0, an=9, layer=4, font=th.font))
    # left panel frame: faint fill + accent border
    L.append(S.rr(60, 150, 1160, 653, 12, th.panel, 0, E, alpha="&H50&", layer=1))
    L.append(S.rr(58, 148, 1164, 657, 12, th.accent, 0, E, alpha="&HFF&", layer=1,
                  extra=f"\\bord3\\3c{th.accent}\\3a&H00&"))
    # right panel
    L.append(S.rr(PX, PY, PW, PH, 18, th.panel, 0, E, alpha="&H10&", layer=1))
    # caption box
    cx, cy, cw, ch = CAP
    L.append(S.rr(cx, cy, cw, ch, 18, "&H000000&", 0, E, alpha="&H50&", layer=2))
    # progress track + ticks
    tx0, ty, tw, tth = TRACK
    L.append(S.rr(tx0, ty, tw, tth, 4, th.bar, 0, E, layer=2))
    for st in _story_starts(parts, dur):
        L.append(S.rr(_tick_x(st, dur) - 3, ty - 8, 6, 24, 3, th.dim, 0, E, layer=3))
    return _header(th.font) + "\n".join(x for x in L if x) + "\n"


# ---------------------------------------------------------------- dynamic assembly
def _domain(url: str) -> str:
    try:
        host = urlparse(str(url or "")).netloc.lower()
    except ValueError:
        return ""
    return re.sub(r"^(www|m|amp)\.", "", host.split(":")[0])


def _story_info(stories, idx: int, part: dict) -> tuple[str, str, str]:
    s = stories[idx - 1] if isinstance(stories, list) and 1 <= idx <= len(stories) and \
        isinstance(stories[idx - 1], dict) else {}
    title = " ".join(str(s.get("title") or part.get("title") or "").split())
    source = " ".join(str(s.get("source") or s.get("story_source") or "").split()) or _domain(s.get("url"))
    tagline = " · ".join(x for x in (str(s.get("series") or "").strip(), source) if x)
    return title, source, tagline


def _story_source(story, card) -> str:
    """Offline version of ``recap.source_text`` (never fetches): the story material plus the confirmed card."""
    s = story if isinstance(story, dict) else {}
    bits = [s.get("title"), s.get("summary"), s.get("script"), s.get("description"), s.get("article"),
            V._card_text(card)]
    return "\n".join(str(b) for b in bits if b)


def _chapter(p: dict, idx: int, stories, cards) -> str:
    """Chapter title shown on screen. The LLM's chapter must pass the fact check and the host/IOC check against
    the story material; otherwise the story's own title is shown."""
    story = stories[idx - 1] if isinstance(stories, list) and 1 <= idx <= len(stories) else None
    card = cards[idx - 1] if isinstance(cards, list) and 1 <= idx <= len(cards) else None
    title = _story_info(stories, idx, p)[0] or f"Story {idx}"
    chapter = V._clean_str(str(p.get("title") or ""))
    if not chapter or V.host_ioc(chapter) or not V.facts_ok(chapter, _story_source(story, card))[0]:
        return title
    return chapter


_WJ = "\u2060"


def _safe_word(text: str) -> str:
    """Caption word: no bidi/control characters; a backslash gets a word joiner so it can never combine with the
    next character into an ASS escape (``captions._escape`` doubles it, which libass does not treat as an escape)."""
    t = V._clean_str(str(text)).replace("\\", "\\" + _WJ)
    return t


def build_newsroom_ass(cfg, words: list, chunks: list, parts: list[dict], duration: float, *, stories: list,
                       visuals: list, cards: list, script: str, para_counts: list, week_label: str,
                       handle: str) -> str:
    th = S.theme(cfg)
    dur = max(float(duration), 0.1)
    raw_words = [(float(w[0]), float(w[1]), str(w[2])) for w in (words or [])]
    ranges = V.story_word_ranges(raw_words, script, para_counts) if raw_words else None   # on the script's words
    words = [(a, b, _safe_word(t)) for a, b, t in raw_words]
    lay = _top_layout(cfg, week_label, handle, True)          # chapter width assumes the logo is present
    cap_size = int(_num(_g(cfg, "caption_font_size", 44), 44))
    max_chars = int(_num(_g(cfg, "caption_max_chars", 33), 33))
    max_chars = min(max_chars, max(12, int(CAP_INNER_W / (cap_size * CAP_FACTOR))))
    max_words = max([len(str(c[2]).split()) for c in chunks or []] or [int(_num(_r(cfg, "words_per_chunk", 6), 6))])
    lines: list[str] = []

    def add(x):
        if x:
            lines.append(x)

    def addall(xs):
        for x in xs or []:
            add(x)

    n_stories = len([p for p in parts if p.get("kind") == "story"])
    card_windows: list[tuple[float, float]] = []

    for k, p in enumerate(parts or []):
        ps = _clamp(float(p.get("start", 0)), 0.0, dur)
        pe = _clamp(float(p.get("end", dur)), 0.0, dur)
        if pe - ps < 0.05:
            continue
        kind = p.get("kind")
        supp: list[tuple[float, float]] = []
        kws: set[str] = set()
        cap_cx = CAP_CX
        if kind == "intro":
            titles = [_chapter(q, int(q.get("index") or 0) or j, stories, cards)
                      for j, q in enumerate([q for q in parts if q.get("kind") == "story"], 1)]
            addall(S.intro_card(str(_r(cfg, "show_title", "THIS WEEK IN CYBER")), week_label, titles, ps, pe, W, H,
                                th))
            card_windows.append((ps, pe))
            cap_cx = W // 2
            add(S.rr(cap_cx - CAP[2] // 2, CAP[1], CAP[2], CAP[3], 18, "&H000000&", ps, pe, alpha="&H50&", layer=29))
        elif kind == "outro":
            addall(S.outro_card(handle, ps, pe, W, H, th))
            card_windows.append((ps, pe))
            cap_cx = W // 2
            add(S.rr(cap_cx - CAP[2] // 2, CAP[1], CAP[2], CAP[3], 18, "&H000000&", ps, pe, alpha="&H50&", layer=29))
        elif kind == "story":
            idx = int(p.get("index") or 0) or (k)
            supp, kws = _story(cfg, p, idx, ps, pe, stories, visuals, cards, words, ranges, k, n_stories or
                               int(p.get("total") or 0), th, lay, lines)
            card_windows.append(supp[0])
        # captions
        a = next((i for i, w in enumerate(words) if w[0] >= ps), len(words))
        b = next((i for i, w in enumerate(words) if w[0] >= pe), len(words)) if pe < dur else len(words)
        if b > a:
            pw_ = words[a:b]
            force = None
            if supp:    # the first word after the transition card always starts a chunk (never dropped whole)
                force = next((i for i, w in enumerate(pw_) if w[0] >= supp[0][1]), None)
            cix = [(x + a, y + a) for x, y in _chunk_idx(pw_, max_words, max_chars, force)]
            addall(_caption_lines(cfg, words, cix, pe, supp, kws, th, cap_size, cap_cx))

    _progress(lines, parts, dur, card_windows, th)
    if _g(cfg, "scanline", True):
        t = 4.0
        while t < dur - 0.5:
            add(S.ev(1, t, min(t + 1.8, dur),
                     "{\\an7\\move(0,-2,0,1080,0,1800)\\bord0\\shad0\\1c&HFFFFFF&\\1a&HF2&\\p1}"
                     f"m 0 0 l {W} 0 {W} 2 0 2{{\\p0}}"))
            t += 8.0
    return _header(th.font) + "\n".join(lines) + "\n"


def _story(cfg, p, idx, ps, pe, stories, visuals, cards, words, ranges, k, total, th, lay, lines):
    """Events of one story part appended to ``lines``. -> (transition window (s, e) as list, keyword set)."""
    add = lines.append
    title, source, tagline = _story_info(stories, idx, p)
    vis = visuals[idx - 1] if isinstance(visuals, list) and 1 <= idx <= len(visuals) and \
        isinstance(visuals[idx - 1], dict) else {}
    card = cards[idx - 1] if isinstance(cards, list) and 1 <= idx <= len(cards) and \
        isinstance(cards[idx - 1], dict) else None
    card = card if _has_card(card) or (card and (card.get("cvss") or card.get("exploited"))) else None
    ranges_k = ranges[k] if ranges and k < len(ranges) else None
    part = dict(p, headline=title, source=source, card=card)

    def tm(items):
        out = []
        for it in items:
            t = None
            if ranges_k and isinstance(it, dict):
                t = V.cue_time(str(it.get("cue") or ""), it.get("para"), words, ranges_k)
            out.append(t)
        return out

    sc_all = vis.get("scenes") if isinstance(vis.get("scenes"), list) else []
    times = {"scenes": tm(sc_all), "points": tm(vis.get("key_points") or []),
             "checklist": tm(vis.get("checklist") or [])} if ranges_k else None
    sch = schedule(cfg, part, vis, times)
    ts, te = sch["transition"]
    kws = V.keyword_set(vis.get("keywords"))
    n = int(p.get("index") or idx)
    total = int(total or p.get("total") or 0) or n

    # transition card at story start
    for x in S.transition(n, total, title, tagline, ts, te, W, H, th):
        add(x)
    # top bar chapter text
    # counter and title are separate events so the spacing survives fit_text
    counter = f"{n} / {total}"
    cw = S.est_w(counter, 30, 0.5, 1.0)
    add(S.tx(lay["chap_x"], 60, counter, 30, th.grey, te, pe, bold=0, layer=4, font=th.font))
    tx0 = lay["chap_x"] + cw + 24
    cl, csz = S.fit_text(_chapter(p, idx, stories, cards), max(120.0, lay["chap_right"] - tx0), 30, min_size=20,
                         factor=0.58, caps=1.0)
    if cl:
        add(S.tx(tx0, 60, cl[0], csz, th.grey, te, pe, bold=0, layer=4, font=th.font))
    # chip
    add(S.rr(84, 174, 236, 50, 10, th.accent, te, pe, layer=4))
    add(S.tx(202, 199, f"STORY {n} OF {total}", 26, th.ink, te, pe, an=5, layer=5, font=th.font))
    # left panel scenes
    for s, e, sc in sch["scenes"]:
        try:
            fn = S.BUILDERS[sc["type"]]
            for x in fn(sc, BOX, s, e, th):
                add(x)
        except Exception as ex:  # noqa: BLE001
            log.warning("recap scene %s failed: %s", sc.get("type"), type(ex).__name__)
    # right panel
    cl_start = sch["checklist"][0] if sch["checklist"] else pe
    pend = min(pe, cl_start)
    if pend - te > 0.05:
        _right_info(add, cfg, th, title, source, card, sch["points"], te, pend)
    if sch["checklist"]:
        cs, ticks = sch["checklist"]
        items = [c for c in (vis.get("checklist") or []) if isinstance(c, dict) and str(c.get("text") or "").strip()]
        ls, _h = S.checklist(items, ticks, PX, PY, PW, cs, pe, th)
        for x in ls:
            add(x)
        add(S.tx(PX + 32, PY + PH - 60, "Full sources in the description", 22, th.dim, cs, pe, bold=0, layer=4,
                 font=th.font))
    return [(ts, te)], kws


def _right_info(add, cfg, th, title, source, card, points, s, e):
    fade = "\\fad(250,0)"
    if source:
        sl, ssz = S.fit_text("SOURCE  ·  " + source.upper(), PW - 64, 22, min_size=14, factor=0.66)
        add(S.tx(PX + 32, PY + 30, sl[0], ssz, th.dim, s, e, layer=4, font=th.font, extra=fade))
    hl, hsz = S.fit_text(title, PW - 64, 38, min_size=26, max_lines=2)
    if hl:
        add(S.tx(PX + 32, PY + 70, S._join(hl), hsz, th.white, s + 0.1, e, layer=4, font=th.font, extra=fade))
    y = PY + 70 + S._text_h(len(hl), hsz) + 26
    if card:
        cls, ch = S.cve_card(card, PX, y, PW, s + 0.2, e, th)
        for x in cls:
            add(x)
        if ch:
            y += ch + 36
    if points:
        if y + 44 + 76 > PY + PH - 20:           # no room even for one point
            return
        add(S.tx(PX + 32, y, "KEY POINTS", 22, th.accent, points[0][0], e, layer=4, font=th.font, extra=fade))
        y += 44
        for t, text in points:
            ls, h = S.key_point(text, PX, y, PW, t, e, th)
            if not h or y + h > PY + PH - 20:
                break
            for x in ls:
                add(x)
            y += h + 16


def _progress(lines, parts, dur, card_windows, th):
    x0, y, w, h = TRACK
    starts = _story_starts(parts, dur)
    a = lines.append
    # track + dim ticks redrawn above full-screen cards (the baked ones are covered)
    for s, e in card_windows:
        a(S.rr(x0, y, w, h, 4, th.bar, s, e, layer=24))
        for st in starts:
            a(S.rr(_tick_x(st, dur) - 3, y - 8, 6, 24, 3, th.dim, s, e, layer=24))
    a(S.rr(x0, y, w, h, 4, th.accent, 0, dur, layer=25, extra=S._clip_grow(x0, y - 1, x0 + w, y + h + 1, dur)))
    for st in starts:
        a(S.rr(_tick_x(st, dur) - 3, y - 8, 6, 24, 3, th.white, st, dur, layer=26))
    lines[:] = [x for x in lines if x]


def write_newsroom_ass(cfg, words: list, chunks: list, parts: list[dict], duration: float, out_path, *,
                       stories: list, visuals: list, cards: list, script: str, para_counts: list,
                       week_label: str, handle: str) -> Path:
    out = Path(out_path)
    out.write_text(build_newsroom_ass(cfg, words, chunks, parts, duration, stories=stories, visuals=visuals,
                                      cards=cards, script=script, para_counts=para_counts,
                                      week_label=week_label, handle=handle), encoding="utf-8")
    return out
