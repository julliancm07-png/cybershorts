"""Sample frames for the newsroom recap layout (mockup, fictional story)."""
import subprocess
from pathlib import Path

D = Path("/tmp/mock")
W, H = 1920, 1080
FONT = "DejaVu Sans"
WHITE, GREY, DIM = "&HFFFFFF&", "&HB8AFA6&", "&H8A7F74&"
ACC = "&HFFDC3D&"      # #3DDCFF
KEY = "&H0AD6FF&"      # #FFD60A
RED, GREEN = "&H303BFF&", "&H58D130&"
PANEL = "&H26160F&"    # #0F1626
INK = "&H140B07&"


def rr(x, y, w, h, r, col, alpha="&H00&", layer=1, extra=""):
    w, h, r = int(w), int(h), int(min(r, w // 2, h // 2))
    k = int(r * 0.45)
    d = (f"m {r} 0 l {w - r} 0 b {w - k} 0 {w} {k} {w} {r} l {w} {h - r} b {w} {h - k} {w - k} {h} {w - r} {h} "
         f"l {r} {h} b {k} {h} 0 {h - k} 0 {h - r} l 0 {r} b 0 {k} {k} 0 {r} 0")
    return (f"Dialogue: {layer},0:00:00.00,0:00:05.00,Default,,0,0,0,,"
            f"{{\\an7\\pos({x},{y})\\bord0\\shad0\\1c{col}\\1a{alpha}{extra}\\p1}}{d}{{\\p0}}")


def tx(x, y, text, size, col=WHITE, bold=0, an=7, layer=3, extra="", font=FONT):
    return (f"Dialogue: {layer},0:00:00.00,0:00:05.00,Default,,0,0,0,,"
            f"{{\\an{an}\\pos({x},{y})\\fn{font}\\fs{size}\\b{bold}\\1c{col}\\bord0\\shad0{extra}}}{text}")


def head():
    return (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {W}\nPlayResY: {H}\nWrapStyle: 2\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
            "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
            "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Default,{FONT},40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")


def top_bar(ev, chapter):
    ev.append(tx(60, 52, "THIS WEEK IN CYBER", 40, WHITE, 1))
    ev.append(rr(520, 50, 150, 48, 24, ACC))
    ev.append(tx(595, 74, "WEEK 1", 28, INK, 1, an=5, layer=4))
    ev.append(tx(700, 60, chapter, 30, GREY, 0))
    ev.append(tx(1860, 60, "@yourhandle", 28, DIM, 0, an=9))


def progress(ev, frac, current):
    x0, x1, y = 60, 1860, 1040
    ev.append(rr(x0, y, x1 - x0, 8, 4, "&H4A3B30&"))
    ev.append(rr(x0, y, (x1 - x0) * frac, 8, 4, ACC, layer=2))
    starts = [0.0, 0.08, 0.27, 0.46, 0.64, 0.82]
    for i, s in enumerate(starts[1:], 1):
        x = x0 + (x1 - x0) * s
        ev.append(rr(x - 3, y - 8, 6, 24, 3, WHITE if i <= current else DIM, layer=3))


def frame_story():
    ev = []
    top_bar(ev, "2 / 5   Critical VPN gateway flaw exploited")
    # video frame border + story chip
    ev.append(rr(58, 148, 1164, 657, 12, ACC, "&HFF&", layer=1, extra=f"\\bord3\\3c{ACC}\\3a&H00&"))
    ev.append(rr(84, 174, 236, 50, 10, ACC, layer=4))
    ev.append(tx(202, 199, "STORY 2 OF 5", 26, INK, 1, an=5, layer=5))
    # lower-third caption (karaoke, current word highlighted)
    ev.append(rr(90, 860, 1100, 92, 18, "&H000000&", "&H50&", layer=2))
    ev.append(tx(640, 906, f"that lets attackers {{\\1c{KEY}}}run code{{\\1c{WHITE}}} without logging in",
                 46, WHITE, 1, an=5, layer=3))
    # right info panel
    px, py, pw, ph = 1260, 146, 600, 862
    ev.append(rr(px, py, pw, ph, 18, PANEL, "&H10&", layer=1))
    ev.append(tx(px + 32, py + 30, "SOURCE  ·  EXAMPLE SECURITY NEWS", 22, DIM, 1))
    ev.append(tx(px + 32, py + 70, "Critical VPN gateway flaw\\Nexploited before a patch", 38, WHITE, 1))
    # CVE card
    cy = py + 190
    ev.append(rr(px + 24, cy, pw - 48, 200, 14, "&H3A2516&", layer=2))
    ev.append(tx(px + 48, cy + 22, "CVE-2026-12345", 28, ACC, 1, font="DejaVu Sans Mono", layer=4))
    ev.append(tx(px + pw - 48, cy + 20, "9.8", 54, RED, 1, an=9, layer=4))
    ev.append(tx(px + pw - 48, cy + 80, "CRITICAL", 20, RED, 1, an=9, layer=4))
    bx, bw = px + 48, 380
    ev.append(rr(bx, cy + 78, bw, 14, 7, "&H5A4436&", layer=3))
    ev.append(rr(bx, cy + 78, bw * 0.98, 14, 7, RED, layer=4))
    ev.append(tx(bx, cy + 98, "CVSS 3.1", 18, DIM, 0, layer=4))
    ev.append(rr(px + 48, cy + 136, 250, 42, 21, RED, "&H30&", layer=4))
    ev.append(tx(px + 173, cy + 157, "● EXPLOITED", 22, WHITE, 1, an=5, layer=5))
    ev.append(rr(px + 312, cy + 136, 240, 42, 21, GREEN, "&H40&", layer=4))
    ev.append(tx(px + 432, cy + 157, "✓ PATCH OUT", 22, WHITE, 1, an=5, layer=5))
    # key points popping in
    ky = cy + 236
    ev.append(tx(px + 32, ky, "KEY POINTS", 22, ACC, 1))
    pts = [("No login needed to break in", "&H00&", 0),
           ("Attacks seen since Nov 12", "&H00&", 0),
           ("Fixed in version 7.4.2", "&H60&", 24)]   # third one mid slide-in
    for i, (t, a, dx) in enumerate(pts):
        y = ky + 44 + i * 92
        ev.append(rr(px + 24 + dx, y, pw - 48 - dx, 76, 12, "&H3A2516&", a, layer=2))
        ev.append(rr(px + 24 + dx, y, 8, 76, 4, ACC, a, layer=3))
        ev.append(tx(px + 56 + dx, y + 38, t, 30, WHITE, 1, an=4, layer=4, extra=f"\\alpha{a}"))
    progress(ev, 0.36, 2)
    return ev


def frame_transition():
    ev = []
    ev.append(rr(0, 0, W, H, 0, "&H0B0705&", "&H38&", layer=0))
    # glitch slices
    for y, dx, col in ((392, -14, "&HFF00FF&"), (420, 18, ACC), (612, -10, ACC)):
        ev.append(rr(0 + dx, y, W, 6, 0, col, "&H90&", layer=1))
    ev.append(tx(W // 2 + 6, 360, "STORY 3 OF 5", 46, "&HFF00FF&", 1, an=5, layer=2, extra="\\alpha&HA0&"))
    ev.append(tx(W // 2 - 6, 360, "STORY 3 OF 5", 46, ACC, 1, an=5, layer=2, extra="\\alpha&HA0&"))
    ev.append(tx(W // 2, 360, "STORY 3 OF 5", 46, ACC, 1, an=5, layer=3))
    ev.append(rr(W // 2 - 60, 404, 120, 6, 3, ACC, layer=3))
    ev.append(tx(W // 2, 520, "Fake shipping texts now\\Nsteal bank logins in real time", 76, WHITE, 1, an=5, layer=3))
    ev.append(tx(W // 2, 680, "SCAM OF THE WEEK  ·  EXAMPLE SECURITY NEWS", 26, DIM, 1, an=5, layer=3))
    progress(ev, 0.46, 3)
    return ev


def frame_checklist():
    ev = []
    top_bar(ev, "2 / 5   Critical VPN gateway flaw exploited")
    ev.append(rr(58, 148, 1164, 657, 12, ACC, "&HFF&", layer=1, extra=f"\\bord3\\3c{ACC}\\3a&H00&"))
    ev.append(rr(84, 174, 236, 50, 10, ACC, layer=4))
    ev.append(tx(202, 199, "STORY 2 OF 5", 26, INK, 1, an=5, layer=5))
    ev.append(rr(90, 860, 1100, 92, 18, "&H000000&", "&H50&", layer=2))
    ev.append(tx(640, 906, f"so if you run one of these, {{\\1c{KEY}}}update today{{\\1c{WHITE}}}", 46, WHITE, 1,
                 an=5, layer=3))
    px, py, pw, ph = 1260, 146, 600, 862
    ev.append(rr(px, py, pw, ph, 18, PANEL, "&H10&", layer=1))
    ev.append(tx(px + 32, py + 34, "WHAT TO DO", 40, KEY, 1))
    ev.append(rr(px + 32, py + 90, 90, 6, 3, KEY, layer=2))
    items = [("Update to version 7.4.2", True), ("Check logs for unknown\\Nadmin logins since Nov 12", True),
             ("Take the admin page\\Noff the open internet", False)]
    y = py + 140
    for t, done in items:
        hgt = 120 if "\\N" in t else 84
        ev.append(rr(px + 24, y, pw - 48, hgt, 14, "&H3A2516&", layer=2))
        ev.append(rr(px + 48, y + hgt / 2 - 22, 44, 44, 10, GREEN if done else "&H5A4436&", layer=3))
        if done:
            ev.append(tx(px + 70, y + hgt / 2, "✓", 32, WHITE, 1, an=5, layer=4))
        ev.append(tx(px + 116, y + hgt / 2, t, 30, WHITE if done else GREY, 1, an=4, layer=4))
        y += hgt + 18
    ev.append(tx(px + 32, py + ph - 60, "Full sources in the description", 22, DIM, 0))
    progress(ev, 0.44, 2)
    return ev


def render(name, events, layout):
    ass = D / f"{name}.ass"
    ass.write_text(head() + "\n".join(events) + "\n", encoding="utf-8")
    out = D / f"{name}.png"
    if layout == "panel":
        graph = ("[1:v]scale=1160:653,eq=brightness=-0.08[v];[0:v][v]overlay=60:150[b];"
                 f"[b]ass={ass.name}[o]")
    else:
        graph = (f"[1:v]scale={W}:{H},gblur=sigma=14,eq=brightness=-0.25[v];[0:v][v]overlay=0:0[b];"
                 f"[b]ass={ass.name}[o]")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", "bg.png", "-i", "broll.png",
                    "-filter_complex", graph, "-map", "[o]", "-frames:v", "1", str(out)], cwd=D, check=True)
    return out


if __name__ == "__main__":
    print(render("1_story", frame_story(), "panel"))
    print(render("2_transition", frame_transition(), "full"))
    print(render("3_checklist", frame_checklist(), "panel"))
