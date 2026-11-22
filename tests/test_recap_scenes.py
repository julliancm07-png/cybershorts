import re
import unittest

from cybershorts import recap_scenes as R

TH = R.Theme()
BOX = R.Box(60, 150, 1160, 653)
W, H = 1920, 1080
S, E = 10.0, 22.0
TOL = 6.0   # px slack (borders, rounding)

DIAL = re.compile(r"Dialogue: (\d+),(\d+:\d\d:\d\d\.\d\d),(\d+:\d\d:\d\d\.\d\d),(\w+),,0,0,0,,(.*)", re.S)
NUM = r"(-?\d+(?:\.\d+)?)"


def secs(t):
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def parse(line):
    m = DIAL.match(line)
    assert m, line
    return int(m[1]), secs(m[2]), secs(m[3]), m[5]


def split_tags(body):
    """-> (tags string of the first {...} block + inline blocks, visible text)."""
    tags = "".join(re.findall(r"\{[^{}]*\}", body))
    text = re.sub(r"\{[^{}]*\}", "", body)
    return tags, text


def static_tags(tags):
    """Tags with every \\t(...) animation removed (nested parens)."""
    out, i = "", 0
    while i < len(tags):
        if tags.startswith("\\t(", i):
            depth, j = 0, i + 2
            while j < len(tags):
                depth += tags[j] == "("
                depth -= tags[j] == ")"
                j += 1
                if depth == 0:
                    break
            i = j
        else:
            out += tags[i]
            i += 1
    return out


def bbox(body):
    """Estimated (x0, y0, x1, y1) of an event, or None. Static \\clip -> intersected; \\t clips ignored."""
    tags, text = split_tags(body)
    st = static_tags(tags)
    an = int((re.search(r"\\an(\d)", st) or [0, 7])[1])
    mv = re.search(r"\\move\(" + NUM + "," + NUM + "," + NUM + "," + NUM, st)
    pos = re.search(r"\\pos\(" + NUM + "," + NUM + r"\)", st)
    if mv:
        pts = [(float(mv[1]), float(mv[2])), (float(mv[3]), float(mv[4]))]
    elif pos:
        pts = [(float(pos[1]), float(pos[2]))]
    else:
        return None
    sx = 1.0
    boxes = []
    if "\\p1" in st:
        d = text
        nums = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", d)]
        xs, ys = nums[0::2], nums[1::2]
        w, h = max(xs) - min(xs), max(ys) - min(ys)
        for px, py in pts:
            if an == 5:
                boxes.append((px - w / 2, py - h / 2, px + w / 2, py + h / 2))
            else:
                boxes.append((px, py, px + w, py + h))
    else:
        size = float((re.search(r"\\fs(\d+)", st) or [0, 40])[1])
        font = (re.search(r"\\fn([^\\}]+)", st) or [0, ""])[1]
        mono = "Mono" in font
        lines = text.split("\\N")
        wd = max(R.est_w(l, size, 0.602 if mono else 0.60, 1.0 if mono else 1.15) for l in lines) * sx
        ht = len(lines) * size * R.LINE_H
        col = (an - 1) % 3          # 0 left, 1 centre, 2 right
        row = (an - 1) // 3         # 0 bottom, 1 middle, 2 top
        for px, py in pts:
            x0 = px - (0, wd / 2, wd)[col]
            y0 = py - (ht, ht / 2, 0)[row]
            boxes.append((x0, y0, x0 + wd, y0 + ht))
    clip = re.search(r"\\clip\(" + NUM + "," + NUM + "," + NUM + "," + NUM + r"\)", st)
    out = []
    for b in boxes:
        if clip:
            c = [float(clip[i]) for i in range(1, 5)]
            b = (max(b[0], c[0]), max(b[1], c[1]), min(b[2], c[2]), min(b[3], c[3]))
        out.append(b)
    return (min(b[0] for b in out), min(b[1] for b in out), max(b[2] for b in out), max(b[3] for b in out))


