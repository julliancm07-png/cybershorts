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


def _segments(beats: list[float] | None, n_clips: int, total: float) -> list[float]:
    """Segment durations summing to `total`: from beat starts if valid, else equal splits."""
    n_clips = max(1, n_clips)
    if beats and len(beats) >= 1:
        starts = [0.0] + [float(b) for b in beats[1:]]
        ok = all(b2 > b1 for b1, b2 in zip(starts, starts[1:])) and starts[-1] < total - 0.05
        if ok:
            ends = starts[1:] + [total]
            return [e - s for s, e in zip(starts, ends)]
    return [total / n_clips] * n_clips


def _watermark_path(cfg: Config) -> Path | None:
    wm = cfg.get("branding.watermark", "brand/logo.png")
    if not wm:
        return None
    p = Path(str(wm)).expanduser()
    if not p.is_absolute():
        p = cfg.path(str(wm))
    return p.resolve() if p.is_file() else None


def _logo_ok(path: Path) -> bool:
    """Quick sanity check (ffprobe, 10 s cap) so a corrupt logo can never stall ffmpeg."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False
    parts = r.stdout.strip().split(",")
    return r.returncode == 0 and len(parts) >= 2 and all(p.strip().isdigit() and int(p) > 0 for p in parts[:2])


def render(cfg: Config, audio_wav: Path | str, ass_path: Path | str,
           broll: Path | str | list[Path | str] | None, out_mp4: Path | str, *,
           beats: list[float] | None = None, watermark: bool = True) -> Path:
    w = int(cfg.get("video.width", 1080))
    h = int(cfg.get("video.height", 1920))
    fps = int(cfg.get("video.fps", 30))
    crf = str(cfg.get("video.crf", 23))
    preset = str(cfg.get("video.preset", "veryfast"))
    darken = float(cfg.get("video.darken", 0.35))
    ken_burns = bool(cfg.get("broll.ken_burns", True))

    audio = Path(audio_wav).resolve()
    ass = Path(ass_path).resolve()
    out = Path(out_mp4).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    duration = probe_duration(audio)
    total = duration + PAD_SECONDS

    if broll is None or broll == "":
        raw_clips: list = []
    elif isinstance(broll, (list, tuple)):
        raw_clips = list(broll)
    else:
        raw_clips = [broll]
    clips = [Path(c).resolve() for c in raw_clips if c and Path(c).exists()]

    inputs: list[str] = []
    graph: list[str] = []
    n_inputs = 0
    if clips:
        segs = _segments(beats, len(clips), total)
        labels = []
        for i, seg in enumerate(segs):
            clip = clips[i % len(clips)]
            inputs += ["-stream_loop", "-1", "-t", f"{seg + 0.5:.3f}", "-i", str(clip)]
            if ken_burns:
                bw, bh = int(round(w * 1.12 / 2) * 2), int(round(h * 1.12 / 2) * 2)
                span = bw - w  # = iw-ow after the scale
                travel = min(span, 0.12 * w)
                sign = 1 if i % 2 == 0 else -1
                x = (f"min(max({span / 2:.2f}+{sign * travel:.2f}*(t/{seg:.3f}-0.5),0),{span})")
                geo = (f"scale={bw}:{bh}:force_original_aspect_ratio=increase:force_divisible_by=2,"
                       f"crop={w}:{h}:x='{x}':y='(ih-oh)/2'")
            else:
                geo = (f"scale={w}:{h}:force_original_aspect_ratio=increase:force_divisible_by=2,"
                       f"crop={w}:{h}")
            graph.append(
                f"[{i}:v]{geo},setsar=1,fps={fps},tpad=stop_mode=clone:stop_duration={seg:.3f},trim=duration={seg:.3f},setpts=PTS-STARTPTS,"
                f"eq=brightness={-darken * 0.6:.3f}:saturation=0.7[c{i}]"
            )
            labels.append(f"[c{i}]")
        graph.append(f"{''.join(labels)}concat=n={len(segs)}:v=1:a=0[bg]")
        n_inputs = len(segs)
    else:
        inputs += ["-f", "lavfi", "-i", f"color=c=0x0b1020:s={w}x{h}:r={fps}"]
        graph.append("[0:v]setsar=1[bg]")
        n_inputs = 1

    wm = _watermark_path(cfg) if watermark else None
    if wm and not _logo_ok(wm):
        log.warning("Watermark %s is not a valid image; rendering without it", wm)
        wm = None
    last = "bg"
    if wm:
        size = int(cfg.get("branding.watermark_size", 150))
        opacity = float(cfg.get("branding.watermark_opacity", 0.85))
        inputs += ["-i", str(wm)]
        graph.append(f"[{n_inputs}:v]scale={size}:-1,format=rgba,colorchannelmixer=aa={opacity:.2f}[wm]")
        graph.append("[bg][wm]overlay=x=W-w-36:y=36[ov]")
        last = "ov"
        n_inputs += 1
    graph.append(f"[{last}]format=yuv420p,ass={ass.name}[v]")
    graph.append(f"[{n_inputs}:a]apad=pad_dur={PAD_SECONDS}[a]")

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        *inputs, "-i", str(audio),
        "-filter_complex", ";".join(graph),
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
