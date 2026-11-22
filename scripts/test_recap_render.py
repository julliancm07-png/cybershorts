#!/usr/bin/env python3
"""Offline smoke test: synthetic audio/clips/logo/ASS -> 1920x1080 recap, 854x480 preview, still, 1280x720 thumbnail."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import logging
import os
import subprocess
import tempfile
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

AUDIO_SECONDS = 20.0
PARTS = [(0.0, 4.0), (4.0, 12.0), (12.0, 20.6)]  # intro, story, outro (sum = audio + PAD)


def ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True, timeout=180)


def probe(path: Path) -> tuple[int, int, float]:
    txt = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-show_entries", "format=duration", "-of", "default=nw=1", str(path)],
        capture_output=True, text=True, check=True, timeout=30).stdout
    info = dict(line.split("=", 1) for line in txt.split() if "=" in line)
    return int(info["width"]), int(info["height"]), (float(info["duration"]) if info.get("duration", "N/A") != "N/A" else 0.0)


def inline_ass(w: int, h: int, text: str, big: bool = False) -> str:
    size = int(h * (0.16 if big else 0.05))
    return (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {w}\nPlayResY: {h}\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
            "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Default,DejaVu Sans,{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,4,0,2,40,40,{int(h*0.1)},1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            f"Dialogue: 0,0:00:00.00,0:00:30.00,Default,,0,0,0,,{text}\n")


def main(argv=None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    tmp = tempfile.TemporaryDirectory()
    os.environ["APP_DIR"] = tmp.name
    from cybershorts import recap_render, render
    from cybershorts.config import load

    cfg = load()
    work = cfg.path("work")
    brand = cfg.path("brand")
    brand.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    clips = []
    for i, pattern in enumerate(("testsrc2", "smptebars", "rgbtestsrc")):
        c = work / f"clip{i}.mp4"
        ff("-f", "lavfi", "-i", f"{pattern}=size=1280x720:rate=30:duration=3", "-pix_fmt", "yuv420p", str(c))
        clips.append(c)
    ff("-f", "lavfi", "-i", "color=c=0xFF2D55:s=300x300", "-vf", "drawbox=x=60:y=60:w=180:h=180:color=white:t=fill",
       "-frames:v", "1", str(brand / "logo.png"))
    wav = work / "recap_tone.wav"
    ff("-f", "lavfi", "-i", f"sine=frequency=220:duration={AUDIO_SECONDS}", "-ar", "22050", "-ac", "1", str(wav))

    ass = work / "recap.ass"
    try:  # prefer the real recap captions when that module exists
        from cybershorts import recap_captions
        words = [(i * 0.5, i * 0.5 + 0.45, f"word{i}") for i in range(int(AUDIO_SECONDS / 0.5))]
        from cybershorts import captions
        chunks = captions.chunk_words(words, 6)
        parts = [{"kind": "intro", "start": 0.0, "end": 4.0, "title": "Weekly recap", "index": 0, "total": 1},
                 {"kind": "story", "start": 4.0, "end": 12.0, "title": "Smoke test story", "index": 1, "total": 1},
                 {"kind": "outro", "start": 12.0, "end": 20.6, "title": "Outro", "index": 2, "total": 1}]
        recap_captions.write_recap_ass(cfg, words, chunks, parts, AUDIO_SECONDS + render.PAD_SECONDS,
                                       title="This Week in Cyber", week_label="Week 1", handle="@cybershorts", out_path=ass)
        print("ass: recap_captions")
    except Exception as e:  # noqa: BLE001
        ass.write_text(inline_ass(1920, 1080, "Recap smoke test"), encoding="utf-8")
        print("ass: inline fallback (%s)" % type(e).__name__)

    durs = [e - s for s, e in PARTS]
    part_clips = [[clips[0], clips[1]], [clips[2]], []]
    out = cfg.path("videos", "recap_smoke.mp4")
    prefix = work / "draft_9_recap"
    t1 = time.time()
    recap_render.render_recap(cfg, wav, ass, part_clips, durs, out, prefix=prefix)
    t_render = time.time() - t1
    w, h, dur = probe(out)
    expect = AUDIO_SECONDS + render.PAD_SECONDS
    assert (w, h) == (1920, 1080), "bad size %dx%d" % (w, h)
    assert abs(dur - expect) < 0.5, "duration %.2f, expected ~%.2f" % (dur, expect)
    assert not list(work.glob("draft_9_recap_*")), "temp files left: %s" % list(work.glob("draft_9_recap_*"))

    prev = cfg.path("videos", "recap_preview.mp4")
    recap_render.render_preview(cfg, out, prev, dur)
    pw, ph, pd = probe(prev)
    assert (pw, ph) == (854, 480), "bad preview size %dx%d" % (pw, ph)
    assert abs(pd - dur) < 0.5, "preview duration %.2f" % pd
    assert prev.stat().st_size <= 45 * 1024 * 1024

    still = recap_render.extract_still(clips[0], work / "still.png")
    sw, sh, _ = probe(still)
    assert (sw, sh) == (1280, 720), "bad still size %dx%d" % (sw, sh)
    tass = work / "thumb.ass"
    tass.write_text(inline_ass(1280, 720, "WEEK IN CYBER", big=True), encoding="utf-8")
    thumb = recap_render.render_thumbnail(cfg, still, tass, work / "thumb.jpg")
    tw, th, _ = probe(thumb)
    assert (tw, th) == (1280, 720), "bad thumbnail size %dx%d" % (tw, th)
    assert thumb.stat().st_size <= 2 * 1024 * 1024

    print("recap  %dx%d %.2fs (expected ~%.2f) %.2f MB, render %.1fs" % (w, h, dur, expect, out.stat().st_size / 1e6, t_render))
    print("preview %dx%d %.2fs %.2f MB" % (pw, ph, pd, prev.stat().st_size / 1e6))
    print("still %dx%d; thumbnail %dx%d %.0f KB" % (sw, sh, tw, th, thumb.stat().st_size / 1024))
    print("OK wall time %.1fs" % (time.time() - t0))
    tmp.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