def anim_clips(body):
    return [tuple(float(x) for x in m) for m in
            re.findall(r"\\clip\(" + NUM + "," + NUM + "," + NUM + "," + NUM + r"\)", body)]


def check(tc, lines, box, s, e, tol=TOL):
    tc.assertTrue(lines)
    for ln in lines:
        layer, a, b, body = parse(ln)
        tc.assertGreaterEqual(a, s - 0.011, ln)
        tc.assertLessEqual(b, e + 0.011, ln)
        tc.assertGreater(b - a, 0.005, ln)
        bb = bbox(body)
        tc.assertIsNotNone(bb, ln)
        x0, y0, x1, y1 = bb
        tc.assertGreaterEqual(x0, box[0] - tol, ln)
        tc.assertGreaterEqual(y0, box[1] - tol, ln)
        tc.assertLessEqual(x1, box[2] + tol, ln)
        tc.assertLessEqual(y1, box[3] + tol, ln)
        for c in anim_clips(body):
            tc.assertGreaterEqual(c[0], box[0] - tol, ln)
            tc.assertLessEqual(c[2], box[2] + tol, ln)


def rect(b):
    return (b.x, b.y, b.x2, b.y2)


LONG = "Extraordinarily long supercalifragilistic text that just keeps going on and on and on without end"

SCENES = {
    "terminal": {"title": "Exploit demo", "lines": [
        {"kind": "cmd", "text": "scan --target vpn-gateway --port 443"},
        {"kind": "out", "text": "found: VPN gateway 7.4.1 (vulnerable)"},
        {"kind": "cmd", "text": "exploit CVE-2026-12345 --no-auth"},
        {"kind": "alert", "text": "remote shell ........ OPEN"},
        {"kind": "out", "text": "root"}]},
    "flow": {"title": "How the attack works", "nodes": [
        {"label": "Attacker", "sub": "anywhere online"}, {"label": "VPN gateway", "sub": "version 7.4.1"},
        {"label": "Inside", "sub": "files, email, servers"}], "arrows": ["no login", "full access"],
        "fixed_in": "7.4.2"},
    "stat": {"value": "$2.3 million", "label": "stolen from small businesses", "context": "Reported by the FBI"},
    "timeline": {"events": [{"date": "Nov 12", "label": "First attacks seen"},
                            {"date": "Nov 14", "label": "Vendor confirms"}, {"date": "Nov 20", "label": "Patch out"}]},
    "scam": {"channel": "sms", "sender": "USPS Delivery",
             "body": "Your package is held. Pay a small fee at {link} today.",
             "flags": ["Unknown number", "Urgent deadline", "Asks for a fee"]},
    "headline": {"title": "Fake shipping texts now steal bank logins", "source": "Example Security News"},
    "cve": {"card": {"cve_id": "CVE-2026-12345", "cvss": 9.8, "severity": "Critical", "products": ["Acme VPN"],
                     "exploited": True, "patch_available": True}},
}

OVERLONG = {
    "terminal": {"title": LONG, "lines": [{"kind": k, "text": LONG} for k in ("cmd", "out", "alert") * 5]},
    "flow": {"title": LONG, "nodes": [{"label": LONG, "sub": LONG}] * 4, "arrows": [LONG] * 3, "fixed_in": LONG},
    "stat": {"value": "$2,300,000,000,000 million", "label": LONG * 2, "context": LONG * 2},
    "timeline": {"events": [{"date": LONG, "label": LONG}] * 4, "title": LONG},
    "scam": {"channel": "sms", "sender": LONG, "body": LONG * 3 + " {link}", "flags": [LONG] * 3},
    "headline": {"title": LONG * 4, "source": LONG},
    "cve": {"card": {"cve_id": "CVE-2026-1234567890123456", "cvss": 10, "severity": "Critical",
                     "products": [LONG], "exploited": True, "patch_available": True}},
}


