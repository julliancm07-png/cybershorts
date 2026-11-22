import copy
import re
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from cybershorts import config, sound
from cybershorts import recap_newsroom as N
from cybershorts import recap_scenes as S
from cybershorts.captions import chunk_words, format_ass_time

TOL = 0.011
DIAL = re.compile(r"Dialogue: (\d+),(\d+:\d\d:\d\d\.\d\d),(\d+:\d\d:\d\d\.\d\d),(\w+),,0,0,0,,(.*)", re.S)


def secs(t):
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def events(ass):
    out = []
    for ln in ass.splitlines():
        if ln.startswith("Dialogue:"):
            m = DIAL.match(ln)
            assert m, ln
            assert len(ln.split(",", 9)) == 10
            out.append((int(m[1]), secs(m[2]), secs(m[3]), m[5]))
    return out


def make_cfg(**over):
    cfg = config.Config(Path(tempfile.mkdtemp()), copy.deepcopy(config.DEFAULTS), {})
    for k, v in over.items():
        cfg.raw["recap"]["newsroom"][k] = v
    return cfg


# ------------------------------------------------------------------ synthetic fixture
def make_fixture(n_stories=5, paras=(4, 4, 4, 4, 4), words_per_para=53, step=0.4, with_visuals=True,
                 with_card=True):
    """Words w0 w1 ... aligned 1:1 to the script; parts = intro, stories, outro."""
    counts = [2] + list(paras[:n_stories]) + [1]
    script_paras, words, t, k = [], [], 0.0, 0
    para_words = []            # per paragraph: (first, last+1) word idx
    for c in counts:
        for _ in range(c):
            a = k
            toks = []
            for j in range(words_per_para):
                tok = f"w{k}" + ("." if j == words_per_para - 1 else "")
                toks.append(tok)
                words.append((t, t + step * 0.9, tok))
                t += step
                k += 1
            script_paras.append(" ".join(toks))
            para_words.append((a, k))
    script = "\n".join(script_paras)
    total = t + 1.0
    starts, pi = [], 0
    for c in counts:
        starts.append(words[para_words[pi][0]][0])
        pi += c
    starts[0] = 0.0
    parts = []
    for i, st in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else total
        kind = "intro" if i == 0 else ("outro" if i == len(starts) - 1 else "story")
        parts.append({"kind": kind, "start": st, "end": end,
                      "title": {"intro": "Intro", "outro": "Subscribe"}.get(kind, f"Story {i} headline"),
                      "index": i if kind == "story" else 0, "total": n_stories})
    stories = [{"title": f"Story {i} has a long headline about a flaw", "source": "Example News",
                "url": f"https://www.example.com/{i}"} for i in range(1, n_stories + 1)]
    visuals, cards = [], []
    pstart = 1
    for i in range(1, n_stories + 1):
        base = sum(counts[:i])           # first paragraph index of story i
        def cue(p, off=2):
            a, _ = para_words[base + p - 1]
            return " ".join(f"w{a + off + q}" for q in range(4))
        if with_visuals:
            visuals.append({
                "keywords": [f"w{para_words[base + 1][0] + 3}"],
                "key_points": [{"text": f"Point {q} text", "para": q + 1, "cue": cue(q + 1)} for q in range(3)],
                "checklist": [{"text": "Update now", "para": counts[i] - 1, "cue": cue(counts[i] - 1)},
                              {"text": "Check logs", "para": counts[i], "cue": cue(counts[i], 6)}],
                "scenes": [
                    {"type": "terminal", "para": 1, "cue": cue(1), "title": "DRAMATIZATION",
                     "lines": [{"kind": "cmd", "text": "ls"}, {"kind": "out", "text": "file"},
                               {"kind": "alert", "text": "boom"}]},
                    {"type": "stat", "para": 2, "cue": cue(2), "value": "4,000", "label": "servers"},
                ],
                "dropped": [],
            })
        else:
            visuals.append({})
        cards.append({"cve_id": "CVE-2026-1111", "cvss": 9.8, "severity": "critical", "exploited": True,
                      "patch_available": True} if with_card and i % 2 else None)
        pstart += 1
    return {"words": words, "script": script, "para_counts": counts, "parts": parts, "total": total,
            "stories": stories, "visuals": visuals, "cards": cards,
            "chunks": chunk_words(words, 6)}


