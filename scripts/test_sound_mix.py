#!/usr/bin/env python3
"""Offline smoke test for the sound mixer: synthetic voice + synthetic kit -> real ffmpeg -> measurements."""
import math
import os
import subprocess
import sys
import tempfile
import time
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

FAILS: list[str] = []


def check(ok: bool, msg: str) -> None:
    print(("ok   " if ok else "FAIL ") + msg)
    if not ok:
        FAILS.append(msg)


def ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args], check=True, timeout=120)


def duration(p) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                       capture_output=True, text=True, check=True)
    return float(r.stdout)


def peak_db(p) -> float:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(p), "-af", "astats=measure_perchannel=none",
                        "-f", "null", "-"], capture_output=True, text=True, check=True)
    vals = [float(l.split(":")[1]) for l in r.stderr.splitlines() if "Peak level dB" in l]
    return max(vals)


def rms_db(p, start: float, dur: float, band: tuple[float, float] | None = None) -> float:
    """RMS (dBFS) of [start, start+dur], optionally through a band-pass (centre, width Hz)."""
    af = f"bandpass=f={band[0]}:width_type=h:w={band[1]}" if band else "anull"
    pre = ["-ss", f"{max(start - 0.5, 0):.3f}"] if band else ["-ss", f"{start:.3f}"]
    # band-pass: decode 0.5 s of lead-in so the filter has settled, then measure the target window
    lead = start - max(start - 0.5, 0) if band else 0.0
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", *pre, "-t", f"{dur + lead:.3f}",
                        "-i", str(p), "-ac", "1", "-af", af, "-f", "f32le", "-"],
                       capture_output=True, check=True)
    a = array("f")
    a.frombytes(r.stdout)
    a = a[int(lead * 44100):] if band else a
    if not len(a):
        return -120.0
    ms = sum(x * x for x in a) / len(a)
    return 10 * math.log10(ms) if ms > 1e-12 else -120.0


def burst(path: Path, freq: int, seconds: float, amp: float = 0.8) -> None:
    ff("-f", "lavfi", "-i", f"aevalsrc={amp}*sin(2*PI*{freq}*t)*min(1\\,t*50):s=44100:d={seconds}",
       "-c:a", "libmp3lame", "-b:a", "192k", str(path))


def main() -> int:
    tmp = tempfile.TemporaryDirectory()
    os.environ["APP_DIR"] = tmp.name
    from cybershorts import sound
    from cybershorts.config import load

    cfg = load()
    kit = Path(tmp.name) / "kit"
    kit.mkdir()
    cfg.raw.setdefault("sound", {})["folder"] = str(kit)
    work = Path(tmp.name)

    # ---- Short ----
    voice = work / "voice.wav"
    ff("-f", "lavfi", "-i", "aevalsrc=0.9*sin(2*PI*220*t):s=44100:d=7", "-ac", "1", str(voice))
    FREQ = {"hook": 1000, "whoosh": 3000, "outro": 5000}
    for name, f in FREQ.items():
        burst(kit / f"{name}.mp3", f, 0.4)
    beats = [0.0, 2.0, 4.0, 6.0]
    t0 = time.time()
    out = sound.short_sfx(cfg, 1, voice, beats=beats, duration=7.0, title="Big flaw")
    wall = time.time() - t0
    check(out is not None and out.exists(), "short_sfx produced a WAV")
    if out is None:
        return 1
    print(f"     Short mix wall time: {wall:.2f}s")
    check(abs(duration(out) - duration(voice)) <= 0.010, f"duration {duration(out):.3f}s vs voice {duration(voice):.3f}s")
    check(peak_db(out) <= -0.3, f"peak {peak_db(out):.2f} dBFS <= -0.3")
    events = sound.short_events(cfg, beats, 7.0, title="Big flaw")
    print("     events:", events)
    gap = (4.7, 0.6)
    for t, name in events:
        f = FREQ[name]
        on = rms_db(out, t + 0.05, 0.3, (f, 400))
        off = rms_db(out, *gap, (f, 400))
        check(on - off > 6, f"{name}@{t:.2f}: band {f} Hz {on:.1f} dB vs gap {off:.1f} dB (> 6 dB above)")
    for s, d in [(2.5, 1.0), (4.7, 1.0), (5.0, 0.8)]:
        a, b = rms_db(out, s, d), rms_db(voice, s, d)
        check(abs(a - b) <= 0.3, f"voice-only {s}-{s + d}s: {a:.2f} vs {b:.2f} dB")
    check(wall < 5, f"wall time {wall:.2f}s < 5s")

    # ---- Recap ----
    rv = work / "recap_voice.wav"  # voice on for the first 2 s of every 4 s block
    ff("-f", "lavfi", "-i", "aevalsrc=0.9*sin(2*PI*220*t)*lt(mod(t\\,4)\\,2):s=44100:d=60", "-ac", "1", str(rv))
    burst(kit / "theme.mp3", 4000, 8.0, 0.8)
    burst(kit / "bed1.mp3", 1500, 10.0, 0.8)
    burst(kit / "whoosh.mp3", 3000, 0.4)
    burst(kit / "outro.mp3", 5000, 1.5)
    parts = [{"kind": "intro", "start": 0.0, "end": 8.0}, {"kind": "story", "start": 8.0, "end": 30.0},
             {"kind": "story", "start": 30.0, "end": 52.0}, {"kind": "outro", "start": 52.0, "end": 60.0}]
    t0 = time.time()
    rout, has_bed = sound.recap_audio(cfg, rv, parts, work / "recap_mix.wav")
    print(f"     Recap mix wall time: {time.time() - t0:.2f}s")
    check(rout is not None and has_bed, "recap_audio produced WAV with bed")
    if rout is None:
        return 1
    check(abs(duration(rout) - 60.0) <= 0.010, f"recap duration {duration(rout):.3f}s")
    check(peak_db(rout) <= -0.3, f"recap peak {peak_db(rout):.2f} dBFS <= -0.3")
    th_on = rms_db(rout, 1.0, 1.0, (4000, 300))
    th_after = rms_db(rout, 10.0, 1.0, (4000, 300))
    check(th_on - th_after > 20, f"theme present at intro ({th_on:.1f} dB) and gone after ({th_after:.1f} dB)")
    bed_voice = rms_db(rout, 24.3, 1.4, (1500, 100))   # voice on (24-26)
    bed_gap = rms_db(rout, 26.4, 1.4, (1500, 100))     # voice off (26-28)
    check(bed_gap - bed_voice > 6, f"bed ducked under voice: {bed_voice:.1f} dB (voice) vs {bed_gap:.1f} dB (gap)")
    bed_intro = rms_db(rout, 5.0, 1.0, (1500, 100))
    check(bed_intro < bed_gap - 10, f"bed silent during intro ({bed_intro:.1f} dB)")
    wh = rms_db(rout, 7.95, 0.3, (3000, 400))
    check(wh - rms_db(rout, 15.0, 1.0, (3000, 400)) > 6, f"story whoosh at 7.88 ({wh:.1f} dB)")
    st = rms_db(rout, 58.6, 0.4, (5000, 400))
    check(st - rms_db(rout, 20.0, 1.0, (5000, 400)) > 6, f"outro sting at end ({st:.1f} dB)")
    tail = rms_db(rout, 59.0, 0.9)
    check(tail > -60, "tail not cut off")

    tmp.cleanup()
    print("\nFAILED: " + "; ".join(FAILS) if FAILS else "\nAll sound mix checks passed.")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
