"""Landscape (16:9) ASS overlays for the weekly recap video, plus the thumbnail ASS."""
from __future__ import annotations

import re
from pathlib import Path

from . import captions as C
from .config import Config

_KEY = "recap."


def _g(cfg: Config, key: str, default):
    v = cfg.get(_KEY + key, default)
    return default if v is None else v


def _fmt(cfg: Config) -> tuple[int, int, str, str, str]:
    font = cfg.get("captions.font", "DejaVu Sans")
    accent = C.ass_color(cfg.get("branding.accent", "#3DDCFF"), "#3DDCFF")
    kw = C.ass_color(cfg.get("branding.keyword_color", "#FFD60A"), "#FFD60A")
    return int(_g(cfg, "width", 1920)), int(_g(cfg, "height", 1080)), font, accent, kw


def _wrap(text: str, width_chars: int, max_lines: int = 2) -> list[str]:
    words = (text or "").split()
    lines: list[str] = []
    cur = ""
    for wd in words:
        if cur and len(cur) + 1 + len(wd) > width_chars:
            lines.append(cur)
            cur = wd
        else:
            cur = f"{cur} {wd}".strip()
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        rest = " ".join(lines[max_lines - 1:])
        lines = lines[:max_lines - 1] + [rest]
    if lines and len(lines[-1]) > width_chars:
        lines[-1] = lines[-1][:max(1, width_chars - 1)].rstrip(" ,.") + "\u2026"
    return lines


def _lookup(overlays: dict | None, idx: int) -> dict:
    if not overlays:
        return {}
    v = overlays.get(idx)
    if v is None:
        v = overlays.get(str(idx))
    return v if isinstance(v, dict) else {}