def build(fx, cfg=None, **kw):
    cfg = cfg or make_cfg()
    return N.build_newsroom_ass(cfg, fx["words"], fx["chunks"], fx["parts"], fx["total"], stories=fx["stories"],
                                visuals=kw.get("visuals", fx["visuals"]), cards=fx["cards"], script=fx["script"],
                                para_counts=fx["para_counts"], week_label="Week 47", handle="@cyber")


FX = make_fixture()
ASS = build(FX)
EVS = events(ASS)


# ------------------------------------------------------------------ schedule
def part(start=100.0, end=190.0, **kw):
    return {"kind": "story", "start": start, "end": end, "title": "T", "headline": "Headline", "source": "Src",
            **kw}


def vis(n_scenes=2, n_points=3, n_check=2):
    return {"scenes": [{"type": "stat", "value": "1", "label": "x"} for _ in range(n_scenes)],
            "key_points": [{"text": f"p{i}"} for i in range(n_points)],
            "checklist": [{"text": f"c{i}"} for i in range(n_check)]}


def check_schedule(sch, p, cfg):
    ps, pe = p["start"], p["end"]
    ts, te = sch["transition"]
    assert ts == ps
    assert te == pytest.approx(ps + min(3.0, 0.4 * (pe - ps)))
    smin = cfg.get("recap.newsroom.scene_min_seconds")
    for s, e, sc in sch["scenes"]:
        assert te - 1e-9 <= s < e <= pe + 1e-9
        assert e - s >= smin - 1e-9
        assert sc["type"] in S.BUILDERS
    ss = sch["scenes"]
    assert all(ss[i][1] <= ss[i + 1][0] + 1e-9 for i in range(len(ss) - 1))
    ts_ = [t for t, _ in sch["points"]]
    assert ts_ == sorted(ts_)
    assert all(b - a >= 1.2 - 1e-9 for a, b in zip(ts_, ts_[1:]))
    assert all(te <= t <= pe for t in ts_)
    cl = sch["checklist"]
    if cl:
        start, ticks = cl
        assert start >= (ts_[-1] + 3.0 - 1e-9 if ts_ else te)
        assert pe - start >= 4.0 - 1e-9
        assert all(start <= t <= pe for t in ticks)
    return sch


def test_schedule_with_times():
    cfg, p = make_cfg(), part()
    times = {"scenes": [110.0, 130.0], "points": [108.0, 109.0, 125.0], "checklist": [150.0, 153.0]}
    sch = check_schedule(N.schedule(cfg, p, vis(), times), p, cfg)
    assert [round(t, 1) for t, _ in sch["points"]] == [108.0, 109.2, 125.0]
    assert sch["checklist"] and sch["checklist"][0] == pytest.approx(149.0)
    assert [round(s) for s, _, x in sch["scenes"] if x["type"] == "stat"] == [110, 130]


def test_schedule_no_timing_even_spacing():
    cfg, p = make_cfg(), part()
    for times in (None, {}, {"points": [None, None, None]}):
        sch = check_schedule(N.schedule(cfg, p, vis(), times), p, cfg)
        assert len(sch["points"]) == 3 and sch["checklist"] and len(sch["scenes"]) >= 2


def test_schedule_no_visuals_is_auto_scenes_covering_window():
    cfg, p = make_cfg(), part(start=50.0, end=140.0)
    for v in ({}, None, {"scenes": [{"type": "bogus"}]}):
        sch = check_schedule(N.schedule(cfg, p, v, None), p, cfg)
        te = sch["transition"][1]
        sc = sch["scenes"]
        assert sc and sc[0][0] == pytest.approx(te) and sc[-1][1] == pytest.approx(140.0)
        assert all(abs(a[1] - b[0]) < 1e-6 for a, b in zip(sc, sc[1:]))
        assert {x["type"] for _, _, x in sc} == {"headline"}      # no card -> no cve
        assert sch["points"] == [] and sch["checklist"] is None
    p2 = part(start=50.0, end=140.0, card={"cve_id": "CVE-2026-1111", "cvss": 9.8})
    sch = N.schedule(cfg, p2, {}, None)
    assert {x["type"] for _, _, x in sch["scenes"]} == {"headline", "cve"}