class Primitives(unittest.TestCase):
    def test_ev_clamps_and_drops(self):
        self.assertIsNone(R.ev(1, 5, 5, "x"))
        self.assertIsNone(R.ev(1, 5, 6, ""))
        self.assertIsNone(R.ev(1, 5, 6, "   "))
        self.assertIsNone(R.ev(1, 1, 2, "x", lo=5, hi=9))          # entirely before the window
        self.assertIsNone(R.ev(1, 20, 30, "x", lo=5, hi=9))        # entirely after
        a, b = parse(R.ev(1, 1, 30, "x", lo=5, hi=9))[1:3]
        self.assertEqual((a, b), (5.0, 9.0))

    def test_rr_tx_events(self):
        self.assertIn("\\p1", R.rr(0, 0, 100, 40, 10, TH.card, 0, 1))
        self.assertIsNone(R.rr(0, 0, 100, 40, 10, TH.card, 2, 1))
        self.assertIsNone(R.tx(0, 0, "", 30, TH.white, 0, 1))

    def test_fit_text(self):
        lines, size = R.fit_text("hello world", 1000, 40)
        self.assertEqual((lines, size), (["hello world"], 40))
        lines, size = R.fit_text("one two three four five six seven", 200, 40, min_size=20, max_lines=2)
        self.assertLessEqual(len(lines), 2)
        for l in lines:
            self.assertLessEqual(R.est_w(l, size), 200 + 1)
        lines, size = R.fit_text(LONG * 3, 300, 30, min_size=20, max_lines=2)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[-1].endswith("\u2026"))
        self.assertLessEqual(R.est_w(lines[-1], size), 301)
        lines, size = R.fit_text("A" * 80, 200, 30, min_size=30)           # unbreakable word is hard-broken
        self.assertTrue(all(R.est_w(l, size) <= 200 for l in lines[:1]))
        self.assertEqual(R.fit_text("", 100, 30)[0], [])

    def test_fit_prefers_shrink_over_mid_word_break(self):
        lines, size = R.fit_text("Developer laptop", 166, 32, min_size=20, max_lines=2)
        self.assertEqual(lines, ["Developer", "laptop"])
        self.assertLess(size, 32)

    def test_theme(self):
        class C:
            def get(self, k, d=None):
                return {"branding.accent": "#112233", "captions.font": "Foo",
                        "recap.newsroom.panel_color": "#0F1626"}.get(k, d)
        th = R.theme(C())
        self.assertEqual(th.accent, "&H332211&")
        self.assertEqual(th.font, "Foo")
        self.assertEqual(th.panel, "&H26160F&")
        self.assertEqual(th.mono, "DejaVu Sans Mono")
        self.assertEqual(R.theme(None).accent, "&HFFDC3D&")
        self.assertEqual(R.theme(None).kw, "&H0AD6FF&")


