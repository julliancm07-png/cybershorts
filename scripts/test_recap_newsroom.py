#!/usr/bin/env python3
"""Offline smoke test for the recap v2 "newsroom" layout: ~30 s of synthetic recap through the real chrome ->
background -> ASS -> single-pass render, then size/duration/frame checks and PNG frames in /tmp/recap_v2_frames/."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import logging
import os
import resource
import subprocess
import tempfile
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

TOTAL = 30.0
STEP = 0.3                      # seconds per fake word
# intro, story 1 (terminal + flow + CVE card + 3 points + checklist), story 2 (stat + timeline + scam), outro
PARTS = [("intro", 0.0, 3.6), ("story", 3.6, 16.2), ("story", 16.2, 26.7), ("outro", 26.7, TOTAL)]  # multiples of STEP
PARA_COUNTS = [1, 2, 2, 1]
FRAMES = [  # name, second
    ("01_intro", 2.0),
    ("02_transition_glitch", 3.75),
    ("03_terminal_typing", 7.0),
    ("04_flow_drawing", 11.6),
    ("05_cve_card_points", 9.6),
    ("06_checklist_tick", 12.3),
    ("07_stat_countup", 19.6),
    ("08_timeline", 22.6),
    ("09_scam", 25.2),
    ("10_outro", 28.5),
    # settled states (extra review frames)
    ("02b_transition_settled", 5.0),
    ("07b_stat_end", 21.1),
    ("08b_timeline_end", 23.7),
    ("09b_scam_end", 26.5),
]
OUT_DIR = Path("/tmp/recap_v2_frames")


def ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True, timeout=300)


def probe(path: Path) -> dict:
    txt = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
         "-show_entries", "stream=width,height,nb_read_packets,avg_frame_rate", "-show_entries", "format=duration",
         "-of", "default=nw=1", str(path)], capture_output=True, text=True, check=True, timeout=120).stdout
    return dict(line.split("=", 1) for line in txt.split() if "=" in line)


NARRATION = [          # one paragraph per entry; word counts must match PARTS (STEP seconds per word)
    ["Welcome back to the weekly recap. Here are the top security stories."],
    ["Attackers are exploiting a flaw in Acme Gateway. Acme released version 7.4.3 to fix it, but about 4,000 "
     "servers were exposed.",
     "Update every gateway today, review your access logs, and rotate admin credentials. Then watch your "
     "network for any unusual login activity."],
    ["A phishing wave hit small businesses this week. Scammers tricked 70% of targets and stole $2.3 million "
     "in 2026.",
     "Never click links in unexpected texts. Check the sender, then report the message to your bank."],
    ["Thanks for watching. Subscribe for next week's recap and the sources."],
]
SOURCES = [
    "Attackers are exploiting a flaw in Acme Gateway. Versions before 7.4.3 are affected, including 7.4.2. "
    "CVE-2026-1111 is rated 9.8 and was disclosed on Nov 12. Acme released version 7.4.3 to fix it, but about "
    "4,000 servers were exposed. Admins should update, review access logs and rotate credentials.",
    "A phishing wave hit small businesses this week. Scam texts claiming to be from Acme Support asked people to "
    "verify their accounts. The campaign began on Nov 10, banks warned customers on Nov 14 and police issued an "
    "alert on Nov 20. Scammers tricked 70% of targets and stole $2.3 million in 2026.",
]


def make_words():
    """Words of the narration timed at STEP seconds each; returns words, script, per-paragraph word ranges."""
    words, paras, bounds, k = [], [], [], 0
    for (kind, s, e), part in zip(PARTS, NARRATION):
        toks_part = [p.split() for p in part]
        assert sum(len(t) for t in toks_part) == int(round((e - s) / STEP)), (kind, s, e)
        for toks in toks_part:
            a = k
            for tok in toks:
                words.append((k * STEP, k * STEP + STEP * 0.85, tok))
                k += 1
            paras.append(" ".join(toks))
            bounds.append((a, k))
    return words, "\n".join(paras), bounds


def cue_for(words, bounds, part_idx, phrase):
    """(para 1-based inside the part, 4-word cue starting at `phrase`) for a phrase of the narration."""
    first = sum(PARA_COUNTS[:part_idx])
    want = phrase.split()
    plain = [str(w[2]).strip(".,").lower() for w in words]
    for q in range(PARA_COUNTS[part_idx]):
        a, b = bounds[first + q]
        for i in range(a, b - len(want) + 1):
            if plain[i:i + len(want)] == [w.lower() for w in want]:
                i = min(i, b - 4)           # keep the 4-word cue inside its paragraph
                return q + 1, " ".join(str(w[2]).strip(".,") for w in words[i:i + 4])
    raise ValueError("phrase not in part %d: %r" % (part_idx, phrase))


def build_visuals(words, bounds, cfg, stories):
    """Raw LLM-style visuals for the two stories, run through the real validation (nothing may be dropped)."""
    from cybershorts import recap, recap_visuals

    def cu(pi, phrase):
        p, c = cue_for(words, bounds, pi, phrase)
        return {"para": p, "cue": c}

    raw = [
        {"keywords": ["Acme Gateway", "version 7.4.3"],
         "key_points": [
             {"text": "Acme Gateway flaw exploited", **cu(1, "flaw in Acme Gateway")},
             {"text": "Fixed in version 7.4.3", **cu(1, "version 7.4.3")},
             {"text": "About 4,000 servers exposed", **cu(1, "about 4,000 servers")},
         ],
         "checklist": [
             {"text": "Update every Acme Gateway", **cu(1, "Update every gateway")},
             {"text": "Review your access logs", **cu(1, "review your access logs")},
             {"text": "Rotate admin credentials", **cu(1, "rotate admin credentials")},
         ],
         "scenes": [
             {"type": "terminal", "title": "Attacker console", **cu(1, "flaw in Acme Gateway"),
              "lines": [{"kind": "cmd", "text": "probe gateway --version"},
                        {"kind": "out", "text": "Acme Gateway 7.4.2"},
                        {"kind": "cmd", "text": "send crafted request"},
                        {"kind": "alert", "text": "Server compromised"},
                        {"kind": "out", "text": "attacker has admin access"}]},
             {"type": "flow", "title": "How the attack works", **cu(1, "about 4,000 servers"),
              "nodes": [{"label": "Attacker", "sub": "crafted request"},
                        {"label": "Gateway", "sub": "unpatched 7.4.2"},
                        {"label": "Network", "sub": "full admin access"}],
              "arrows": ["sends", "exploits"], "fixed_in": "7.4.3"},
         ]},
        {"keywords": ["phishing wave"],
         "key_points": [
             {"text": "70% of targets were tricked", **cu(2, "tricked 70% of targets")},
             {"text": "$2.3 million stolen in 2026", **cu(2, "stole $2.3 million in")},
         ],
         "checklist": [],
         "scenes": [
             {"type": "stat", "value": "70%", "label": "of targets were tricked", "context": "Phishing wave, 2026",
              **cu(2, "tricked 70% of targets")},
             {"type": "timeline", **cu(2, "stole $2.3 million in"),
              "events": [{"date": "Nov 10", "label": "Campaign begins"}, {"date": "Nov 14", "label": "Banks warn"},
                         {"date": "Nov 20", "label": "Police alert"}]},
             {"type": "scam", "channel": "sms", "sender": "Acme Support", **cu(2, "unexpected texts"),
              "body": "Your Acme account is at risk. Verify your account now: {link}",
              "flags": ["Urgent tone", "Unknown link", "Asks to verify"]},
         ]},
    ]
    out = []
    for i, (r, s) in enumerate(zip(raw, stories)):
        paras = NARRATION[i + 1]
        v = recap_visuals.clean_visuals(r, recap.source_text(cfg, s), "\n\n".join(paras), cfg)
        assert not v["dropped"], v["dropped"]
        v["story_index"] = i + 1
        out.append(v)
    return out


def main(argv=None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    tmp = tempfile.TemporaryDirectory()
    os.environ["APP_DIR"] = tmp.name
    from cybershorts import captions, recap_newsroom, recap_render, render
    from cybershorts.config import load

    cfg = load()
    nr = cfg.raw["recap"]["newsroom"]       # ~11 s stories need shorter scenes than the 8-minute defaults
    nr.update({"transition_seconds": 2.0, "scene_min_seconds": 2.5, "scene_max_seconds": 8, "scene_seconds": 5})
    work = cfg.path("work")
    work.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for old in OUT_DIR.glob("*.png"):
        old.unlink()
    t0 = time.time()

    wav = work / "recap_tone.wav"
    ff("-f", "lavfi", "-i", f"sine=frequency=220:duration={TOTAL - render.PAD_SECONDS}", "-ar", "22050", "-ac", "1",
       str(wav))

    words, script, bounds = make_words()
    chunks = captions.chunk_words(words, int(cfg.get("recap.words_per_chunk", 6)))
    n = 2
    parts = [{"kind": k, "start": s, "end": e,
              "title": {"intro": "Intro", "outro": "Subscribe"}.get(k, "Gateway flaw" if s < 10 else "Phishing wave"),
              "index": (1 if s < 10 else 2) if k == "story" else 0, "total": n} for k, s, e in PARTS]
    stories = [{"title": "Acme Gateway flaw exploited in the wild", "source": "Example News",
                "url": "https://www.example.com/a", "series": "Breach", "summary": SOURCES[0], "article": ""},
               {"title": "Phishing wave hits small businesses", "source": "Sample Daily",
                "url": "https://www.sample.org/b", "summary": SOURCES[1], "article": ""}]
    cards = [{"cve_id": "CVE-2026-1111", "cvss": 9.8, "severity": "critical", "exploited": True,
              "patch_available": True}, None]
    visuals = build_visuals(words, bounds, cfg, stories)

    chrome = work / "draft_9_chrome.ass"
    bg = work / "draft_9_bg.png"
    has_logo = bool(render._watermark_path(cfg)) and bool(render._logo_ok(render._watermark_path(cfg)))
    chrome.write_text(recap_newsroom.build_chrome_ass(cfg, parts, TOTAL, week_label="Week 1", handle="@yourhandle",
                                                      has_logo=has_logo), encoding="utf-8")
    ass = recap_newsroom.write_newsroom_ass(
        cfg, words, chunks, parts, TOTAL, work / "draft_9.ass", stories=stories, visuals=visuals, cards=cards,
        script=script, para_counts=PARA_COUNTS, week_label="Week 1", handle="@yourhandle")
    n_events = sum(1 for ln in ass.read_text(encoding="utf-8").splitlines() if ln.startswith("Dialogue:"))

    out = cfg.path("videos", "recap_newsroom_smoke.mp4")
    t1 = time.time()
    recap_render.render_background(cfg, bg, chrome_ass=chrome)
    t_bg = time.time() - t1
    t2 = time.time()
    recap_render.render_recap_single(cfg, wav, ass, bg, TOTAL, out)
    t_render = time.time() - t2

    info = probe(out)
    w, h, dur = int(info["width"]), int(info["height"]), float(info["duration"])
    fps = int(cfg.get("recap.fps", 30))
    frames = int(info["nb_read_packets"])
    assert (w, h) == (1920, 1080), "bad size %dx%d" % (w, h)
    assert abs(dur - TOTAL) <= 0.35, "duration %.2f, expected %.2f" % (dur, TOTAL)
    assert abs(frames - fps * dur) <= 1, "frames %d, expected %d" % (frames, round(fps * dur))

    for name, t in FRAMES:
        png = OUT_DIR / f"{name}.png"
        ff("-ss", f"{t:.3f}", "-i", str(out), "-frames:v", "1", str(png))
        assert png.exists() and png.stat().st_size > 10_000, "frame %s missing/empty" % name
    saved = sorted(p.name for p in OUT_DIR.glob("*.png"))

    rss_mb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024.0
    est = t_render * 480.0 / TOTAL
    print("recap  %dx%d %.2fs frames=%d (fps %d) %.2f MB, %d ass events" % (
        w, h, dur, frames, fps, out.stat().st_size / 1e6, n_events))
    print("frames saved in %s: %s" % (OUT_DIR, ", ".join(saved)))
    print("background %.1fs, final pass %.1fs = %.1f fps" % (t_bg, t_render, frames / max(t_render, 1e-6)))
    print("peak child RSS %.0f MB" % rss_mb)
    print("8-minute recap estimate: ~%.0f s (%.1f min; event density of this sample is higher than a real recap)" % (
        est, est / 60))
    print("OK wall time %.1fs" % (time.time() - t0))
    tmp.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