def test_schedule_gap_fill_only_beyond_max():
    cfg, p = make_cfg(), part(start=0.0, end=100.0)
    v = {"scenes": [{"type": "stat", "value": "1", "label": "x"}]}
    sch = N.schedule(cfg, p, v, {"scenes": [50.0]})
    types = [x["type"] for _, _, x in sch["scenes"]]
    assert types.count("stat") == 1 and "headline" in types
    # a scene every ~10 s leaves no gap > 18 s -> no auto scenes
    v = {"scenes": [{"type": "stat", "value": "1", "label": "x"} for _ in range(3)]}
    sch = N.schedule(cfg, part(start=0.0, end=40.0), v, {"scenes": [3.5, 15.0, 27.0]})
    assert [x["type"] for _, _, x in sch["scenes"]] == ["stat"] * 3


def test_schedule_scene_min_enforced():
    cfg, p = make_cfg(), part(start=0.0, end=60.0)
    v = {"scenes": [{"type": "stat", "value": "1", "label": "x"} for _ in range(4)]}
    sch = check_schedule(N.schedule(cfg, p, v, {"scenes": [10.0, 10.5, 11.0, 59.0]}), p, cfg)
    assert all(e - s >= 6 - 1e-9 for s, e, _ in sch["scenes"])


def test_schedule_points_late_are_dropped_and_checklist_omitted():
    cfg, p = make_cfg(), part(start=0.0, end=60.0)
    times = {"points": [20.0, 21.0, 59.5], "checklist": [58.0, 59.0]}
    sch = check_schedule(N.schedule(cfg, p, vis(), times), p, cfg)
    assert len(sch["points"]) == 2            # the 59.5 s point cannot be shown
    assert sch["checklist"][0] == pytest.approx(56.0)       # late cues: start pulled back to keep 4 s
    times = {"points": [20.0, 21.0, 40.0], "checklist": [58.0]}
    assert N.schedule(cfg, p, vis(), times)["checklist"][0] == pytest.approx(56.0)
    times = {"points": [20.0, 21.0, 56.0], "checklist": [58.0]}
    assert N.schedule(cfg, p, vis(), times)["checklist"] is None     # 56 + 3 > 56 -> no room


def test_schedule_checklist_needs_room():
    cfg, p = make_cfg(), part(start=0.0, end=30.0)
    sch = N.schedule(cfg, p, vis(), {"points": [10.0, 11.5, 27.0], "checklist": [25.0, 28.0]})
    assert sch["checklist"] is None or 30.0 - sch["checklist"][0] >= 4.0
    short = part(start=0.0, end=9.0)
    sch = check_schedule(N.schedule(cfg, short, vis(), None), short, cfg)
    assert sch["transition"][1] == pytest.approx(3.0)


def test_schedule_tiny_part_stays_inside():
    cfg, p = make_cfg(), part(start=10.0, end=12.0)
    sch = check_schedule(N.schedule(cfg, p, vis(), {"points": [11.9], "scenes": [5.0, 99.0]}), p, cfg)
    assert sch["transition"] == (10.0, pytest.approx(10.8))
    assert sch["scenes"] == []


def test_schedule_card_limits_points_to_three():
    cfg = make_cfg()
    p = part(card={"cve_id": "CVE-2026-1111"})
    v = vis(n_points=4)
    assert len(N.schedule(cfg, p, v, None)["points"]) == 3
    assert len(N.schedule(cfg, part(), v, None)["points"]) == 4