class Builders(unittest.TestCase):
    def test_all_types_registered(self):
        self.assertEqual(set(R.BUILDERS), set(SCENES))

    def test_events_within_time_and_box(self):
        for name, fn in R.BUILDERS.items():
            with self.subTest(name):
                check(self, fn(SCENES[name], BOX, S, E, TH), rect(BOX), S, E)

    def test_overlong_inputs_fit(self):
        for name, fn in R.BUILDERS.items():
            with self.subTest(name):
                check(self, fn(OVERLONG[name], BOX, S, E, TH), rect(BOX), S, E)

    def test_short_scene_window(self):
        for name, fn in R.BUILDERS.items():
            with self.subTest(name):
                check(self, fn(SCENES[name], BOX, S, S + 1.5, TH), rect(BOX), S, S + 1.5)

    def test_event_counts(self):
        counts = {n: len(fn(SCENES[n], BOX, S, E, TH)) for n, fn in R.BUILDERS.items()}
        self.assertLessEqual(counts["stat"], 20, counts)
        self.assertLessEqual(counts["terminal"], 30, counts)
        self.assertLessEqual(len(R.terminal(OVERLONG["terminal"], BOX, S, E, TH)), 30)
        for n, c in counts.items():
            self.assertLessEqual(c, 40, (n, counts))
            self.assertGreaterEqual(c, 2, (n, counts))

    def test_invalid_scenes_yield_nothing_or_fit(self):
        self.assertEqual(R.flow({"nodes": [{"label": "a"}]}, BOX, S, E, TH), [])
        self.assertEqual(R.timeline({"events": [{"date": "x", "label": "y"}]}, BOX, S, E, TH), [])
        self.assertEqual(R.stat({}, BOX, S, E, TH), [])
        self.assertEqual(R.scam({}, BOX, S, E, TH), [])
        self.assertEqual(R.cve({}, BOX, S, E, TH), [])

    def test_terminal_semantics(self):
        lines = R.terminal(SCENES["terminal"], BOX, S, E, TH)
        joined = "\n".join(lines)
        self.assertIn("DRAMATIZATION", joined)
        self.assertIn("$ scan --target", joined)
        self.assertIn("\\clip(", joined)
        self.assertIn("\\t(", joined)
        self.assertIn("\u2588", joined)                      # cursor
        starts = [parse(l)[1] for l in lines if "$ " in l or "found" in l]
        self.assertEqual(starts, sorted(starts))

    def test_flow_semantics(self):
        lines = R.flow(SCENES["flow"], BOX, S, E, TH)
        joined = "\n".join(lines)
        self.assertIn("\\fscx", joined)
        self.assertIn("Fixed in 7.4.2", joined)
        badge = [parse(l)[1] for l in lines if "Fixed in" in l][0]
        self.assertGreaterEqual(badge, max(parse(l)[1] for l in lines if "full access" in l))
        nofix = dict(SCENES["flow"], fixed_in=None)
        self.assertNotIn("Fixed in", "\n".join(R.flow(nofix, BOX, S, E, TH)))

    def test_stat_count_up_keeps_format(self):
        self.assertEqual(R.count_values("4,000", 6)[-1], "4,000")
        self.assertTrue(all("," in v or len(v) < 4 for v in R.count_values("4,000", 8)))
        v = R.count_values("70%", 6)
        self.assertTrue(all(x.endswith("%") for x in v))
        self.assertEqual(v[-1], "70%")
        v = R.count_values("$2.3 million", 8)
        self.assertTrue(all(x.startswith("$") and x.endswith(" million") for x in v), v)
        self.assertEqual(v[-1], "$2.3 million")
        self.assertEqual(R.count_values("zero", 8), ["zero"])
        self.assertEqual(R.count_values("3 to 5 days", 8), ["3 to 5 days"])
        self.assertLessEqual(len(R.count_values("1,000,000", 14)), 14)

    def test_stat_events_value_sequence(self):
        lines = R.stat({"value": "4,000", "label": "x"}, BOX, S, E, TH)
        vals = [re.sub(r"\{[^}]*\}", "", parse(l)[3]) for l in lines if "\\fs190" in l or "\\fs19" in l]
        self.assertIn("4,000", vals[-1])
        # the final value stays until the end of the scene
        self.assertAlmostEqual(max(parse(l)[2] for l in lines), E, places=1)

    def test_scam_semantics(self):
        lines = R.scam(SCENES["scam"], BOX, S, E, TH)
        joined = "\n".join(lines)
        self.assertIn("EXAMPLE SCAM MESSAGE", joined)
        self.assertIn(R.BAR, joined)
        self.assertNotIn("{link}", joined)
        flag_starts = [parse(l)[1] for l in lines for f in SCENES["scam"]["flags"] if f in l]
        self.assertEqual(flag_starts, sorted(flag_starts))
        self.assertEqual(len(set(flag_starts)), 3)
        em = R.scam({"channel": "email", "sender": "a@b.example", "subject": "Hi", "body": "Go to {link}"},
                    BOX, S, E, TH)
        self.assertIn("Subject: Hi", "\n".join(em))
        check(self, em, rect(BOX), S, E)

    def test_cve_gauge(self):
        lines = R.cve(SCENES["cve"], BOX, S, E, TH)
        joined = "\n".join(lines)
        self.assertIn("CVE-2026-12345", joined)
        self.assertIn("9.8", joined)
        self.assertLessEqual(len(lines), 30)
        self.assertEqual(R.cve({"card": {"cve_id": "CVE-2026-1"}}, BOX, S, E, TH) != [], True)

    def test_escaping(self):
        evil = "a{b}c\\d{\\fs200}"
        sc = {"terminal": {"title": evil, "lines": [{"kind": "cmd", "text": evil}] * 3},
              "flow": {"title": evil, "nodes": [{"label": evil, "sub": evil}] * 2, "arrows": [evil],
                       "fixed_in": evil},
              "stat": {"value": "5", "label": evil, "context": evil},
              "timeline": {"title": evil, "events": [{"date": evil, "label": evil}] * 2},
              "scam": {"channel": "email", "sender": evil, "subject": evil, "body": evil + " {link}",
                       "flags": [evil]},
              "headline": {"title": evil, "source": evil},
              "cve": {"card": {"cve_id": evil, "cvss": 5, "products": [evil]}}}
        for name, fn in R.BUILDERS.items():
            with self.subTest(name):
                lines = fn(sc[name], BOX, S, E, TH)
                self.assertTrue(lines)
                for ln in lines:
                    body = parse(ln)[3]
                    self.assertNotIn("\\fs200", body.replace("\\\\fs200", ""))
                    # visible text carries no braces or single backslashes
                    _, text = split_tags(body)
                    text = text.replace("\\N", "").replace("\\\u2060", "")
                    if "\\p1" not in body:
                        self.assertNotIn("\\", text)
                        self.assertNotIn("{", text)
                        self.assertNotIn("}", text)
        self.assertEqual(R.tx(0, 0, evil, 30, TH.white, 0, 1).count("{"), 1)


