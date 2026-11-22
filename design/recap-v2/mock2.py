import subprocess
from mock import rr, tx, head, top_bar, progress, ACC, KEY, RED, GREEN, WHITE, GREY, DIM, INK, PANEL, D, W, H

MONO = "DejaVu Sans Mono"
GRN = "&H7AE05A&"  # terminal green #5AE07A

def right_panel(ev):
    px, py, pw, ph = 1260, 146, 600, 862
    ev.append(rr(px, py, pw, ph, 18, PANEL, "&H10&", layer=1))
    ev.append(tx(px + 32, py + 30, "SOURCE  ·  EXAMPLE SECURITY NEWS", 22, DIM, 1))
    ev.append(tx(px + 32, py + 70, "Critical VPN gateway flaw\\Nexploited before a patch", 38, WHITE, 1))
    ky = py + 210
    ev.append(tx(px + 32, ky, "KEY POINTS", 22, ACC, 1))
    for i, t in enumerate(["No login needed to break in", "Attacks seen since Nov 12", "Fixed in version 7.4.2"]):
        y = ky + 44 + i * 92
        ev.append(rr(px + 24, y, pw - 48, 76, 12, "&H3A2516&", layer=2))
        ev.append(rr(px + 24, y, 8, 76, 4, ACC, layer=3))
        ev.append(tx(px + 56, y + 38, t, 30, WHITE, 1, an=4, layer=4))

def frame(ev):
    top_bar(ev, "2 / 5   Critical VPN gateway flaw exploited")
    ev.append(rr(58, 148, 1164, 657, 12, "&H1A0F0A&", "&H00&", layer=1, extra=f"\\bord3\\3c{ACC}\\3a&H00&"))
    ev.append(rr(84, 174, 236, 50, 10, ACC, layer=4))
    ev.append(tx(202, 199, "STORY 2 OF 5", 26, INK, 1, an=5, layer=5))
    ev.append(rr(90, 860, 1100, 92, 18, "&H000000&", "&H50&", layer=2))

def terminal():
    ev = []; frame(ev); right_panel(ev)
    x, y = 110, 260
    ev.append(rr(100, 240, 1080, 520, 12, "&H0A0604&", layer=2))
    for i, c in enumerate(("&H5F57FF&", "&H2EBDFE&", "&H40C828&")):
        ev.append(rr(124 + i * 30, 258, 16, 16, 8, c, layer=3))
    ev.append(tx(640, 266, "attacker@remote — bash", 20, DIM, 0, an=5, layer=3))
    lines = [(GRN, "$ scan --target vpn-gateway --port 443"),
             (GREY, "  found: VPN gateway 7.4.1   (vulnerable)"),
             (GRN, "$ exploit CVE-2026-12345 --no-auth"),
             (GREY, "  login check ......... skipped"),
             (RED,  "  remote shell ........ OPEN"),
             (GRN, "$ whoami"),
             (WHITE, "  root"),
             (GRN, "$ █")]
    for i, (c, t) in enumerate(lines):
        ev.append(tx(x + 24, y + 50 + i * 54, t, 32, c, 0, font=MONO, layer=4))
    ev.append(tx(640, 906, f"attackers skip the login and get {{\\1c{KEY}}}full control{{\\1c{WHITE}}}", 46, WHITE, 1, an=5, layer=3))
    progress(ev, 0.33, 2)
    return ev

def node(ev, cx, cy, title, sub, col):
    ev.append(rr(cx - 130, cy - 70, 260, 140, 16, "&H3A2516&", layer=3, extra=f"\\bord3\\3c{col}\\3a&H00&"))
    ev.append(tx(cx, cy - 20, title, 32, WHITE, 1, an=5, layer=4))
    ev.append(tx(cx, cy + 28, sub, 22, GREY, 0, an=5, layer=4))

def arrow(ev, x1, x2, y, label, col, alpha="&H00&"):
    ev.append(rr(x1, y - 3, x2 - x1 - 18, 6, 3, col, alpha, layer=3))
    ev.append(f"Dialogue: 3,0:00:00.00,0:00:05.00,Default,,0,0,0,,{{\\an7\\pos({x2 - 22},{y - 14})\\bord0\\shad0\\1c{col}\\1a{alpha}\\p1}}m 0 0 l 22 14 l 0 28{{\\p0}}")
    ev.append(tx((x1 + x2) // 2, y - 40, label, 24, col, 1, an=5, layer=4, extra=f"\\alpha{alpha}"))

def flow():
    ev = []; frame(ev); right_panel(ev)
    ev.append(tx(640, 250, "HOW THE ATTACK WORKS", 30, ACC, 1, an=5, layer=4))
    y = 470
    node(ev, 230, y, "Attacker", "anywhere online", RED)
    node(ev, 640, y, "VPN gateway", "version 7.4.1", ACC)
    node(ev, 1050, y, "Inside", "files, email, servers", "&H5A4436&")
    arrow(ev, 368, 502, y, "no login", RED)
    arrow(ev, 778, 912, y, "full access", RED, "&H70&")  # mid-draw
    ev.append(rr(490, 640, 300, 64, 32, GREEN, "&H30&", layer=3))
    ev.append(tx(640, 672, "✓ Fixed in 7.4.2", 28, WHITE, 1, an=5, layer=4))
    ev.append(tx(640, 906, f"from there they can reach {{\\1c{KEY}}}everything inside{{\\1c{WHITE}}}", 46, WHITE, 1, an=5, layer=3))
    progress(ev, 0.38, 2)
    return ev

def render(name, events):
    ass = D / f"{name}.ass"
    ass.write_text(head() + "\n".join(events) + "\n", encoding="utf-8")
    out = D / f"{name}.png"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", "bg.png", "-vf", f"ass={ass.name}",
                    "-frames:v", "1", str(out)], cwd=D, check=True)
    print(out)

render("4_terminal", terminal())
render("5_flow", flow())
