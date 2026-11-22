"""Newsroom recap scene builders: pure functions that return ASS ``Dialogue:`` lines.

Everything is positioned in absolute PlayRes pixels (1920x1080 design). Geometry, colours and the drawing
approach come from design/recap-v2/mock.py + mock2.py. No I/O, no ffmpeg: callers write the lines into an
.ass file whose Default style is a plain left/top style (every event carries its own \\an/\\pos).

Scene builders have the signature ``(sc, box, s, e, th) -> list[str]`` and keep every event inside [s, e] and
inside ``box``. All user text goes through the recap-local ``_esc`` (not ``captions._escape``: a doubled backslash does not
escape in libass, so ``\\N`` in a title would still break the line) and is fitted with ``fit_text``.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .captions import _card_rows, _rrect, _sev_color, ass_color, format_ass_time

__all__ = ["Theme", "theme", "Box", "ev", "rr", "tx", "fit_text", "BUILDERS", "terminal", "flow", "stat",
           "timeline", "scam", "headline", "cve", "cve_card", "key_point", "checklist", "transition",
           "intro_card", "outro_card"]

PANEL_BOX = (60, 150, 1160, 653)
CAPTION_TOP = 860           # top of the caption box (design pixels)
BAR = "█" * 6          # redacted-link bar (rendered in the "redacted" colour)
LINE_H = 1.17               # libass line height / \fs for DejaVu


# ---------------------------------------------------------------- theme / geometry
def _c(hex_color: str, default: str) -> str:
    """'#RRGGBB' -> '&HBBGGRR&' (mock style)."""
    return "&H" + ass_color(hex_color, default)[4:] + "&"


@dataclass(frozen=True)
class Theme:
    accent: str = "&HFFDC3D&"
    kw: str = "&H0AD6FF&"
    panel: str = "&H26160F&"
    card: str = "&H3A2516&"
    bar: str = "&H5A4436&"       # unfilled bars / neutral borders
    dim: str = "&H8A7F74&"
    grey: str = "&HB8AFA6&"
    white: str = "&HFFFFFF&"
    ink: str = "&H140B07&"
    red: str = "&H303BFF&"
    green: str = "&H58D130&"
    term_green: str = "&H7AE05A&"
    term_bg: str = "&H0A0604&"
    font: str = "DejaVu Sans"
    mono: str = "DejaVu Sans Mono"


def theme(cfg=None) -> Theme:
    def g(key, default):
        try:
            v = cfg.get(key, default) if cfg is not None else default
        except Exception:
            v = default
        return default if v in (None, "") else v

    return Theme(
        accent=_c(g("branding.accent", "#3DDCFF"), "#3DDCFF"),
        kw=_c(g("branding.keyword_color", "#FFD60A"), "#FFD60A"),
        panel=_c(g("recap.newsroom.panel_color", "#0F1626"), "#0F1626"),
        font=str(g("captions.font", "DejaVu Sans")),
    )


@dataclass(frozen=True)
class Box:
    x: float
    y: float
    w: float
    h: float

    @property
    def x2(self) -> float:
        return self.x + self.w

    @property
    def y2(self) -> float:
        return self.y + self.h

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    def inner(self) -> tuple[float, float, float, float]:
        """Content area below the story chip: (x, y, w, h)."""
        return self.x + 40, self.y + 92, self.w - 80, self.h - 92 - 28


# ---------------------------------------------------------------- primitives
def ev(layer: int, s: float, e: float, text: str, *, lo: float | None = None, hi: float | None = None) -> str | None:
    """One Dialogue line. Times are clamped to [lo, hi]; empty text or <10 ms duration -> None."""
    if lo is not None:
        s, e = max(s, lo), max(e, lo)
    if hi is not None:
        s, e = min(s, hi), min(e, hi)
    if not text or not str(text).strip() or e - s < 0.01:
        return None
    return f"Dialogue: {int(layer)},{format_ass_time(s)},{format_ass_time(e)},Default,,0,0,0,,{text}"


def rr(x, y, w, h, r, col, s, e, *, alpha="&H00&", layer=2, extra="", center=False, lo=None, hi=None):
    """Rounded rectangle with its top-left at (x, y) (or centred there-about for pop animations)."""
    w, h = max(2, int(round(w))), max(2, int(round(h)))
    if center:
        pos, an = f"{x + w / 2:.0f},{y + h / 2:.0f}", 5
    else:
        pos, an = f"{x:.0f},{y:.0f}", 7
    body = f"{{\\an{an}\\pos({pos})\\bord0\\shad0\\1c{col}\\1a{alpha}{extra}\\p1}}{_rrect(w, h, r)}{{\\p0}}"
    return ev(layer, s, e, body, lo=lo, hi=hi)


_WJ = "\u2060"          # word joiner: keeps a literal backslash from forming "\N", "\n", "\h" or a tag
_BIDI_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\u00ad\u061c\u180e]")
_CTRL_RE = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f\u2028\u2029]")


class _Lines(str):
    """Text whose newlines are intentional line breaks (built by ``_join``)."""


def _esc(text) -> str:
    """Safe ASS text: no override blocks, no ``\\N``-style sequences, no bidi/control characters. Newlines
    become spaces unless the text came from ``_join`` (intentional line breaks)."""
    s = _CTRL_RE.sub(" ", _BIDI_RE.sub("", str(text))).replace("\r", "")
    s = s.replace("\\", "\\" + _WJ).replace("{", "(").replace("}", ")")
    return s.replace("\n", "\\N" if isinstance(text, _Lines) else " ")


def tx(x, y, text, size, col, s, e, *, bold=1, an=7, layer=4, extra="", font="DejaVu Sans", raw=False,
       lo=None, hi=None):
    """Text event. ``text`` is escaped unless ``raw`` (caller supplies safe override tags)."""
    body = text if raw else _esc(text)
    if not str(body).strip():
        return None
    return ev(layer, s, e, f"{{\\an{an}\\pos({x:.0f},{y:.0f})\\fn{font}\\fs{int(size)}\\b{int(bold)}"
                           f"\\1c{col}\\bord0\\shad0{extra}}}{body}", lo=lo, hi=hi)


def _clean(lines) -> list[str]:
    return [l for l in lines if l]


# ---------------------------------------------------------------- text fitting
_NARROW = set("il.,:;'!|jtfI ")


def est_w(text: str, size: float, factor: float = 0.60, caps: float = 1.15) -> float:
    n = 0.0
    for ch in text:
        if ch == "█":
            n += 1.25
        elif ch.isupper():
            n += caps
        elif ch in _NARROW:
            n += 0.6
        else:
            n += 1.0
    return n * size * factor


def _wrap(text: str, max_w: float, size: float, factor: float, caps: float,
          split_words: bool = True) -> tuple[list[str], bool]:
    """Greedy wrap -> (lines, hard_broke): hard_broke when a single word had to be split mid-word (or, with
    ``split_words=False``, cut to an ellipsis)."""
    lines: list[str] = []
    broke = False
    cur = ""
    for word in text.split(" "):
        if not split_words and word and est_w(word, size, factor, caps) > max_w:
            k = len(word) - 1
            while k > 1 and est_w(word[:k] + "…", size, factor, caps) > max_w:
                k -= 1
            word = word[:k] + "…"
            broke = True
        while word and est_w(word, size, factor, caps) > max_w:       # hard-break an over-long word
            k = len(word) - 1
            while k > 1 and est_w(word[:k], size, factor, caps) > max_w:
                k -= 1
            broke = True
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(word[:k])
            word = word[k:]
        if not word:
            continue
        trial = f"{cur} {word}" if cur else word
        if cur and est_w(trial, size, factor, caps) > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines, broke


def fit_text(text, max_w, size, *, min_size=None, max_lines=1, factor=0.60, caps=1.15, split_words=True):
    """Wrap, then shrink (down to ``min_size``), then ellipsise the last line. -> (lines, size).
    ``split_words=False``: a single word wider than ``max_w`` is shortened with an ellipsis, never split."""
    text = " ".join(_BIDI_RE.sub("", str(text or "")).split())
    size = int(size)
    min_size = int(min_size if min_size is not None else size)
    if not text:
        return [], size
    sz = size
    while True:
        lines, broke = _wrap(text, max_w, sz, factor, caps, split_words)
        if len(lines) <= max_lines and not (broke and sz > min_size):
            return lines, sz
        if sz <= min_size:
            break
        sz = max(min_size, sz - 2)
    lines = lines[:max_lines]
    last = lines[-1]
    if last.endswith("…") and est_w(last, sz, factor, caps) <= max_w:
        return lines, sz
    while last and est_w(last + "…", sz, factor, caps) > max_w:
        last = last[:-1]
    lines[-1] = last.rstrip(" ,.;:") + "…"
    return lines, sz


def _text_h(n_lines: int, size: float) -> float:
    return n_lines * size * LINE_H


def _join(lines) -> str:
    return _Lines("\n".join(lines))


def _span(s: float, e: float, frac: float, lead: float = 0.3) -> tuple[float, float]:
    """Reveal window inside [s, e]: starts shortly after s, ends at ``frac`` of the scene."""
    d = max(0.0, e - s)
    a = s + min(lead, 0.1 * d)
    return a, max(a, s + frac * d)


def _ms(sec: float) -> int:
    return max(1, int(round(sec * 1000)))


def _ease(x: float) -> float:
    return 1 - (1 - x) ** 3


def _clip_grow(x0, y0, x1, y1, dur: float, vertical=False) -> str:
    """Tag string: rectangular \\clip that grows left->right (or top->bottom) over ``dur`` seconds."""
    x0, y0, x1, y1 = (int(round(v)) for v in (x0, y0, x1, y1))
    start = f"\\clip({x0},{y0},{x0 + 1},{y1})" if not vertical else f"\\clip({x0},{y0},{x1},{y0 + 1})"
    return f"{start}\\t(0,{_ms(dur)},\\clip({x0},{y0},{x1},{y1}))"


def _pop(dur=0.25, start=70) -> str:
    return f"\\fscx{start}\\fscy{start}\\t(0,{_ms(dur)},\\fscx100\\fscy100)\\fad(120,0)"


# ---------------------------------------------------------------- terminal
def terminal(sc, box: Box, s: float, e: float, th: Theme) -> list[str]:
    ix, iy, iw, ih = box.inner()
    L = lambda f, *a, **k: f(*a, lo=s, hi=e, **k)  # noqa: E731
    out = [L(rr, ix, iy, iw, ih, 12, th.term_bg, s, e, layer=3, extra="\\fad(200,0)")]
    for i, c in enumerate(("&H5F57FF&", "&H2EBDFE&", "&H40C828&")):
        out.append(L(rr, ix + 24 + i * 30, iy + 18, 16, 16, 8, c, s, e, layer=4))
    raw_title = re.sub(r"^\s*dramati[sz]ation\s*[·:\-]*\s*", "", str(sc.get("title") or ""), flags=re.I).strip(" ·")
    ttl, tsz = fit_text("DRAMATIZATION · " + raw_title if raw_title else "DRAMATIZATION", iw - 240, 26,
                        min_size=24, factor=0.64)
    out.append(L(tx, ix + iw / 2, iy + 26, _join(ttl), tsz, th.grey, s, e, bold=1, an=5, layer=4, font=th.font))
    msz, row_h = 32, 54
    rows = int((ih - 62 - 20) // row_h)
    lines = [l for l in (sc.get("lines") or []) if isinstance(l, dict) and str(l.get("text") or "").strip()]
    lines = lines[:max(1, rows - 1)]
    n = len(lines)
    r0, r1 = _span(s, e, 0.7)
    slot = (r1 - r0) / n if n else 0
    x0, y0 = ix + 24, iy + 62
    for i, ln in enumerate(lines):
        kind = str(ln.get("kind") or "out")
        txt = ("$ " if kind == "cmd" else "\u00a0\u00a0") + str(ln["text"]).strip()
        col = th.term_green if kind == "cmd" else th.red if kind == "alert" else th.grey
        fl, fs_ = fit_text(txt, iw - 48, msz, min_size=20, factor=0.602, caps=1.0)
        t0 = r0 + i * slot
        dur = max(0.05, slot * (0.8 if kind == "cmd" else 0.3))
        y = y0 + i * row_h
        clip = _clip_grow(ix, y - 4, ix + iw, y + row_h - 8, dur)
        out.append(L(tx, x0, y, fl[0], fs_, col, t0, e, bold=0, layer=5, font=th.mono, extra=clip))
    # prompt row with blinking cursor
    y = y0 + n * row_h
    cyc = min(20, int(math.ceil(max(0.0, e - r1) / 0.8)))
    blink = "".join(f"\\t({_ms(0.4 + 0.8 * k)},{_ms(0.4 + 0.8 * k) + 1},\\alpha&HFF&)"
                    f"\\t({_ms(0.8 + 0.8 * k)},{_ms(0.8 + 0.8 * k) + 1},\\alpha&H00&)" for k in range(cyc))
    out.append(L(tx, x0, y, f"$ {{\\alpha&H00&{blink}}}█", msz, th.term_green, r1, e, bold=0, layer=5,
                 font=th.mono, raw=True))
    return _clean(out)


# ---------------------------------------------------------------- flow
def flow(sc, box: Box, s: float, e: float, th: Theme) -> list[str]:
    ix, iy, iw, ih = box.inner()
    L = lambda f, *a, **k: f(*a, lo=s, hi=e, **k)  # noqa: E731
    nodes = [n for n in (sc.get("nodes") or []) if isinstance(n, dict) and str(n.get("label") or "").strip()][:4]
    if len(nodes) < 2:
        return []
    n = len(nodes)
    arrows = list(sc.get("arrows") or [])
    fixed = re.sub(r"^\s*fixed\s+in\s+", "", str(sc.get("fixed_in") or ""), flags=re.I).strip()
    out = []
    ttl, tsz = fit_text(str(sc.get("title") or "").upper(), iw, 30, min_size=18, factor=0.66)
    out.append(L(tx, box.cx, iy - 4, _join(ttl), tsz, th.accent, s, e, an=8, layer=4, font=th.font,
                 extra="\\fad(200,0)"))
    nw = {2: 300, 3: 240, 4: 190}[n]
    gap = min(260.0, (iw - n * nw) / (n - 1))
    total = n * nw + (n - 1) * gap
    left = ix + (iw - total) / 2
    cy = iy + ih * 0.42
    steps = n + (n - 1) + (1 if fixed else 0)
    r0, r1 = _span(s, e, 0.75, 0.3)
    dt = (r1 - r0) / steps
    k = 0
    for i, nd in enumerate(nodes):
        cx = left + i * (nw + gap) + nw / 2
        t0 = r0 + k * dt
        k += 1
        col = th.red if i == 0 else th.bar if i == n - 1 and n > 2 else th.accent
        pop = _pop()
        out.append(L(rr, cx - nw / 2, cy - 70, nw, 140, 16, th.card, t0, e, layer=4, center=True,
                     extra=f"\\bord3\\3c{col}\\3a&H00&{pop}"))
        lab, lsz = fit_text(str(nd["label"]).strip(), nw - 24, 32, min_size=16, max_lines=2, factor=0.66,
                            split_words=False)
        sub, ssz = fit_text(str(nd.get("sub") or "").strip(), nw - 24, 22, min_size=15, max_lines=2, factor=0.60,
                            split_words=False)
        ly = cy - 20 if sub else cy
        out.append(L(tx, cx, ly - (lsz * LINE_H / 2 * (len(lab) - 1)), _join(lab), lsz, th.white, t0, e, an=5,
                     layer=5, font=th.font, extra=_pop()))
        if sub:
            out.append(L(tx, cx, cy + 32 + (8 if len(lab) > 1 else 0), _join(sub), ssz, th.grey, t0, e, bold=0, an=5, layer=5, font=th.font, extra=_pop()))
        if i < n - 1:
            t1 = r0 + k * dt
            k += 1
            ax1 = cx + nw / 2 + 8
            ax2 = cx + nw / 2 + gap - 8
            al = str(arrows[i]).strip() if i < len(arrows) else ""
            if ax2 - ax1 > 50:
                wid = ax2 - ax1
                shaft = f"m 0 11 l {wid - 20:.0f} 11 l {wid - 20:.0f} 17 l 0 17 m {wid - 22:.0f} 0 l {wid:.0f} 14 l {wid - 22:.0f} 28"
                grow = _clip_grow(ax1, cy - 16, ax2 + 1, cy + 16, min(0.6, dt * 0.9))
                out.append(L(ev, 4, t1, e, f"{{\\an7\\pos({ax1:.0f},{cy - 14:.0f})\\bord0\\shad0\\1c{th.red}"
                                           f"{grow}\\p1}}{shaft}{{\\p0}}"))
                if al:
                    ll, lsz2 = fit_text(al, gap - 6, 22, min_size=16, max_lines=2, factor=0.62)
                    out.append(L(tx, (ax1 + ax2) / 2, cy - 22, _join(ll), lsz2, th.red, t1 + 0.15, e, an=2,
                                 layer=5, font=th.font, extra="\\fad(150,0)"))
    if fixed:
        t0 = r0 + k * dt
        fl, fsz = fit_text("✓ Fixed in " + fixed, iw - 80, 28, min_size=18, factor=0.62)
        bw = min(iw - 20, est_w(fl[0], fsz, 0.62) + 70)
        out.append(L(rr, box.cx - bw / 2, cy + 170, bw, 64, 32, th.green, t0, e, alpha="&H30&", layer=4,
                     center=True, extra=_pop()))
        out.append(L(tx, box.cx, cy + 202, fl[0], fsz, th.white, t0, e, an=5, layer=5, font=th.font,
                     extra=_pop()))
    return _clean(out)


# ---------------------------------------------------------------- stat
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def count_values(value: str, n: int) -> list[str]:
    """Count-up strings that keep the source formatting ('4,000', '70%', '$2.3 million'); last == value."""
    value = str(value).strip()
    ms = _NUM.findall(value)
    if len(ms) != 1 or n < 2:
        return [value]
    m = _NUM.search(value)
    num = m.group(0)
    try:
        target = float(num.replace(",", ""))
    except ValueError:
        return [value]
    dec = len(num.split(".")[1]) if "." in num else 0
    commas = "," in num
    vals: list[str] = []
    for i in range(1, n):
        v = target * _ease(i / n)
        t = f"{v:,.{dec}f}" if commas else f"{v:.{dec}f}"
        vals.append(value[:m.start()] + t + value[m.end():])
    vals.append(value)
    dedup = [vals[0]]
    for v in vals[1:]:
        if v != dedup[-1]:
            dedup.append(v)
    return dedup


def stat(sc, box: Box, s: float, e: float, th: Theme) -> list[str]:
    ix, iy, iw, ih = box.inner()
    L = lambda f, *a, **k: f(*a, lo=s, hi=e, **k)  # noqa: E731
    value = str(sc.get("value") or "").strip()
    if not value:
        return []
    label, lsz = fit_text(str(sc.get("label") or ""), iw - 60, 46, min_size=28, max_lines=2)
    ctx, csz = fit_text(str(sc.get("context") or ""), iw - 60, 30, min_size=20, max_lines=2, factor=0.58)
    vsz = int(fit_text(value, iw - 60, 190, min_size=60, factor=0.66, caps=1.0)[1])
    out = [L(rr, ix, iy, iw, ih, 16, th.card, s, e, layer=2, alpha="&H30&", extra="\\fad(200,0)")]
    vh = vsz * LINE_H
    lh, ch = _text_h(len(label), lsz), _text_h(len(ctx), csz)
    total = vh + 24 + lh + (14 + ch if ctx else 0)
    y = iy + max(10, (ih - total) / 2)
    cnt_dur = min(2.2, 0.5 * (e - s))
    n = int(max(2, min(14, cnt_dur / 0.12)))
    vals = count_values(value, n)
    r0 = s + min(0.3, 0.1 * (e - s))
    step = cnt_dur / max(1, len(vals))
    for i, v in enumerate(vals):
        t0 = r0 + i * step
        t1 = e if i == len(vals) - 1 else r0 + (i + 1) * step
        out.append(L(tx, box.cx, y, v, vsz, th.accent, t0, t1, an=8, layer=4, font=th.font,
                     extra="\\fad(150,0)" if i == 0 else ""))
    y += vh + 24
    t_lab = r0 + cnt_dur * 0.6
    out.append(L(tx, box.cx, y, _join(label), lsz, th.white, t_lab, e, an=8, layer=4, font=th.font,
                 extra="\\fad(250,0)"))
    if ctx:
        out.append(L(tx, box.cx, y + lh + 14, _join(ctx), csz, th.grey, t_lab + 0.4, e, bold=0, an=8, layer=4,
                     font=th.font, extra="\\fad(250,0)"))
    return _clean(out)


# ---------------------------------------------------------------- timeline
def timeline(sc, box: Box, s: float, e: float, th: Theme) -> list[str]:
    ix, iy, iw, ih = box.inner()
    L = lambda f, *a, **k: f(*a, lo=s, hi=e, **k)  # noqa: E731
    evs = [x for x in (sc.get("events") or []) if isinstance(x, dict) and (x.get("date") or x.get("label"))][:4]
    if len(evs) < 2:
        return []
    n = len(evs)
    slot = iw / n
    cy = iy + ih * 0.50
    ttl, tsz = fit_text(str(sc.get("title") or "TIMELINE").upper(), iw, 30, min_size=18, factor=0.66)
    out = [L(tx, box.cx, iy - 4, _join(ttl), tsz, th.accent, s, e, an=8, layer=4, font=th.font,
             extra="\\fad(200,0)")]
    lx0, lx1 = ix + slot / 2, ix + iw - slot / 2
    r0, r1 = _span(s, e, 0.75)
    out.append(L(rr, lx0, cy - 3, lx1 - lx0, 6, 3, th.bar, s, e, layer=3))
    out.append(L(rr, lx0, cy - 3, lx1 - lx0, 6, 3, th.accent, r0, e, layer=4,
                 extra=_clip_grow(lx0, cy - 4, lx1, cy + 4, r1 - r0)))
    dt = (r1 - r0) / n
    for i, x in enumerate(evs):
        cx = ix + slot * (i + 0.5)
        t0 = r0 + dt * i
        out.append(L(rr, cx - 14, cy - 14, 28, 28, 14, th.accent, t0, e, layer=5, center=True,
                     extra=f"\\bord4\\3c{th.card}\\3a&H00&{_pop(0.2, 40)}"))
        dl, dsz = fit_text(str(x.get("date") or ""), slot - 24, 32, min_size=18, factor=0.62)
        out.append(L(tx, cx, cy - 40, _join(dl), dsz, th.accent, t0 + 0.1, e, an=2, layer=5, font=th.font,
                     extra="\\fad(200,0)"))
        ll, lsz = fit_text(str(x.get("label") or ""), slot - 24, 28, min_size=18, max_lines=3)
        out.append(L(tx, cx, cy + 36, _join(ll), lsz, th.white, t0 + 0.2, e, an=8, layer=5, font=th.font,
                     extra="\\fad(200,0)"))
    return _clean(out)


# ---------------------------------------------------------------- scam
def scam(sc, box: Box, s: float, e: float, th: Theme) -> list[str]:
    ix, iy, iw, ih = box.inner()
    L = lambda f, *a, **k: f(*a, lo=s, hi=e, **k)  # noqa: E731
    body = str(sc.get("body") or "").replace("{link}", BAR).strip()
    if not body:
        return []
    email = str(sc.get("channel") or "sms").lower() == "email"
    out = []
    lab_w = 360
    out.append(L(rr, ix + 40, iy - 2, lab_w, 44, 22, th.red, s, e, alpha="&H30&", layer=4, extra="\\fad(200,0)"))
    out.append(L(tx, ix + 40 + lab_w / 2, iy + 20, "EXAMPLE SCAM MESSAGE", 22, th.white, s, e, an=5, layer=5,
                 font=th.font, extra="\\fad(200,0)"))
    px, py, pw, ph = ix + 40, iy + 54, 440, ih - 54
    out.append(L(rr, px, py, pw, ph, 36, th.term_bg, s, e, layer=3, extra=f"\\bord3\\3c{th.bar}\\3a&H00&\\fad(200,0)"))
    out.append(L(rr, px + pw / 2 - 40, py + 14, 80, 8, 4, th.bar, s, e, layer=4))
    sender = str(sc.get("sender") or "").strip()
    sl, ssz = fit_text(sender, pw - 60, 26, min_size=16, factor=0.62)
    cy = py + 40
    if email:
        fl_, fsz_ = fit_text("From: " + sender, pw - 56, 22, min_size=14, factor=0.58)
        out.append(L(tx, px + 28, cy, fl_[0], fsz_, th.grey, s + 0.3, e, bold=0,
                     layer=5, font=th.font, extra="\\fad(200,0)"))
        subj, sbs = fit_text("Subject: " + str(sc.get("subject") or "").strip(), pw - 56, 24, min_size=14, factor=0.62)
        if subj and str(sc.get("subject") or "").strip():
            out.append(L(tx, px + 28, cy + 32, subj[0], sbs, th.white, s + 0.3, e, layer=5,
                         font=th.font, extra="\\fad(200,0)"))
        out.append(L(rr, px + 28, cy + 68, pw - 56, 2, 1, th.bar, s + 0.3, e, layer=5))
        by = cy + 84
        bx_w = pw - 56
        out_lines_max = int((py + ph - 30 - by) // (22 * LINE_H * 1.1))
        bl, bsz = fit_text(body, bx_w, 28, min_size=16, max_lines=max(2, out_lines_max))
        bubble = False
    else:
        out.append(L(tx, px + pw / 2, cy, sl[0] if sl else "", ssz, th.white, s + 0.3, e, an=8, layer=5,
                     font=th.font, extra="\\fad(200,0)"))
        out.append(L(rr, px + 20, cy + 40, pw - 40, 2, 1, th.bar, s + 0.3, e, layer=5))
        by = cy + 70
        bx_w = pw - 40 - 56
        max_ln = int((py + ph - 40 - by - 30) // (26 * LINE_H))
        bl, bsz = fit_text(body, bx_w - 28, 26, min_size=18, max_lines=max(2, max_ln))
        bubble = True
    bh_text = _text_h(len(bl), bsz)
    t_body = s + 0.5
    if bubble:
        bw = min(bx_w, max(est_w(l, bsz) for l in bl) + 44)
        out.append(L(rr, px + 20, by, bw, bh_text + 36, 22, th.card, t_body, e, layer=4, extra=_pop(0.25, 85)))
        tx_x, tx_y = px + 20 + 22, by + 18
    else:
        tx_x, tx_y = px + 28, by
    red_on, red_off = f"{{\\1c{th.red}}}", f"{{\\1c{th.white}}}"
    txt = "\\N".join(_esc(l).replace(BAR, red_on + BAR + red_off) for l in bl)
    out.append(L(tx, tx_x, tx_y, txt, bsz, th.white, t_body, e, bold=0, layer=5, font=th.font, raw=True,
                 extra="\\fad(200,0)"))
    # red-flag callouts
    flags = [str(f).strip() for f in (sc.get("flags") or []) if str(f).strip()][:3]
    cx0 = px + pw + 60
    cw = ix + iw - cx0
    if flags and cw > 200:
        row_h, rgap = 104, 26
        top = iy + 54 + (ph - (len(flags) * row_h + (len(flags) - 1) * rgap)) / 2
        r0 = s + 1.4
        dt = max(0.4, min(2.0, (e - r0 - 0.8) / len(flags)))
        for i, f in enumerate(flags):
            t0 = r0 + i * dt
            y = top + i * (row_h + rgap)
            fl, fsz = fit_text(f, cw - 130, 30, min_size=18, max_lines=2, factor=0.6)
            # slides in from the left, inside the gap to the phone; the clip spans the whole travel so the
            # border is never cut during the slide
            mv = f"\\move({cx0 - 40:.0f},{y:.0f},{cx0:.0f},{y:.0f},0,250)"
            clip = f"\\clip({cx0 - 48:.0f},{y - 6:.0f},{cx0 + cw + 4:.0f},{y + row_h + 6:.0f})"
            out.append(L(ev, 4, t0, e, f"{{\\an7{mv}{clip}\\bord3\\shad0\\1c{th.card}\\3c{th.red}\\fad(150,0)\\p1}}"
                                       f"{_rrect(cw, row_h, 16)}{{\\p0}}"))
            out.append(L(rr, cx0 + 22, y + row_h / 2 - 24, 48, 48, 24, th.red, t0 + 0.1, e, layer=5,
                         center=True, extra=_pop(0.2, 40)))
            out.append(L(tx, cx0 + 46, y + row_h / 2, "!", 34, th.white, t0 + 0.1, e, an=5, layer=6, font=th.font,
                         extra="\\fad(150,0)"))
            out.append(L(tx, cx0 + 92, y + row_h / 2, _join(fl), fsz, th.white, t0 + 0.1, e, an=4, layer=5,
                         font=th.font, extra="\\fad(200,0)"))
    return _clean(out)


# ---------------------------------------------------------------- headline (auto scene)
def headline(sc, box: Box, s: float, e: float, th: Theme) -> list[str]:
    ix, iy, iw, ih = box.inner()
    L = lambda f, *a, **k: f(*a, lo=s, hi=e, **k)  # noqa: E731
    text = str(sc.get("title") or sc.get("headline") or "").strip()
    if not text:
        return []
    lines, sz = fit_text(text, iw - 60, 68, min_size=36, max_lines=3)
    lh = sz * 1.22
    src = str(sc.get("source") or "").strip()
    total = lh * len(lines) + (70 if src else 0)
    y0 = iy + max(0, (ih - total) / 2)
    out = []
    r0 = s + min(0.3, 0.1 * (e - s))
    for i, ln in enumerate(lines):
        t0 = r0 + i * 0.35
        y = y0 + i * lh
        # kinetic: slide up + fade (\move replaces \pos, so the tag set is built by hand)
        out.append(L(ev, 4, t0, e, f"{{\\an8\\move({box.cx:.0f},{y + 28:.0f},{box.cx:.0f},{y:.0f},0,350)"
                                   f"\\fn{th.font}\\fs{sz}\\b1\\1c{th.white}\\bord0\\shad0\\fad(250,0)}}{_esc(ln)}"))
    yb = y0 + lh * len(lines) + 18
    t1 = r0 + len(lines) * 0.35
    barw = min(iw - 60, 160)
    out.append(L(rr, box.cx - barw / 2, yb, barw, 6, 3, th.accent, t1, e, layer=4,
                 extra=_clip_grow(box.cx - barw / 2, yb - 1, box.cx + barw / 2, yb + 7, 0.5)))
    if src:
        sl, ssz = fit_text("SOURCE  ·  " + src.upper(), iw - 60, 26, min_size=16, factor=0.66)
        out.append(L(tx, box.cx, yb + 28, sl[0], ssz, th.dim, t1 + 0.2, e, an=8, layer=4, font=th.font,
                     extra="\\fad(250,0)"))
    return _clean(out)


# ---------------------------------------------------------------- cve (auto scene) + panel card
def _card_of(sc) -> dict:
    c = sc.get("card") if isinstance(sc, dict) else None
    return c if isinstance(c, dict) else (sc if isinstance(sc, dict) else {})


def _score(card) -> float | None:
    try:
        v = float(card.get("cvss"))
    except (TypeError, ValueError):
        return None
    return v if 0 < v <= 10 else None


def cve(sc, box: Box, s: float, e: float, th: Theme) -> list[str]:
    ix, iy, iw, ih = box.inner()
    L = lambda f, *a, **k: f(*a, lo=s, hi=e, **k)  # noqa: E731
    card = _card_of(sc)
    cid = str(card.get("cve_id") or "").strip().upper()
    if not cid:
        return []
    score = _score(card)
    sev = str(card.get("severity") or "").strip().upper()
    col = _c(_sev_color(score, sev), "#FFD60A")
    out = [L(rr, ix, iy, iw, ih, 16, th.card, s, e, layer=2, alpha="&H30&", extra="\\fad(200,0)")]
    il, isz = fit_text(cid, iw - 60, 52, min_size=24, factor=0.602, caps=1.0)
    out.append(L(tx, box.cx, iy + 8, il[0], isz, th.accent, s, e, an=8, layer=4, font=th.mono,
                 extra="\\fad(200,0)"))
    rows = {k: t for k, t, _ in _card_rows(card)}
    aff = rows.get("affects")
    if aff:
        al, asz = fit_text(aff, iw - 60, 26, min_size=16, factor=0.58)
        out.append(L(tx, box.cx, iy + 68, al[0], asz, th.grey, s + 0.2, e, bold=0, an=8, layer=4, font=th.font))
    r0 = s + min(0.3, 0.1 * (e - s))
    if score is not None:
        dur = min(2.0, 0.5 * (e - s))
        n = int(max(2, min(14, dur / 0.12)))
        vals = [f"{score * _ease(i / n):.1f}" for i in range(1, n)] + [f"{score:.1f}"]
        vals = [v for i, v in enumerate(vals) if i == 0 or v != vals[i - 1]]
        step = dur / len(vals)
        for i, v in enumerate(vals):
            t1 = e if i == len(vals) - 1 else r0 + (i + 1) * step
            out.append(L(tx, box.cx, iy + 100, v, 160, col, r0 + i * step, t1, an=8, layer=4, font=th.font))
        bw = min(iw - 120, 800)
        bx, by = box.cx - bw / 2, iy + 364
        out.append(L(rr, bx, by, bw, 20, 10, th.bar, s, e, layer=3))
        out.append(L(rr, bx, by, bw * score / 10, 20, 10, col, r0, e, layer=4,
                     extra=_clip_grow(bx, by - 1, bx + bw * score / 10, by + 21, dur)))
        out.append(L(tx, bx, by + 26, "CVSS", 18, th.dim, r0, e, bold=0, layer=4, font=th.font))
        out.append(L(tx, bx + bw, by + 26, "10", 18, th.dim, r0, e, bold=0, an=9, layer=4, font=th.font))
    if sev:
        out.append(L(tx, box.cx, iy + 292, sev, 38, col, r0 + 0.4, e, an=8, layer=4, font=th.font,
                     extra="\\fad(250,0)"))
    badges = []
    if card.get("exploited"):
        badges.append(("● EXPLOITED", th.red))
    if card.get("patch_available"):
        badges.append(("✓ PATCH OUT", th.green))
    t = r0 + 1.0
    bwid = 250
    bx0 = box.cx - (len(badges) * bwid + (len(badges) - 1) * 24) / 2
    for i, (lab, c) in enumerate(badges):
        x = bx0 + i * (bwid + 24)
        out.append(L(rr, x, iy + ih - 64, bwid, 46, 23, c, t + i * 0.3, e, alpha="&H30&", layer=4, center=True,
                     extra=_pop()))
        out.append(L(tx, x + bwid / 2, iy + ih - 41, lab, 24, th.white, t + i * 0.3, e, an=5, layer=5,
                     font=th.font, extra="\\fad(150,0)"))
    return _clean(out)


def cve_card(card: dict, x, y, w, s, e, th: Theme):
    """Panel CVE card (mock.py): id + score + animated CVSS bar + badges. -> (lines, height)."""
    L = lambda f, *a, **k: f(*a, lo=s, hi=e, **k)  # noqa: E731
    cid = str(card.get("cve_id") or "").strip().upper()
    score = _score(card)
    sev = str(card.get("severity") or "").strip().upper()
    badges = []
    if card.get("exploited"):
        badges.append(("● EXPLOITED", th.red))
    if card.get("patch_available"):
        badges.append(("✓ PATCH OUT", th.green))
    if not (cid or score or badges):
        return [], 0
    h = 130 if score is not None else 70
    if badges:
        h += 62
    col = _c(_sev_color(score, sev), "#FFD60A")
    out = [L(rr, x + 24, y, w - 48, h, 14, th.card, s, e, layer=3, extra="\\fad(200,0)")]
    ix = x + 48
    right = 140 if score is not None else 0
    il, isz = fit_text(cid, w - 96 - right, 28, min_size=16, factor=0.602, caps=1.0)
    if il:
        out.append(L(tx, ix, y + 22, il[0], isz, th.accent, s, e, an=7, layer=5, font=th.mono, extra="\\fad(200,0)"))
    if score is not None:
        out.append(L(tx, x + w - 48, y + 14, f"{score:.1f}", 54, col, s, e, an=9, layer=5, font=th.font))
        if sev:
            out.append(L(tx, x + w - 48, y + 76, sev, 20, col, s, e, an=9, layer=5, font=th.font))
        bw = min(w - 96 - 150, 380)
        out.append(L(rr, ix, y + 78, bw, 14, 7, th.bar, s, e, layer=4))
        fw = max(8.0, bw * score / 10)
        out.append(L(rr, ix, y + 78, fw, 14, 7, col, s + 0.2, e, layer=5,
                     extra=_clip_grow(ix, y + 77, ix + fw, y + 93, 0.8)))
        out.append(L(tx, ix, y + 98, "CVSS", 18, th.dim, s, e, bold=0, layer=5, font=th.font))
    by = y + h - 62
    bwid = (w - 96 - 14) / 2 if len(badges) == 2 else min(250, w - 96)
    for i, (lab, c) in enumerate(badges):
        bx = ix + i * (bwid + 14)
        out.append(L(rr, bx, by + 6, bwid, 42, 21, c, s + 0.5 + i * 0.25, e, alpha="&H30&", layer=5,
                     extra="\\fad(150,0)"))
        bl, bsz = fit_text(lab, bwid - 20, 22, min_size=14, factor=0.62)
        out.append(L(tx, bx + bwid / 2, by + 27, bl[0], bsz, th.white, s + 0.5 + i * 0.25, e, an=5, layer=6,
                     font=th.font, extra="\\fad(150,0)"))
    return _clean(out), h


# ---------------------------------------------------------------- panel: key point / checklist
def key_point(text, x, y, w, s, e, th: Theme):
    """Slide-in key point card. -> (lines, height)."""
    L = lambda f, *a, **k: f(*a, lo=s, hi=e, **k)  # noqa: E731
    lines, sz = fit_text(text, w - 48 - 60, 30, min_size=22, max_lines=2)
    if not lines:
        return [], 0
    h = 76 if len(lines) == 1 else 112
    cw = w - 48
    # slides in from the left; the clip covers the whole travel (28 px) so the card is never cut
    clip = f"\\clip({x + 24 - 28:.0f},{y - 2:.0f},{x + 24 + cw + 4:.0f},{y + h + 2:.0f})"

    def mv(px, py):
        return f"\\move({px - 28:.0f},{py:.0f},{px:.0f},{py:.0f},0,300)"

    out = [
        L(ev, 3, s, e, f"{{\\an7{mv(x + 24, y)}{clip}\\bord0\\shad0\\1c{th.card}\\fad(200,0)\\p1}}"
                       f"{_rrect(cw, h, 12)}{{\\p0}}"),
        L(ev, 4, s, e, f"{{\\an7{mv(x + 24, y)}{clip}\\bord0\\shad0\\1c{th.accent}\\fad(200,0)\\p1}}"
                       f"{_rrect(8, h, 4)}{{\\p0}}"),
        L(ev, 5, s, e, f"{{\\an4{mv(x + 56, y + h / 2)}{clip}\\fn{th.font}\\fs{sz}\\b1\\1c{th.white}\\bord0\\shad0"
                       f"\\fad(200,0)}}{_esc(_join(lines))}"),
    ]
    return _clean(out), h


def checklist(items, times, x, y, w, s, e, th: Theme):
    """'WHAT TO DO' list; item i flips grey -> green tick at times[i]. (x, y, w) = right panel. -> (lines, height)."""
    L = lambda f, *a, **k: f(*a, lo=s, hi=e, **k)  # noqa: E731
    items = [(i.get("text") if isinstance(i, dict) else i) for i in (items or [])]
    items = [str(i).strip() for i in items if str(i or "").strip()][:3]
    if not items:
        return [], 0
    out = [L(tx, x + 32, y + 34, "WHAT TO DO", 40, th.kw, s, e, layer=4, font=th.font, extra="\\fad(200,0)"),
           L(rr, x + 32, y + 90, 90, 6, 3, th.kw, s, e, layer=4)]
    cy = y + 140
    for i, it in enumerate(items):
        lines, sz = fit_text(it, w - 48 - 116 - 20, 30, min_size=22, max_lines=2)
        h = 84 if len(lines) == 1 else 120
        t0 = s + 0.15 * i
        try:
            tk = max(t0, float(times[i]))
        except (IndexError, TypeError, ValueError):
            tk = None
        mid = cy + h / 2
        out.append(L(rr, x + 24, cy, w - 48, h, 14, th.card, t0, e, layer=3, extra="\\fad(200,0)"))
        out.append(L(rr, x + 48, mid - 22, 44, 44, 10, th.bar, t0, tk if tk is not None else e, layer=4))
        out.append(L(tx, x + 116, mid, _join(lines), sz, th.grey, t0, tk if tk is not None else e, an=4, layer=5,
                     font=th.font))
        if tk is not None:
            out.append(L(rr, x + 48, mid - 22, 44, 44, 10, th.green, tk, e, layer=4, center=True, extra=_pop(0.2, 60)))
            out.append(L(tx, x + 70, mid, "✓", 32, th.white, tk, e, an=5, layer=6, font=th.font,
                         extra=_pop(0.2, 60)))
            out.append(L(tx, x + 116, mid, _join(lines), sz, th.white, tk, e, an=4, layer=5, font=th.font))
        cy += h + 18
    return _clean(out), cy - y


# ---------------------------------------------------------------- full-screen cards
_JIT = [(-14, 18, -10), (12, -16, 8), (-8, 10, -14), (16, -6, 12), (-4, 4, -6)]


def _glitch_rects(W, dx, col, t0, t1, s, e, y):
    x = max(0, dx)
    return rr(x, y, W - abs(dx), 6, 0, col, t0, t1, alpha="&H90&", layer=21, lo=s, hi=e)


def transition(n, total, headline_text, tagline, s, e, W, H, th: Theme):
    """Full-screen story card; glitch-offset ghosts jitter for 400 ms then settle."""
    L = lambda f, *a, **k: f(*a, lo=s, hi=e, **k)  # noqa: E731
    k = H / 1080.0
    cx = W / 2
    out = [L(rr, 0, 0, W, H, 0, "&H0B0705&", s, e, alpha="&H00&", layer=20)]     # opaque: hides the chrome
    label = f"STORY {int(n)} OF {int(total)}"
    ly = 360 * k
    out.append(L(tx, cx, ly, label, 46 * k, th.accent, s, e, an=5, layer=23, font=th.font, extra="\\fad(120,0)"))
    jit_end = min(e, s + 0.4)
    nst = len(_JIT)
    stp = (jit_end - s) / nst
    for i, (a, b, c) in enumerate(_JIT):
        t0, t1 = s + i * stp, s + (i + 1) * stp
        for dx, col in ((a, "&HFF00FF&"), (b, th.accent)):
            out.append(L(tx, cx + dx, ly, label, 46 * k, col, t0, t1, an=5, layer=22, font=th.font,
                         extra="\\alpha&HA0&"))
        out.append(_glitch_rects(W, a * 2, "&HFF00FF&", t0, t1, s, e, (372 + 14 * (i % 3)) * k))
        out.append(_glitch_rects(W, b * 2, th.accent, t0, t1, s, e, (612 - 30 * (i % 2)) * k))
    out.append(L(rr, cx - 60 * k, 404 * k, 120 * k, 6 * k, 3, th.accent, jit_end, e, layer=23,
                 extra=_clip_grow(cx - 60 * k, 403 * k, cx + 60 * k, 411 * k, 0.3)))
    lines, sz = fit_text(headline_text, W - 240 * k, 76 * k, min_size=40 * k, max_lines=3)
    out.append(L(tx, cx, 440 * k, _join(lines), sz, th.white, s, e, an=8, layer=23, font=th.font,
                 extra="\\fad(200,0)"))
    if tagline:
        tl, tsz = fit_text(str(tagline).upper(), W - 240 * k, 26 * k, min_size=16 * k, factor=0.66)
        ty = 440 * k + _text_h(len(lines), sz) + 50 * k
        out.append(L(tx, cx, ty, tl[0], tsz, th.dim, s + 0.2, e, an=8, layer=23, font=th.font,
                     extra="\\fad(250,0)"))
    return _clean(out)


def intro_card(show_title, week_label, titles, s, e, W, H, th: Theme):
    """Intro: show title, Week pill, lineup of chapter titles fading in one by one."""
    L = lambda f, *a, **k: f(*a, lo=s, hi=e, **k)  # noqa: E731
    k = H / 1080.0
    cx = W / 2
    out = [L(rr, 0, 0, W, H, 0, "&H0B0705&", s, e, alpha="&H00&", layer=20)]     # opaque: hides the chrome
    tl, tsz = fit_text(str(show_title or "").upper(), W - 240 * k, 92 * k, min_size=44 * k, max_lines=2, factor=0.66)
    ty = 150 * k
    out.append(L(tx, cx, ty, _join(tl), tsz, th.white, s, e, an=8, layer=23, font=th.font, extra="\\fad(300,0)"))
    py = ty + _text_h(len(tl), tsz) + 24 * k
    wk = str(week_label or "").strip()
    if wk:
        wl, wsz = fit_text(wk.upper(), 500 * k, 34 * k, min_size=18 * k, factor=0.66)
        pw = est_w(wl[0], wsz, 0.66) + 60 * k
        out.append(L(rr, cx - pw / 2, py, pw, 56 * k, 28 * k, th.accent, s + 0.2, e, layer=22, extra="\\fad(300,0)"))
        out.append(L(tx, cx, py + 28 * k, wl[0], wsz, th.ink, s + 0.2, e, an=5, layer=23, font=th.font,
                     extra="\\fad(300,0)"))
        py += 56 * k
    lines = [str(t).strip() for t in (titles or []) if str(t).strip()]
    # rows must end above the caption box (top y = CAPTION_TOP) with a 20 px gap: shrink the row pitch (and the
    # font with it) when there are many stories or the title/pill took two lines
    top = py + 60 * k
    avail = max(44 * k, CAPTION_TOP * k - 20 * k - top)
    maxn = max(1, int(avail // (44 * k)))
    lines = lines[:min(8, maxn)]
    if lines:
        row_h = min(66 * k, avail / len(lines))
        row_sz = min(38 * k, row_h / 1.3)
        r0 = s + min(1.0, 0.2 * (e - s))
        dt = max(0.1, min(0.7, (e - r0 - 0.8) / len(lines)))
        colw = min(W - 240 * k, 1200 * k)
        fits = [fit_text(t, colw - 110 * k, row_sz, min_size=min(22 * k, row_sz)) for t in lines]
        blk = 90 * k + max(est_w(f[0][0], f[1], 0.62) if f[0] else 0 for f in fits)
        x0 = cx - min(blk, colw) / 2                  # centre the lineup block under the title
        for i, t in enumerate(lines):
            fl, fsz = fits[i]
            y = top + i * row_h
            t0 = r0 + i * dt
            out.append(L(tx, x0, y, f"{i + 1:02d}", fsz, th.accent, t0, e, layer=23, font=th.mono,
                         extra="\\fad(350,0)"))
            out.append(L(tx, x0 + 90 * k, y, fl[0], fsz, th.white, t0, e, layer=23, font=th.font,
                         extra="\\fad(350,0)"))
    return _clean(out)


def outro_card(handle, s, e, W, H, th: Theme):
    """Outro: SUBSCRIBE pill, handle, sources note."""
    L = lambda f, *a, **k: f(*a, lo=s, hi=e, **k)  # noqa: E731
    k = H / 1080.0
    cx = W / 2
    out = [L(rr, 0, 0, W, H, 0, "&H0B0705&", s, e, alpha="&H00&", layer=20)]     # opaque: hides the chrome
    pw, ph = 640 * k, 150 * k
    py = 330 * k
    out.append(L(rr, cx - pw / 2, py, pw, ph, 36 * k, th.red, s + 0.1, e, layer=22, center=True, extra=_pop(0.35, 60)))
    out.append(L(tx, cx, py + ph / 2, "SUBSCRIBE", 84 * k, th.white, s + 0.1, e, an=5, layer=23, font=th.font,
                 extra=_pop(0.35, 60)))
    h = str(handle or "").strip()
    y = py + ph + 60 * k
    if h:
        hl, hsz = fit_text(h, W - 240 * k, 60 * k, min_size=24 * k, factor=0.6)
        out.append(L(tx, cx, y, hl[0], hsz, th.accent, s + 0.5, e, an=8, layer=23, font=th.font,
                     extra="\\fad(300,0)"))
        y += hsz * LINE_H + 30 * k
    out.append(L(tx, cx, y, "Full sources in the description", 32 * k, th.dim, s + 0.8, e, bold=0, an=8, layer=23,
                 font=th.font, extra="\\fad(300,0)"))
    return _clean(out)


BUILDERS = {"terminal": terminal, "flow": flow, "stat": stat, "timeline": timeline, "scam": scam,
            "headline": headline, "cve": cve}