class Panel(unittest.TestCase):
    X, Y, PW = 1260, 330, 600

    def test_cve_card(self):
        lines, h = R.cve_card(SCENES["cve"]["card"], self.X, self.Y, self.PW, S, E, TH)
        self.assertGreater(h, 100)
        check(self, lines, (self.X, self.Y, self.X + self.PW, self.Y + h), S, E)
        self.assertIn("\\clip(", "\n".join(lines))
        l2, h2 = R.cve_card({"cve_id": "CVE-2026-1"}, self.X, self.Y, self.PW, S, E, TH)
        self.assertLess(h2, h)
        check(self, l2, (self.X, self.Y, self.X + self.PW, self.Y + h2), S, E)
        self.assertEqual(R.cve_card({}, self.X, self.Y, self.PW, S, E, TH), ([], 0))
        lo, ho = R.cve_card(OVERLONG["cve"]["card"], self.X, self.Y, self.PW, S, E, TH)
        check(self, lo, (self.X, self.Y, self.X + self.PW, self.Y + ho), S, E)

    def test_key_point(self):
        for text in ("No login needed to break in", LONG, "x", "A" * 60):
            with self.subTest(text):
                lines, h = R.key_point(text, self.X, self.Y, self.PW, S, S + 6, TH)
                self.assertIn(h, (76, 112))
                check(self, lines, (self.X, self.Y, self.X + self.PW, self.Y + h), S, S + 6)
                self.assertIn("\\move(", "\n".join(lines))
                self.assertIn("\\fad(", "\n".join(lines))
        self.assertEqual(R.key_point("", self.X, self.Y, self.PW, S, E, TH), ([], 0))

    def test_checklist(self):
        items = ["Update to version 7.4.2", "Check logs for unknown admin logins since Nov 12", LONG]
        lines, h = R.checklist(items, [S + 3, S + 5, S + 7], 1260, 146, 600, S, E, TH)
        check(self, lines, (1260, 146, 1860, 146 + h + 8), S, E)
        self.assertLessEqual(146 + h, 1008)
        joined = "\n".join(lines)
        self.assertIn("WHAT TO DO", joined)
        self.assertIn("\u2713", joined)
        ticks = sorted(parse(l)[1] for l in lines if "\u2713" in l)
        self.assertEqual(len(ticks), 3)
        self.assertAlmostEqual(ticks[0], S + 3, delta=0.02)
        # fewer times than items: the rest never tick
        l2, _ = R.checklist(items, [S + 3], 1260, 146, 600, S, E, TH)
        self.assertEqual(sum("\u2713" in l for l in l2), 1)
        self.assertEqual(R.checklist([], [], 1260, 146, 600, S, E, TH), ([], 0))
        self.assertEqual(R.checklist([{"text": "do it"}], [], 1260, 146, 600, S, E, TH)[1] > 0, True)


