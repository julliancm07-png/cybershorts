"""FFmpeg composition."""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from .config import Config

log = logging.getLogger("cybershorts")

PAD_SECONDS = 0.6


def probe_duration(path: Path | str) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError as e:
        raise RuntimeError("ffprobe not found; install ffmpeg") from e
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"ffprobe failed for {path}: {r.stderr.strip()[-500:]}")
    return float(r.stdout.strip())


def render(cfg: Config, audio_wav: Path | str, ass_path: Path | str,
           broll: Path | str | None, out_mp4: Path | str) -> Path:
    w = int(cfg.get("video.width", 1080))
    h = int(cfg.get("video.height", 1920))
    fps = int(cfg.get("video.fps", 30))
    crf = str(cfg.get("video.crf", 23))
    preset = str(cfg.get("video.preset", "veryfast"))
    darken = float(cfg.get("video.darken", 0.35))

    audio = Path(audio_wav).resolve()
    ass = Path(ass_path).resolve()
    out = Path(out_mp4).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    duration = probe_duration(audio)
    total = duration + PAD_SECONDS

    if broll and Path(broll).exists():
        in0 = ["-stream_loop", "-1", "-i", str(Path(broll).resolve())]
        vf = (
            f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,fps={fps},"
            f"eq=brightness={-darken * 0.6:.3f}:saturation=0.7,format=yuv420p,ass={ass.name}[v]"
        )
    else:
        in0 = ["-f", "lavfi", "-i", f"color=c=0x0b1020:s={w}x{h}:r={fps}"]
        vf = f"[0:v]setsar=1,format=yuv420p,ass={ass.name}[v]"

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        *in0, "-i", str(audio),
        "-filter_complex", vf + ";[1:a]apad=pad_dur=%s[a]" % PAD_SECONDS,
        "-map", "[v]", "-map", "[a]",
        "-t", f"{total:.3f}",
        "-c:v", "libx264", "-preset", preset, "-crf", crf, "-maxrate", "6M", "-bufsize", "12M", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-ar", "44100",
        "-movflags", "+faststart", str(out),
    ]
    try:
        r = subprocess.run(cmd, cwd=str(ass.parent), capture_output=True, text=True, timeout=900)
    except FileNotFoundError as e:
        raise RuntimeError("ffmpeg not found; install ffmpeg") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("ffmpeg timed out after 900s") from e
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed (exit {r.returncode}): {r.stderr.strip()[-1500:]}")
    return out