# ------------------------------------------------------------------ fit_chunks
def test_fit_chunks_limits_and_words():
    words = [(i * 0.3, i * 0.3 + 0.25, w) for i, w in enumerate(
        "this is a fairly long sentence, with several other words that need splitting up. Next one".split())]
    ch = N.fit_chunks(words, 6, 24)
    assert " ".join(c[2] for c in ch).split() == [w[2] for w in words]
    assert all(len(c[2].split()) <= 6 for c in ch)
    assert all(len(c[2]) <= 24 or len(c[2].split()) == 1 for c in ch)
    assert any(c[2].endswith(",") for c in ch)             # breaks after punctuation
    assert all(c[0] < c[1] for c in ch)
    assert N.fit_chunks([], 6, 40) == []
    long = N.fit_chunks([(0, 1, "supercalifragilisticexpialidocious"), (1, 2, "ok")], 6, 10)
    assert [c[2] for c in long] == ["supercalifragilisticexpialidocious", "ok"]


# ------------------------------------------------------------------ chrome
def test_chrome_ass():
    cfg = make_cfg()
    a = N.build_chrome_ass(cfg, FX["parts"], FX["total"], week_label="Week 47", handle="@cyber", has_logo=False)
    b = N.build_chrome_ass(cfg, FX["parts"], FX["total"], week_label="Week 47", handle="@cyber", has_logo=True)
    ea, eb = events(a), events(b)
    assert "THIS WEEK IN CYBER" in a and "WEEK 47" in a and "@cyber" in a
    assert all(e[1] == 0 and e[2] >= 5 for e in ea)       # visible on frame 0
    assert len(ea) == len(eb)

    def hx(ass):
        m = re.search(r"\\pos\((\d+),60\)[^}]*\}@cyber", ass)
        return int(m[1])
    assert hx(b) < hx(a)
    assert a.count("PlayResX: 1920") == 1 and "Style: Default" in a
    assert "Alignment" in a
    # ticks: one per story
    assert sum(1 for e in ea if e[0] == 3 and "\\p1" in e[3] and e[3].split("}")[0].count("1c&H8A7F74&")) == 5


def test_chrome_without_handle_or_week():
    a = N.build_chrome_ass(make_cfg(), [], 60.0, week_label="", handle="", has_logo=False)
    assert "@" not in a and events(a)


# ------------------------------------------------------------------ whole file
def story_parts(fx):
    return [p for p in fx["parts"] if p["kind"] == "story"]


def test_output_parses_and_has_style():
    assert "[Events]" in ASS and "Style: Default" in ASS
    assert len(re.findall(r"^Style: ", ASS, re.M)) == 1
    assert EVS


def test_every_event_inside_its_part():
    # events of story i are the ones that start inside its window: they must also end inside it
    parts = FX["parts"]
    for p in parts:
        ps, pe = p["start"], p["end"]
        for layer, s, e, _ in EVS:
            if layer not in (1, 24, 25, 26) and ps - 1e-9 <= s < pe - TOL:
                assert e <= pe + TOL, (p["title"], layer, s, e)
    assert all(0 <= s and e <= FX["total"] + TOL for _l, s, e, _b in EVS)
    # only whole-video elements (progress, scanline) may span parts
    for layer, s, e, body in EVS:
        if layer in (25, 26, 1) or layer == 24:
            continue
        owners = [p for p in parts if p["start"] - TOL <= s < p["end"] - TOL][:1]
        assert owners, (layer, s)
        assert e <= owners[0]["end"] + TOL, (layer, s, e, owners[0]["title"], body[:60])


def test_transition_starts_at_part_start_and_whoosh_leads():
    cfg = make_cfg()
    with mock.patch.object(sound, "kit_file", side_effect=lambda c, n: Path("/x") if n in ("whoosh", "outro") else None):
        evs = sound.recap_events(cfg, FX["parts"], FX["total"])
    whooshes = [t for t, k in evs if k == "whoosh"]
    lead = cfg.get("sound.whoosh_lead")
    for p, w in zip(story_parts(FX), whooshes):
        assert w == pytest.approx(p["start"] - lead)
        trans = [e for e in EVS if e[0] == 23 and "STORY" in e[3] and e[3].count("\\fad(120,0)")]
        assert any(abs(t[1] - p["start"]) <= TOL for t in trans), p["start"]
        # the card leads the sound by `lead` (cs-rounded)
        assert format_ass_time(p["start"]) in [format_ass_time(t[1]) for t in trans]