class FullScreen(unittest.TestCase):
    def test_transition(self):
        s, e = 30.0, 33.0
        lines = R.transition(3, 5, "Fake shipping texts now steal bank logins in real time",
                             "Scam of the week \u00b7 Example", s, e, W, H, TH)
        check(self, lines, (0, 0, W, H), s, e, tol=2)
        joined = "\n".join(lines)
        self.assertIn("STORY 3 OF 5", joined)
        self.assertIn("Fake shipping", joined)
        # jitter ghosts end within 400 ms of the start, the settled headline lasts the whole card
        ghosts = [parse(l) for l in lines if "\\alpha&HA0&" in l]
        self.assertTrue(ghosts)
        self.assertLessEqual(max(g[2] for g in ghosts), s + 0.41)
        self.assertEqual(min(parse(l)[1] for l in lines), s)
        main = [parse(l) for l in lines if "Fake shipping" in l][0]
        self.assertEqual((main[1], main[2]), (s, e))
        # very short card and overlong text still fit
        check(self, R.transition(1, 1, LONG * 3, LONG, s, s + 0.2, W, H, TH), (0, 0, W, H), s, s + 0.2, tol=2)
        check(self, R.transition(1, 9, LONG * 3, LONG, s, e, 1280, 720, TH), (0, 0, 1280, 720), s, e, tol=2)

    def test_intro(self):
        titles = ["Critical VPN gateway flaw exploited", "Fake shipping texts steal bank logins",
                  "Hospital hit by ransomware", "Browser update fixes zero-day"]
        s, e = 0.0, 12.0
        lines = R.intro_card("This week in cyber", "Week 3", titles, s, e, W, H, TH)
        check(self, lines, (0, 0, W, H), s, e, tol=2)
        joined = "\n".join(lines)
        self.assertIn("THIS WEEK IN CYBER", joined)
        self.assertIn("WEEK 3", joined)
        starts = [parse(l)[1] for t in titles for l in lines if t in l]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(len(set(starts)), 4)
        many = R.intro_card(LONG, LONG, [LONG] * 20, s, 5.0, W, H, TH)
        check(self, many, (0, 0, W, H), s, 5.0, tol=2)
        self.assertLessEqual(len(many), 40)

    def test_outro(self):
        lines = R.outro_card("@yourhandle", 100.0, 108.0, W, H, TH)
        check(self, lines, (0, 0, W, H), 100.0, 108.0, tol=2)
        joined = "\n".join(lines)
        for needle in ("SUBSCRIBE", "@yourhandle", "Full sources in the description"):
            self.assertIn(needle, joined)
        check(self, R.outro_card(LONG * 3, 0, 3, W, H, TH), (0, 0, W, H), 0, 3, tol=2)


# --- review round -----------------------------------------------------------------------------
EVIL_TEXTS = ["C:\\new\\Nline (\\pos(10,10))", "a\\N b \\n c \\h d {\\fs200} \\", "line\nbreak\r\nhere",
              "rtl \u202eevil\u202c \u2066x\u2069 \u200b zw \x00\x1b ctl \u2028"]


