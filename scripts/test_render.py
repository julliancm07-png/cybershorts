#!/usr/bin/env python3
"""Offline smoke test: synthetic audio + fake words -> 1080x1920 mp4."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import logging
import subprocess

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

TEXT = ("Researchers disclosed a critical flaw in a popular VPN appliance today and "
        "attackers are already exploiting it so patch now Stay patched stay safe").split()


def main() -> int:
    from cybershorts import captions, render
    from cybershorts.config import load

    cfg = load()
    work = cfg.path("work")
    wav = work / "test_tone.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=220:duration=6",
         "-ar", "22050", "-ac", "1", str(wav)],
        check=True, timeout=60,
    )
    per = 6.0 / len(TEXT)
    words = [(i * per, (i + 1) * per, w) for i, w in enumerate(TEXT)]
    chunks = captions.chunk_words(words, int(cfg.get("captions.words_per_chunk", 3)))
    ass = work / "test_render.ass"
    captions.write_ass(chunks, cfg, "Critical VPN flaw exploited", cfg.get("channel.handle", "@cybershorts"), 6.0, ass)
    out = cfg.path("videos", "test_render.mp4")
    render.render(cfg, wav, ass, None, out)

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-show_entries", "format=duration", "-of", "default=nw=1", str(out)],
        capture_output=True, text=True, check=True, timeout=30,
    ).stdout
    info = dict(line.split("=", 1) for line in probe.split() if "=" in line)
    w, h, dur = int(info["width"]), int(info["height"]), float(info["duration"])
    assert (w, h) == (1080, 1920), "bad size %dx%d" % (w, h)
    assert dur >= 6, "duration too short: %.2f" % dur
    print("OK %dx%d %.2fs %s" % (w, h, dur, out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