def test_no_caption_starts_inside_a_transition():
    cfg = make_cfg()
    wins = []
    for p in story_parts(FX):
        n = p["end"] - p["start"]
        wins.append((p["start"], p["start"] + min(3.0, 0.4 * n)))
    caps = [e for e in EVS if e[0] == 30]
    assert len(caps) > 500
    for _, s, e, _b in caps:
        for a, b in wins:
            assert not (a - 1e-9 <= s < b - 1e-9), (s, a, b)
    # and captions resume right after
    for a, b in wins:
        assert any(b - 1e-9 <= s < b + 3 for _, s, _e, _b in caps)
    del cfg


def test_captions_use_layer_30_default_and_keywords():
    caps = [ln for ln in ASS.splitlines() if ln.startswith("Dialogue: 30,")]
    assert caps and all(",Default,,0,0,0,," in c for c in caps)
    assert any("\\c&H000AD6FF&" in c for c in caps)              # per-story keyword colour
    # story captions sit in the left-of-centre box, intro/outro captions are centred under their card
    cards = [(p["start"], p["end"]) for p in FX["parts"] if p["kind"] in ("intro", "outro")]
    for c in caps:
        t0 = secs(c.split(",")[1])
        inside = any(a - 1e-6 <= t0 < b for a, b in cards)
        assert ("\\pos(960,906)" if inside else "\\pos(640,906)") in c, c
    # intro/outro captions get their own box above the card layers, centred on x = 960
    boxes = [e for e in EVS if e[0] == 29]
    assert len(boxes) == 2
    for b in boxes:
        m = re.search(r"\\pos\((\d+),(\d+)\)", b[3])
        assert int(m[1]) + 550 == 960, b[3]
    assert any(c for c in caps if "\\pos(960,906)" in c)


def test_cards_and_panels_present():
    layers = {e[0] for e in EVS}
    assert {20, 22, 23, 24, 25, 26, 30} <= layers
    assert "STORY 1 OF 5" in ASS and "STORY 5 OF 5" in ASS
    assert "KEY POINTS" in ASS and "WHAT TO DO" in ASS and "CVE-2026-1111" in ASS
    assert "SUBSCRIBE" in ASS and "THIS WEEK IN CYBER" in ASS
    assert ">1 / 5<" not in ASS
    assert any(e[3].endswith("}1 / 5") for e in EVS) and any(e[3].endswith("}Story 1 headline") for e in EVS)
    assert "SOURCE · EXAMPLE NEWS" in ASS
    assert ASS.count("CVE-2026-1111") >= 3          # 3 stories carry the card


def test_points_after_transition_and_checklist_after_points():
    for p in story_parts(FX):
        te = p["start"] + min(3.0, 0.4 * (p["end"] - p["start"]))
        pts = sorted(e[1] for e in EVS if e[0] == 3 and "\\move" in e[3] and p["start"] <= e[1] < p["end"]
                     and "&H3A2516&" in e[3])
        assert pts and all(t >= te - TOL for t in pts)
        wtd = [e for e in EVS if "WHAT TO DO" in e[3] and p["start"] <= e[1] < p["end"]]
        assert len(wtd) == 1
        assert wtd[0][1] >= pts[-1] + 3.0 - TOL and wtd[0][2] <= p["end"] + TOL
        assert p["end"] - wtd[0][1] >= 4.0 - TOL


def test_story_without_visuals_renders_with_auto_scenes():
    fx = make_fixture(with_visuals=False, with_card=True)
    ass = build(fx)
    evs = events(ass)
    assert evs
    for p in story_parts(fx):
        te = p["start"] + min(3.0, 0.4 * (p["end"] - p["start"]))
        scene_evs = [e for e in evs if p["start"] <= e[1] < p["end"] and 2 <= e[0] <= 8 and e[1] >= te - TOL
                     and e[3].startswith("{\\an")]
        assert scene_evs
        assert max(e[2] for e in scene_evs) >= p["end"] - 0.6        # covers the window (to its end)
    assert "WHAT TO DO" not in ass and "KEY POINTS" not in ass
    # visuals list shorter than stories / None entries
    ass2 = build(fx, visuals=[None])
    assert events(ass2)