class Escape(unittest.TestCase):
    def test_no_escape_sequences_survive(self):
        for t in EVIL_TEXTS:
            out = R._esc(t)
            self.assertNotRegex(out, r"\\[Nnh]")           # a backslash is never followed by N, n or h
            self.assertNotRegex(out, r"\\(?!\u2060)")       # every backslash is followed by the word joiner
            self.assertNotIn("{", out)
            self.assertNotIn("}", out)
            self.assertNotIn("\n", out)
            for ch in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200b\u2028\x00\x1b\r":
                self.assertNotIn(ch, out)

    def test_newlines_only_where_intended(self):
        self.assertEqual(R._esc("a\nb"), "a b")
        self.assertEqual(R._esc(R._join(["a", "b"])), "a\\Nb")
        body = parse(R.tx(10, 10, "one\ntwo \\N three", 30, "&HFFFFFF&", 0, 1))[3]
        self.assertEqual(body.count("\\N"), 0)
        body = parse(R.tx(10, 10, R._join(["one", "two"]), 30, "&HFFFFFF&", 0, 1))[3]
        self.assertEqual(body.count("\\N"), 1)

    def test_captions_escape_untouched(self):
        from cybershorts import captions
        self.assertEqual(captions._escape("a\\b\nc"), "a\\\\b\\Nc")      # Shorts keep their own behaviour

    def test_libass_renders_one_line(self):
        import shutil
        import subprocess
        import tempfile
        from pathlib import Path

        if not shutil.which("ffmpeg"):
            self.skipTest("no ffmpeg")
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("no PIL")
        from cybershorts import recap_newsroom as N
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            heights = {}
            for name, text in (("evil", "C:\\new\\Nline (\\pos(10,10)) \\h x"), ("plain", "C:/new/line (pos) h x")):
                (d / f"{name}.ass").write_text(
                    N._header("DejaVu Sans") + R.tx(60, 100, text, 40, "&HFFFFFF&", 0, 1) + "\n", encoding="utf-8")
                r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                                    "color=c=black:s=1920x1080:d=1", "-vf", f"ass={name}.ass", "-frames:v", "1",
                                    f"{name}.png"], cwd=d, capture_output=True, text=True)
                if r.returncode:
                    self.skipTest("ffmpeg has no ass filter")
                im = Image.open(d / f"{name}.png").convert("L").point(lambda v: 255 if v > 60 else 0)
                box = im.getbbox()
                heights[name] = box[3] - box[1]
                self.assertGreater(box[2] - box[0], 200)
            self.assertLessEqual(abs(heights["evil"] - heights["plain"]), 12)     # one line, no \N break
            self.assertLess(heights["evil"], 60)


