#!/usr/bin/env python3
"""Offline smoke test: synthetic clips/audio/logo + fake word timings -> 1080x1920 mp4 (v2 visuals)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import logging
import os
import subprocess
import tempfile

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

SCRIPT = (
    "Researchers disclosed a critical flaw in a popular VPN appliance today.\n\n"
    "Attackers are already exploiting it to steal admin credentials, so patch now.\n\n"
    "Stay patched, stay safe. Follow for daily cyber news."
)
KEYWORDS = ["VPN appliance", "patch"]
AUDIO_SECONDS = 7.0


def ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True, timeout=120)


def main() -> int:
    tmp = tempfile.TemporaryDirectory()
    os.environ["APP_DIR"] = tmp.name  # keep data/ out of the repo
    from cybershorts import captions, render
    from cybershorts.config import load

    cfg = load()
    work = cfg.path("work")
    brand = cfg.path("brand")
    brand.mkdir(parents=True, exist_ok=True)

    clips = []
    for i, pattern in enumerate(("testsrc2", "smptebars", "rgbtestsrc")):
        clip = work / f"clip{i}.mp4"
        ff("-f", "lavfi", "-i", f"{pattern}=size=640x360:rate=30:duration=2", "-pix_fmt", "yuv420p", str(clip))
        clips.append(clip)
    logo = brand / "logo.png"
    ff("-f", "lavfi", "-i", "color=c=0xFF2D55:s=300x300", "-vf", "drawbox=x=60:y=60:w=180:h=180:color=white:t=fill",
       "-frames:v", "1", str(logo))
    wav = work / "test_tone.wav"
    ff("-f", "lavfi", "-i", f"sine=frequency=220:duration={AUDIO_SECONDS}", "-ar", "22050", "-ac", "1", str(wav))

    tokens = [t for t in SCRIPT.split() if captions._norm(t)]
    per = AUDIO_SECONDS / len(tokens)
    words = [(i * per, (i + 1) * per, t) for i, t in enumerate(tokens)]
    beats = captions.beat_times(words, SCRIPT, AUDIO_SECONDS)
    assert len(beats) == 3 and beats[0] == 0.0, beats
    chunks = captions.chunk_words(words, int(cfg.get("captions.words_per_chunk", 3)))
    ass = work / "test_render.ass"
    captions.write_ass(chunks, cfg, "Critical VPN flaw exploited", cfg.get("channel.handle", "@cybershorts"),
                       AUDIO_SECONDS, ass, words=words, keywords=KEYWORDS, beats=beats)
    out = cfg.path("videos", "test_render.mp4")
    render.render(cfg, wav, ass, clips, out, beats=beats)

    def probe(path: Path) -> tuple[int, int, float]:
        txt = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
             "-show_entries", "format=duration", "-of", "default=nw=1", str(path)],
            capture_output=True, text=True, check=True, timeout=30,
        ).stdout
        info = dict(line.split("=", 1) for line in txt.split() if "=" in line)
        return int(info["width"]), int(info["height"]), float(info["duration"])

    w, h, dur = probe(out)
    expect = AUDIO_SECONDS + render.PAD_SECONDS
    assert (w, h) == (1080, 1920), "bad size %dx%d" % (w, h)
    assert abs(dur - expect) < 0.35, "duration %.2f, expected ~%.2f" % (dur, expect)

    # Edge cases: single clip, no beats, no watermark (fallback bg) must still render 1080x1920.
    for label, kwargs in (("one clip", dict(broll=clips[0])), ("no b-roll", dict(broll=None))):
        extra = work / f"edge_{label.replace(' ', '_')}.mp4"
        render.render(cfg, wav, ass, kwargs["broll"], extra)
        ew, eh, _ = probe(extra)
        assert (ew, eh) == (1080, 1920), "%s: bad size %dx%d" % (label, ew, eh)

    # 5 beats, clips of different lengths (one shorter than its segment), watermark and ken_burns on/off.
    import time

    five = []
    for i, dur_s in enumerate((0.5, 1.0, 2.0, 3.0, 1.5)):
        c5 = work / f"five{i}.mp4"
        ff("-f", "lavfi", "-i", f"testsrc2=size=480x270:rate=24:duration={dur_s}", "-pix_fmt", "yuv420p", str(c5))
        five.append(c5)
    beats5 = [0.0, 1.2, 2.6, 4.1, 5.5]
    for kb in (True, False):
        for wm_on in (True, False):
            cfg.raw["broll"] = {**cfg.raw.get("broll", {}), "ken_burns": kb}
            cfg.raw["branding"] = {**cfg.raw.get("branding", {}), "watermark": "brand/logo.png" if wm_on else ""}
            o5 = work / f"five_{kb}_{wm_on}.mp4"
            t0 = time.time()
            render.render(cfg, wav, ass, five, o5, beats=beats5)
            el = time.time() - t0
            w5, h5, d5 = probe(o5)
            assert (w5, h5) == (1080, 1920), "5-clip bad size %dx%d" % (w5, h5)
            assert abs(d5 - expect) < 0.35, "5-clip duration %.2f" % d5
            assert el < 60, "5-clip render too slow: %.1fs" % el
            print("5 clips kb=%s wm=%s: %.1fs" % (kb, wm_on, el))

    # Odd-sized / portrait / landscape sources, and a clip much shorter than its segment.
    for label, size, dur_s in (("odd portrait 721x1279", "721x1279", 1.0), ("landscape 1920x1080", "1920x1080", 1.0),
                               ("short clip", "320x180", 0.2)):
        cs = work / f"src_{size}_{dur_s}.mp4"
        ff("-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30:duration={dur_s}", "-pix_fmt", "yuv444p" if size == "721x1279" else "yuv420p", str(cs))
        for kb in (True, False):
            cfg.raw["broll"] = {**cfg.raw.get("broll", {}), "ken_burns": kb}
            os_ = work / f"edge_src_{label.split()[0]}_{kb}.mp4"
            render.render(cfg, wav, ass, [cs], os_, beats=beats)
            sw, sh, sd = probe(os_)
            assert (sw, sh) == (1080, 1920), "%s: bad size %dx%d" % (label, sw, sh)
            assert abs(sd - expect) < 0.35, "%s: duration %.2f, expected ~%.2f" % (label, sd, expect)

    frames_dir = Path(tmp.name) / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    for name, t in (("hook", 0.5), ("karaoke", 3.0), ("endcard", expect - 1.2)):
        ff("-ss", f"{t:.2f}", "-i", str(out), "-frames:v", "1", str(frames_dir / f"{name}.png"))
    print("OK %dx%d %.2fs (expected ~%.2f); frames rendered and discarded" % (w, h, dur, expect))
    tmp.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