def test_no_timing_data_falls_back():
    fx = make_fixture()
    fx["script"] = fx["script"] + " extra"        # word count mismatch -> story_word_ranges None
    ass = build(fx)
    assert "KEY POINTS" in ass and "WHAT TO DO" in ass
    fx["words"] = []
    ass = build(fx)
    assert events(ass) and not [e for e in events(ass) if e[0] == 30]


def test_scale_8_minutes():
    fx = make_fixture()
    assert len(fx["words"]) >= 1200 and fx["total"] > 470
    evs = events(build(fx))
    assert len(evs) <= 3000, len(evs)
    # active events at sampled seconds
    peak = 0
    t = 0.5
    while t < fx["total"]:
        n = sum(1 for _, s, e, _b in evs if s <= t < e)
        peak = max(peak, n)
        t += 1.0
    assert peak <= 80, peak


def test_hostile_text_is_escaped():
    fx = make_fixture(n_stories=1, paras=(4,), words_per_para=20)
    fx["stories"][0]["title"] = "Bad {\\an9} title \\N with {braces}"
    fx["visuals"][0]["key_points"][0]["text"] = "{\\p1}evil"
    ass = build(fx)
    assert "{\\an9} title" not in ass and "{\\p1}evil" not in ass
    assert events(ass)


def test_write_newsroom_ass(tmp_path):
    out = N.write_newsroom_ass(make_cfg(), FX["words"], FX["chunks"], FX["parts"], FX["total"], tmp_path / "n.ass",
                               stories=FX["stories"], visuals=FX["visuals"], cards=FX["cards"],
                               script=FX["script"], para_counts=FX["para_counts"], week_label="W", handle="@h")
    assert out == tmp_path / "n.ass" and out.read_text(encoding="utf-8").startswith("[Script Info]")


def test_scanline_toggle():
    cfg = make_cfg(scanline=False)
    ass = build(FX, cfg)
    assert "\\1a&HF2&" not in ass and "\\1a&HF2&" in ASS


# ------------------------------------------------------------------ review round
def _tags_pos(body):
    m = re.search(r"\\pos\((\d+),(\d+)\)", body)
    return int(m[1]), int(m[2])


def test_top_bar_spacing_matches_design():
    lay = N._top_layout(make_cfg(), "Week 1", "@yourhandle", True)
    px, pw, label, _ = lay["pill"]
    title_end = 60 + S.est_w("THIS WEEK IN CYBER", 40, 0.60, 1.0)
    assert 45 <= px - title_end <= 65                      # design: ~55 px between the title and the WEEK pill
    assert 505 <= px <= 535                                # design pill at x = 520
    assert 690 <= lay["chap_x"] <= 720                     # design chapter at x = 700
    import inspect
    assert list(inspect.signature(N._top_layout).parameters) == ["cfg", "week_label", "handle", "has_logo"]


def test_counter_and_chapter_are_separate_events():
    p = story_parts(FX)[0]
    te = p["start"] + min(3.0, 0.4 * (p["end"] - p["start"]))
    top = [e for e in EVS if e[0] == 4 and "\\pos(" in e[3] and _tags_pos(e[3])[1] == 60 and abs(e[1] - te) < 0.02]
    counter = [e for e in top if e[3].endswith("}1 / 5")]
    chap = [e for e in top if e[3].endswith("}Story 1 headline")]
    assert len(counter) == 1 and len(chap) == 1
    cx, tx_ = _tags_pos(counter[0][3])[0], _tags_pos(chap[0][3])[0]
    assert 12 <= tx_ - cx - S.est_w("1 / 5", 30, 0.5, 1.0) <= 40
    lay = N._top_layout(make_cfg(), "Week 47", "@cyber", True)
    assert cx == round(lay["chap_x"])


def _fx_one(chapter, title="Acme Gateway flaw exploited in the wild"):
    fx = make_fixture(n_stories=3, paras=(3, 3, 3), words_per_para=30)
    fx["parts"][1]["title"] = chapter
    fx["stories"][0]["title"] = title
    fx["stories"][0]["summary"] = "Acme Gateway version 7.4.2 had a serious flaw."
    return fx