def build_recap_ass(cfg: Config, words: list, chunks: list, parts: list[dict], duration: float, *,
                    title: str, week_label: str, handle: str, overlays: dict | None = None) -> str:
    w, h, font, accent, kw_color = _fmt(cfg)
    dur = max(float(duration), 0.1)
    cap_size = int(_g(cfg, "caption_font_size", 52))
    tc_secs = float(_g(cfg, "title_card_seconds", 3.5))
    card_secs = float(cfg.get("data_card.seconds", 4.5) or 4.5)
    sc = w / 1920.0
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {w}\nPlayResY: {h}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Caption,{font},{cap_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,0,5,60,60,0,1\n"
        f"Style: Label,{font},{int(cap_size * 0.55)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,0,7,40,40,30,1\n"
        f"Style: Card,{font},46,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1\n"
        f"Style: Bar,{font},20,{accent},{accent},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines: list[str] = [header]

    def ev(layer: int, s: float, e: float, style: str, text: str) -> str | None:
        s, e = max(0.0, s), min(e, dur)
        if round(e * 100) <= round(s * 100):
            return None
        return f"Dialogue: {layer},{C.format_ass_time(s)},{C.format_ass_time(e)},{style},,0,0,0,,{text}\n"

    def add(x: str | None) -> None:
        if x:
            lines.append(x)

    def pill(layer: int, s: float, e: float, cx: int, cy: int, text: str, fs: int, color: str,
             text_color: str = "&H00000000&", fad: str = "\\fad(200,200)") -> None:
        tw = C._est_width(text, fs, 0.74)
        pw, ph = int(tw + fs * 1.0 + 10), int(fs * 1.5)
        add(ev(layer, s, e, "Card",
               f"{{\\an7\\pos({cx - pw // 2},{cy - ph // 2}){fad}\\p1\\c{color}&\\bord0\\shad0}}"
               f"{C._rrect(pw, ph, ph // 2)}{{\\p0}}"))
        add(ev(layer + 1, s, e, "Card",
               f"{{\\an5\\q0\\pos({cx},{cy}){fad}\\fs{fs}\\b1\\bord0\\shad0\\c{text_color}}}{C._escape(text.upper())}"))

    # ---- lower-third captions ----
    pos = f"\\pos({w // 2},{int(h * 0.87)})"
    evs = None
    if str(cfg.get("captions.style", "karaoke")).lower() == "karaoke" and words and chunks:
        evs = C._karaoke_events(chunks, words, accent, kw_color, set(), pos, dur)
    if evs is None:
        evs = C._fit_events([(a, b, f"{{{pos}}}{C._plain_text(t)}") for a, b, t in chunks], dur)
    lines.extend(evs)

    # ---- per-part overlays ----
    for idx, p in enumerate(parts or []):
        kind = p.get("kind")
        ps, pe = float(p.get("start", 0)), min(float(p.get("end", dur)), dur)
        ptitle = str(p.get("title") or "").strip()
        if pe <= ps:
            continue
        if kind == "intro":
            e = min(ps + 6.0, pe)
            add(ev(4, ps, e, "Card",
                   f"{{\\an5\\q0\\pos({w // 2},{int(h * 0.40)})\\fad(300,300)\\fs{int(h * 0.085)}\\b1\\bord6\\shad0"
                   f"\\c{kw_color}&}}{C._escape((title or ptitle).upper())}"))
            if week_label:
                add(ev(4, ps, e, "Card",
                       f"{{\\an5\\q0\\pos({w // 2},{int(h * 0.52)})\\fad(300,300)\\fs{int(h * 0.05)}\\b1\\bord4\\shad0"
                       f"\\c{accent}&}}{C._escape(week_label.upper())}"))
        elif kind == "outro":
            s = min(ps + 0.3, pe)
            add(ev(4, s, pe, "Card",
                   f"{{\\an5\\q0\\pos({w // 2},{int(h * 0.38)})\\fad(300,300)\\fs{int(h * 0.12)}\\b1\\bord6\\shad0"
                   f"\\c{accent}&}}SUBSCRIBE"))
            if handle:
                add(ev(4, s, pe, "Card",
                       f"{{\\an5\\q0\\pos({w // 2},{int(h * 0.54)})\\fad(300,300)\\fs{int(h * 0.07)}\\b1\\bord5\\shad0"
                       f"\\c&H00FFFFFF&}}{C._escape(handle)}"))
        elif kind == "story":
            n, total = int(p.get("index", idx)), int(p.get("total", 0))
            ov = _lookup(overlays, idx)
            label = f"{n}/{total}  {ptitle}".strip().upper()
            if len(label) > 52:
                label = label[:51].rstrip() + "\u2026"
            add(ev(3, ps, pe, "Label", f"{{\\an7\\q0\\pos({int(40 * sc)},{int(30 * sc)})\\bord3}}{C._escape(label)}"))
            # title card
            e = min(ps + tc_secs, pe)
            bw, fs_h, fs_s = int(w * 0.62), int(h * 0.062), int(h * 0.034)
            hl = _wrap(ptitle.upper(), max(8, int((bw - 100) / (fs_h * 0.68))), 2)
            badge = str(ov.get("badge") or "").strip()
            fs_b = int(h * 0.03)
            ph_b = int(fs_b * 1.5)
            pad = int(h * 0.035)
            bh = 2 * pad + int(fs_s * 1.3) + len(hl) * int(fs_h * 1.2) + (ph_b + int(pad * 0.5) if badge else 0)
            cx, cy = w // 2, int(h * 0.42)
            top = cy - bh // 2
            fad = "\\fad(200,200)"
            add(ev(4, ps, e, "Card",
                   f"{{\\an7\\pos({cx - bw // 2},{top}){fad}\\p1\\c&H00141414&\\1a&H18&\\3c{accent}&\\bord4\\shad0}}"
                   f"{C._rrect(bw, bh, int(h * 0.03))}{{\\p0}}"))
            y = top + pad
            if badge:
                pill(4, ps, e, cx, y + ph_b // 2, badge, fs_b, C.ass_color(ov.get("badge_color"), "#FF3B30"),
                     "&H00FFFFFF&", fad)
                y += ph_b + int(pad * 0.5)
            add(ev(5, ps, e, "Card",
                   f"{{\\an5\\q0\\pos({cx},{y + int(fs_s * 0.65)}){fad}\\fs{fs_s}\\b1\\bord0\\shad0\\c{accent}&}}"
                   f"STORY {n} OF {total}"))
            y += int(fs_s * 1.3)
            add(ev(5, ps, e, "Card",
                   f"{{\\an8\\q0\\pos({cx},{y}){fad}\\fs{fs_h}\\b1\\bord0\\shad0\\c&H00FFFFFF&}}"
                   + "\\N".join(C._escape(x) for x in hl)))
            # data card
            card = ov.get("card")
            if isinstance(card, dict) and card:
                s = ps + tc_secs
                ce = min(s + card_secs, pe, dur)
                if ce - s >= 1.0:
                    lines.extend(C._card_events(card, accent, w, h, s, ce, 0,
                                                scale=0.75, center=(0.74 * w, 0.40 * h)))

    # ---- progress bar with part ticks ----
    bh_ = max(6, int(10 * sc))
    dur_ms = int(round(dur * 1000))
    add(ev(3, 0, dur, "Bar",
           f"{{\\an7\\pos(0,{h - bh_})\\p1\\fscx0\\t(0,{dur_ms},\\fscx100)\\c{accent}&\\bord0\\shad0}}"
           f"m 0 0 l {w} 0 {w} {bh_} 0 {bh_}{{\\p0}}"))
    for p in (parts or [])[1:]:
        ps = float(p.get("start", 0))
        if 0 < ps < dur:
            add(ev(4, 0, dur, "Bar",
                   f"{{\\an7\\pos({int(w * ps / dur) - 2},{h - bh_ - 8})\\p1\\c&H00FFFFFF&\\bord0\\shad0}}"
                   f"m 0 0 l 4 0 4 {bh_ + 8} 0 {bh_ + 8}{{\\p0}}"))
    return "".join(lines)


def write_recap_ass(cfg: Config, words: list, chunks: list, parts: list[dict], duration: float,
                    out_path: Path | str, *, title: str, week_label: str, handle: str,
                    overlays: dict | None = None) -> Path:
    out = Path(out_path)
    out.write_text(build_recap_ass(cfg, words, chunks, parts, duration, title=title, week_label=week_label,
                                   handle=handle, overlays=overlays), encoding="utf-8")
    return out


def build_thumbnail_ass(cfg: Config, text: str, *, w: int = 1280, h: int = 720,
                        label: str = "WEEKLY RECAP") -> str:
    font = cfg.get("captions.font", "DejaVu Sans")
    accent = C.ass_color(cfg.get("branding.accent", "#3DDCFF"), "#3DDCFF")
    kw = C.ass_color(cfg.get("branding.keyword_color", "#FFD60A"), "#FFD60A")
    t = re.sub(r"\s+", " ", (text or "").strip().upper())
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {w}\nPlayResY: {h}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Thumb,{font},120,{kw},{kw},&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,12,0,5,40,40,0,1\n"
        f"Style: Pill,{font},40,&H00000000,&H00000000,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    out = [header]
    end = "0:00:10.00"
    if t:
        avail = 0.92 * w
        size = min(int(0.26 * h), int(avail / (max(1, len(t)) * 0.72)))
        rows = [t]
        if size < int(0.15 * h) and " " in t:
            toks = t.split()
            best = min(range(1, len(toks)), key=lambda i: abs(len(" ".join(toks[:i])) - len(" ".join(toks[i:]))))
            rows = [" ".join(toks[:best]), " ".join(toks[best:])]
            longest = max(len(r) for r in rows)
            size = min(int(0.22 * h), int(avail / (longest * 0.72)))
        size = max(size, 24)
        body = "\\N".join(
            f"{{\\c{kw if i == len(rows) - 1 else '&H00FFFFFF'}&}}{C._escape(r)}" for i, r in enumerate(rows))
        out.append(f"Dialogue: 2,0:00:00.00,{end},Thumb,,0,0,0,,"
                   f"{{\\an5\\q0\\pos({w // 2},{int(h * 0.56)})\\fs{size}\\bord{max(8, size // 10)}}}{body}\n")
    if label:
        fs = int(h * 0.058)
        pw, ph = int(C._est_width(label, fs, 0.74) + fs * 1.2), int(fs * 1.6)
        x0, y0 = int(w * 0.04), int(h * 0.06)
        out.append(f"Dialogue: 3,0:00:00.00,{end},Pill,,0,0,0,,{{\\an7\\pos({x0},{y0})\\p1\\c{accent}&\\bord0\\shad0}}"
                   f"{C._rrect(pw, ph, ph // 2)}{{\\p0}}\n")
        out.append(f"Dialogue: 4,0:00:00.00,{end},Pill,,0,0,0,,{{\\an5\\q0\\pos({x0 + pw // 2},{y0 + ph // 2})"
                   f"\\fs{fs}\\bord0}}{C._escape(label)}\n")
    return "".join(out)