class ReviewFixes(unittest.TestCase):
    def test_flow_long_word_is_not_split(self):
        sc = {"title": "Chain", "nodes": [{"label": "Authentication", "sub": "Authentication"},
                                          {"label": "Authorization", "sub": "x"}, {"label": "Gateway", "sub": ""},
                                          {"label": "Infrastructure", "sub": "y"}], "arrows": ["a", "b", "c"]}
        lines = R.flow(sc, BOX, S, E, TH)
        texts = [split_tags(parse(l)[3])[1] for l in lines if "\\p1" not in l]
        for word in ("Authentication", "Authorization", "Infrastructure"):
            hits = [t for t in texts if t.startswith(word[:6])]
            self.assertTrue(hits, word)
            for t in hits:
                self.assertNotIn("\\N", t, t)                  # one line: never split mid-word
                self.assertTrue(t == word or t.endswith("\u2026"), t)
        lab, sz = R.fit_text("Authentication", 100, 32, min_size=16, split_words=False)
        self.assertEqual(len(lab), 1)
        self.assertTrue(lab[0].endswith("\u2026") or lab[0] == "Authentication")
        lab, sz = R.fit_text("Authentication", 166, 32, min_size=16, factor=0.66, split_words=False)
        self.assertEqual(lab, ["Authentication"])
        # default behaviour (headline text) still hard-splits
        self.assertGreater(len(R.fit_text("A" * 80, 300, 30, min_size=30, max_lines=3)[0]), 1)

    def test_intro_rows_stay_above_caption_box(self):
        titles = ["Story %d has a fairly long headline about things" % i for i in range(1, 9)]
        for show, week, tl in (("This week in cyber", "Week 3", titles),
                               ("A very long show title that needs two whole lines to fit " * 2, "Week 12", titles),
                               ("This week in cyber", "Week 3", titles[:7]),
                               ("This week in cyber", "Week 3", titles[:2])):
            lines = R.intro_card(show, week, tl, 0.0, 14.0, W, H, TH)
            rows = [parse(l) for l in lines if "\\fs" in l and ("\\fn" in l) and "Story " in l]
            self.assertTrue(rows)
            for _, _a, _b, body in rows:
                y = float(re.search(r"\\pos\(\d+,(\d+)\)", body)[1])
                fs = float(re.search(r"\\fs(\d+)", body)[1])
                self.assertLessEqual(y + fs * R.LINE_H, 860 - 20 + 1, body)
            ys = sorted(float(re.search(r"\\pos\(\d+,(\d+)\)", r[3])[1]) for r in rows)
            fss = [float(re.search(r"\\fs(\d+)", r[3])[1]) for r in rows]
            for a, b in zip(ys, ys[1:]):
                if b > a:
                    self.assertGreaterEqual(b - a, min(fss) * 1.0)       # rows do not overlap each other
        self.assertEqual(len([l for l in R.intro_card("T", "W", titles, 0, 14, W, H, TH) if "Story 8" in l]) > 0, True)

    def test_slide_ins_keep_borders_inside_clip(self):
        lines, h = R.key_point("No login needed to break in", 1260, 400, 600, S, S + 6, TH)
        for l in lines:
            body = parse(l)[3]
            mv = re.search(r"\\move\(" + NUM + "," + NUM + "," + NUM + "," + NUM, body)
            clip = anim_clips(body)[0]
            self.assertLess(float(mv[1]), float(mv[3]))                # slides in from the left
            self.assertLessEqual(clip[0], float(mv[1]) + 0.5)          # clip covers the start of the travel
            bb = bbox(body.replace("\\clip", "\\xclip"))
            self.assertLessEqual(bb[2], clip[2] + 0.5)                 # and the final right edge
        sc = SCENES["scam"]
        for l in R.scam(sc, BOX, S, E, TH):
            body = parse(l)[3]
            if "\\move(" in body and "\\clip(" in body:
                mv = re.search(r"\\move\(" + NUM + "," + NUM + "," + NUM + "," + NUM, body)
                clip = anim_clips(body)[0]
                self.assertLess(float(mv[1]), float(mv[3]))
                self.assertLessEqual(clip[0], float(mv[1]) + 0.5)
                self.assertLessEqual(bbox(body.replace("\\clip", "\\xclip"))[2], clip[2] + 0.5)

    def test_dramatization_label_readable(self):
        lines = R.terminal(SCENES["terminal"], BOX, S, E, TH)
        lab = [l for l in lines if "DRAMATIZATION" in l]
        self.assertEqual(len(lab), 1)
        body = parse(lab[0])[3]
        self.assertTrue(24 <= int(re.search(r"\\fs(\d+)", body)[1]) <= 26)
        self.assertTrue(f"\\1c{TH.grey}" in body or f"\\1c{TH.white}" in body)

    def test_full_screen_cards_are_opaque(self):
        for lines in (R.transition(1, 3, "Headline", "tag", S, E, W, H, TH),
                      R.intro_card("Show", "Week 1", ["a b c"], S, E, W, H, TH),
                      R.outro_card("@h", S, E, W, H, TH)):
            first = parse(lines[0])[3]
            self.assertIn("\\1a&H00&", first)
            self.assertIn("m 0 0", first)
            self.assertEqual(parse(lines[0])[0], 20)

    def test_unused_params_removed(self):
        import inspect
        self.assertNotIn("handle", inspect.signature(R.intro_card).parameters)
        self.assertEqual(list(inspect.signature(R._glitch_rects).parameters),
                         ["W", "dx", "col", "t0", "t1", "s", "e", "y"])


if __name__ == "__main__":
    unittest.main()