def test_unverified_chapter_falls_back_to_story_title():
    for bad in ("Texts link to acme-login.online", "Hxxp://evil[.]com wave", "PayPal breach hits 5,000",
                "Zeta Corp breach", "CVE-2026-9999 exploited"):
        fx = _fx_one(bad)
        ass = build(fx)
        evs = events(ass)
        assert bad not in ass and "evil" not in ass and "PayPal" not in ass and "Zeta" not in ass
        assert any(e[3].endswith("}Acme Gateway flaw exploited in the wild") for e in evs if e[0] == 4)
        assert any(e[0] == 23 and "Acme Gateway flaw exploited in the wild" in e[3] for e in evs)     # intro lineup
    ass = build(_fx_one("Acme Gateway flaw"))
    assert any(e[3].endswith("}Acme Gateway flaw") for e in events(ass))
    assert any(e[0] == 23 and e[3].endswith("}Acme Gateway flaw") for e in events(ass))


def test_chapter_check_uses_card_values_and_never_fetches():
    fx = _fx_one("CVE-2026-1111 exploited")
    fx["cards"][0] = {"cve_id": "CVE-2026-1111", "cvss": 9.8}
    ass = build(fx)
    assert any(e[3].endswith("}CVE-2026-1111 exploited") for e in events(ass))


def test_caption_after_transition_is_not_dropped():
    # the 6-word chunk spanning the transition end used to be dropped whole -> up to 2 s of silence
    fx = make_fixture(n_stories=3, paras=(3, 3, 3), words_per_para=30, step=0.4)
    words = fx["words"]
    for p in story_parts(fx):
        te = p["start"] + min(3.0, 0.4 * (p["end"] - p["start"]))
        first = next(w for w in words if w[0] >= te - 1e-9)
        evs = events(build(fx))
        caps = [e for e in evs if e[0] == 30 and p["start"] <= e[1] < p["end"]]
        assert caps
        t_first = min(e[1] for e in caps)
        assert t_first <= first[0] + 0.05, (t_first, first[0])
        assert t_first >= te - 1e-6
    # unit: the forced break starts a chunk exactly at the first word after the transition
    ws = [(i * 0.5, i * 0.5 + 0.4, f"w{i}") for i in range(12)]
    plain = N._chunk_idx(ws, 6, 60)
    forced = N._chunk_idx(ws, 6, 60, force_break=3)
    assert plain == [(0, 6), (6, 12)]
    assert forced == [(0, 3), (3, 9), (9, 12)]


def test_caption_lines_signature_has_no_unused_params():
    import inspect
    assert "ps" not in inspect.signature(N._caption_lines).parameters


def test_hostile_text_cannot_break_lines_or_inject_overrides():
    fx = make_fixture(n_stories=1, paras=(4,), words_per_para=20)
    fx["stories"][0]["title"] = "C:\\new\\Nline (\\pos(1,1)) \u202eRTL\u202c\nsecond"
    fx["words"][5] = (fx["words"][5][0], fx["words"][5][1], "evil\\Nword{\\fs200}")
    ass = build(fx)
    body = "\n".join(e[3] for e in events(ass))
    assert "\u202e" not in body and "\u202c" not in body
    for e in events(ass):
        text = re.sub(r"\{[^{}]*\}", "", e[3]) if "\\p1" not in e[3] else ""
        # no user-supplied \N: a visible backslash is always followed by the word joiner
        for m in re.finditer(r"\\(.?)", text):
            assert m[1] in ("N", "\u2060", "\\"), text
    assert "Nline" in body and "FS200" in body.upper()           # the text is still shown, harmlessly
    assert "{\\fs200}" not in body.lower()


def test_multiline_right_panel_title_keeps_its_line_break():
    # a title wider than the panel wraps to two lines: the break is intentional (\N), not turned into a space
    evs = [e for e in EVS if e[0] == 4 and _tags_pos(e[3] if "\\pos(" in e[3] else "{\\pos(0,0)}") == (N.PX + 32, N.PY + 70)]
    assert evs and all("\\N" in e[3] for e in evs), evs[:1]
