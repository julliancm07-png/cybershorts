"""Staged landscape render for the weekly recap (low RAM: never more than a few decoders at once).

Stage 1: one video-only segment per part (its clips split the part evenly).
Stage 2: concat demuxer, stream copy, into one background video.
Stage 3: one final pass: watermark + ass= captions + narration audio.
"""
from __future__ import annotations

import logging
import math
import subprocess
from pathlib import Path

from . import render
from .config import Config

log = logging.getLogger("cybershorts")

THREADS = ["-threads", "2"]
MIN_PREVIEW_KBPS = 200
THUMB_MAX_BYTES = 2 * 1024 * 1024


def _run(cmd: list[str], *, cwd: Path | None = None, timeout: int = 900, what: str = "ffmpeg") -> None:
    try:
        r = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise RuntimeError("ffmpeg not found; install ffmpeg") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"{what} timed out after {timeout}s") from e
    if r.returncode != 0:
        raise RuntimeError(f"{what} failed (exit {r.returncode}): {(r.stderr or '').strip()[-1500:]}")


def _ff(*args: str) -> list[str]:
    return ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args]


def _cover(w: int, h: int, fps: int, darken: float, ken_burns: bool, seg: float, idx: int) -> str:
    if ken_burns:
        bw, bh = int(round(w * 1.12 / 2) * 2), int(round(h * 1.12 / 2) * 2)
        span = bw - w
        travel = min(span, 0.12 * w)
        sign = 1 if idx % 2 == 0 else -1
        x = f"min(max({span / 2:.2f}+{sign * travel:.2f}*(t/{seg:.3f}-0.5),0),{span})"
        geo = (f"scale={bw}:{bh}:force_original_aspect_ratio=increase:force_divisible_by=2,"
               f"crop={w}:{h}:x='{x}':y='(ih-oh)/2'")
    else:
        geo = f"scale={w}:{h}:force_original_aspect_ratio=increase:force_divisible_by=2,crop={w}:{h}"
    return (f"{geo},setsar=1,fps={fps},tpad=stop_mode=clone:stop_duration={seg:.3f},"
            f"trim=duration={seg:.3f},setpts=PTS-STARTPTS,"
            f"eq=brightness={-darken * 0.6:.3f}:saturation=0.7")


def _segment_cmd(cfg: Config, clips: list[Path], dur: float, out: Path, idx: int) -> list[str]:
    w = int(cfg.get("recap.width", 1920))
    h = int(cfg.get("recap.height", 1080))
    fps = int(cfg.get("recap.fps", 30))
    darken = float(cfg.get("video.darken", 0.35))
    kb = bool(cfg.get("broll.ken_burns", True))
    enc = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p", "-an",
           "-r", str(fps), *THREADS, str(out)]
    if not clips:
        return _ff("-f", "lavfi", "-i", f"color=c=0x0b1020:s={w}x{h}:r={fps}", "-t", f"{dur:.3f}", *enc)
    seg = dur / len(clips)
    inputs: list[str] = []
    graph: list[str] = []
    for i, clip in enumerate(clips):
        inputs += ["-stream_loop", "-1", "-t", f"{seg + 0.5:.3f}", "-i", str(clip)]
        graph.append(f"[{i}:v]{_cover(w, h, fps, darken, kb, seg, idx + i)}[c{i}]")
    graph.append("".join(f"[c{i}]" for i in range(len(clips))) + f"concat=n={len(clips)}:v=1:a=0[bg]")
    return _ff(*inputs, "-filter_complex_threads", "2", "-filter_complex", ";".join(graph),
               "-map", "[bg]", "-t", f"{dur:.3f}", *enc)


def render_recap(cfg: Config, wav: Path | str, ass: Path | str, part_clips: list[list[Path]],
                 part_durations: list[float], out: Path | str, *, prefix: Path | str) -> Path:
    """Render the full landscape recap. `prefix` is a path prefix for temp files ({prefix}_seg_*, {prefix}_bg.mp4)."""
    if len(part_clips) != len(part_durations) or not part_durations:
        raise ValueError("part_clips and part_durations must be the same non-zero length")
    wav, ass, out = Path(wav).resolve(), Path(ass).resolve(), Path(out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    pre = str(Path(str(prefix)).resolve())
    timeout = int(cfg.get("recap.render_timeout", 3600))
    crf = str(cfg.get("recap.crf", 22))
    preset = str(cfg.get("recap.preset", "veryfast"))
    total = float(sum(part_durations))
    seg_files = [Path(f"{pre}_seg_{i}.mp4") for i in range(len(part_durations))]
    list_file = Path(f"{pre}_seg_list.txt")
    bg = Path(f"{pre}_bg.mp4")
    try:
        for i, (clips, dur) in enumerate(zip(part_clips, part_durations)):
            usable = [Path(c).resolve() for c in (clips or []) if c and Path(c).exists()]
            _run(_segment_cmd(cfg, usable, max(float(dur), 0.1), seg_files[i], i * 3),
                 timeout=timeout, what=f"ffmpeg part {i + 1}/{len(seg_files)}")
        list_file.write_text(
            "".join("file '%s'\n" % str(p).replace("'", "'\\''") for p in seg_files), encoding="utf-8")
        _run(_ff("-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", *THREADS, str(bg)),
             timeout=timeout, what="ffmpeg concat")

        inputs = ["-i", str(bg)]
        graph: list[str] = []
        wm = render._watermark_path(cfg)
        if wm and not render._logo_ok(wm):
            log.warning("Watermark %s is not a valid image; rendering without it", wm)
            wm = None
        last, n = "0:v", 1
        if wm:
            size = int(cfg.get("branding.watermark_size", 150))
            opacity = float(cfg.get("branding.watermark_opacity", 0.85))
            inputs += ["-i", str(wm)]
            graph.append(f"[1:v]scale={size}:-1,format=rgba,colorchannelmixer=aa={opacity:.2f}[wm]")
            graph.append("[0:v][wm]overlay=x=W-w-36:y=36[ov]")
            last, n = "ov", 2
        graph.append(f"[{last}]format=yuv420p,ass={ass.name}[v]")
        graph.append(f"[{n}:a]apad=pad_dur={render.PAD_SECONDS}[a]")
        cmd = _ff(*inputs, "-i", str(wav), "-filter_complex_threads", "2", "-filter_complex", ";".join(graph),
                  "-map", "[v]", "-map", "[a]", "-t", f"{total:.3f}",
                  "-c:v", "libx264", "-preset", preset, "-crf", crf, "-maxrate", "8M", "-bufsize", "16M",
                  "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
                  *THREADS, "-movflags", "+faststart", str(out))
        _run(cmd, cwd=ass.parent, timeout=timeout, what="ffmpeg final pass")
    finally:
        for p in [*seg_files, list_file, bg]:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
    return out


def preview_bitrate_kbps(duration: float, max_mb: float, audio_kbps: int, cap_kbps: int) -> int:
    """Video bitrate (kbit/s) keeping the preview (audio included) under max_mb MiB, with 8% headroom."""
    duration = max(float(duration), 1.0)
    fit = math.floor(float(max_mb) * 8192 * 0.92 / duration) - int(audio_kbps)
    return int(max(MIN_PREVIEW_KBPS, min(int(cap_kbps), fit)))


def _preview_pass(cfg: Config, src: Path, out: Path, kbps: int, audio_kbps: int, w: int, h: int, timeout: int) -> None:
    cmd = _ff("-i", str(src), "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2",
              "-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{kbps}k", "-maxrate", f"{int(kbps * 1.3)}k",
              "-bufsize", f"{kbps * 2}k", "-pix_fmt", "yuv420p", "-ac", "1", "-c:a", "aac", "-b:a", f"{audio_kbps}k",
              "-ar", "44100", *THREADS, "-movflags", "+faststart", str(out))
    _run(cmd, timeout=timeout, what="ffmpeg preview")


def render_preview(cfg: Config, src: Path | str, out: Path | str, duration: float) -> Path:
    src, out = Path(src).resolve(), Path(out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    w = int(cfg.get("recap.preview.width", 854))
    h = int(cfg.get("recap.preview.height", 480))
    max_mb = float(cfg.get("recap.preview.max_mb", 45))
    a_kbps = int(cfg.get("recap.preview.audio_kbps", 64))
    cap = int(cfg.get("recap.preview.max_kbps", 1500))
    timeout = int(cfg.get("recap.render_timeout", 3600))
    kbps = preview_bitrate_kbps(duration, max_mb, a_kbps, cap)
    _preview_pass(cfg, src, out, kbps, a_kbps, w, h, timeout)
    if out.stat().st_size > max_mb * 1024 * 1024:
        kbps2 = max(MIN_PREVIEW_KBPS, int(kbps * 0.7))
        log.warning("Preview %.1f MiB > %.0f MiB; retrying at %dk", out.stat().st_size / 1048576, max_mb, kbps2)
        _preview_pass(cfg, src, out, kbps2, a_kbps, w, h, timeout)
    return out


def extract_still(clip: Path | str | None, out_png: Path | str, *, w: int = 1280, h: int = 720,
                  at: float = 1.0) -> Path:
    out = Path(out_png).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    if clip is None or not Path(clip).exists():
        _run(_ff("-f", "lavfi", "-i", f"color=c=0x0b1020:s={w}x{h}", "-frames:v", "1", *THREADS, str(out)),
             timeout=120, what="ffmpeg still")
        return out
    vf = f"scale={w}:{h}:force_original_aspect_ratio=increase:force_divisible_by=2,crop={w}:{h},setsar=1"
    for t in (float(at), 0.0):
        out.unlink(missing_ok=True)
        _run(_ff("-ss", f"{t:.2f}", "-i", str(Path(clip).resolve()), "-frames:v", "1", "-vf", vf, *THREADS, str(out)),
             timeout=120, what="ffmpeg still")
        if out.exists() and out.stat().st_size > 0:
            return out
    raise RuntimeError(f"ffmpeg still produced no frame from {clip}")


def render_thumbnail(cfg: Config, still: Path | str, ass: Path | str, out_jpg: Path | str) -> Path:
    still, ass, out = Path(still).resolve(), Path(ass).resolve(), Path(out_jpg).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    wm = render._watermark_path(cfg)
    if wm and not render._logo_ok(wm):
        wm = None
    inputs = ["-i", str(still)]
    if wm:
        inputs += ["-i", str(wm)]
        graph = ("[1:v]scale=140:-1,format=rgba[wm];[0:v][wm]overlay=x=W-w-30:y=30[ov];"
                 f"[ov]format=yuv420p,ass={ass.name}[v]")
    else:
        graph = f"[0:v]format=yuv420p,ass={ass.name}[v]"
    for q in ("3", "6"):
        _run(_ff(*inputs, "-filter_complex", graph, "-map", "[v]", "-frames:v", "1", "-q:v", q,
                 *THREADS, str(out)), cwd=ass.parent, timeout=120, what="ffmpeg thumbnail")
        if out.stat().st_size <= THUMB_MAX_BYTES:
            break
    return out


# ---------------------------------------------------------------------------
# Newsroom layout: one background frame + one final pass (additions for recap v2).
# ---------------------------------------------------------------------------
_GRADIENTS_OK: bool | None = None


def _has_gradients_filter() -> bool:
    """Detect the `gradients` source once (ffmpeg 5.1+/6.1); cached for the process."""
    global _GRADIENTS_OK
    if _GRADIENTS_OK is None:
        try:
            r = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True, timeout=30)
            _GRADIENTS_OK = r.returncode == 0 and any(
                ln.split()[1:2] == ["gradients"] for ln in (r.stdout or "").splitlines())
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            _GRADIENTS_OK = False
    return _GRADIENTS_OK


def _ffmpeg_color(value: str, default: str) -> str:
    v = str(value or "").strip().lstrip("#")
    if len(v) == 6 and all(c in "0123456789abcdefABCDEF" for c in v):
        return "0x" + v.upper()
    return default


def render_background(cfg: Config, out_png: Path | str, *, w: int = 1920, h: int = 1080,
                      chrome_ass: Path | str | None = None) -> Path:
    """One ffmpeg run -> a single PNG: gradient (or flat colour), light noise vs banding, optional ass chrome."""
    out = Path(out_png).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    cols = cfg.get("recap.newsroom.bg_colors", ["#070B14", "#101A33"])
    cols = list(cols) if isinstance(cols, (list, tuple)) else []
    c0 = _ffmpeg_color(cols[0] if cols else "", "0x070B14")
    c1 = _ffmpeg_color(cols[1] if len(cols) > 1 else "", "0x101A33")
    if _has_gradients_filter():
        src = (f"gradients=s={w}x{h}:c0={c0}:c1={c1}:x0=0:y0=0:x1={w}:y1={h}:nb_colors=2:"
               f"duration=1:speed=0.0001:rate=1")
    else:
        src = f"color=c={c0}:s={w}x{h}:r=1:d=1"
    vf = f"{src},format=yuv420p,noise=alls=3:allf=t+u:all_seed=7,format=rgb24"
    cwd = None
    if chrome_ass:
        ass = Path(chrome_ass).resolve()
        vf += f",ass={ass.name}"
        cwd = ass.parent
    tmp = out.with_name(f"{out.stem}.tmp{out.suffix}")
    try:
        _run(_ff("-f", "lavfi", "-i", vf, "-frames:v", "1", "-update", "1", *THREADS, str(tmp)),
             cwd=cwd, timeout=300, what="ffmpeg background")
        if not tmp.exists() or tmp.stat().st_size == 0:
            raise RuntimeError("ffmpeg background produced no frame")
        tmp.replace(out)
    finally:
        tmp.unlink(missing_ok=True)
    return out


def render_recap_single(cfg: Config, wav: Path | str, ass: Path | str, bg_png: Path | str,
                        total: float, out: Path | str) -> Path:
    """Newsroom final pass: looped background frame (decoded once) + watermark + ass= + narration audio."""
    wav, ass, bg, out = Path(wav).resolve(), Path(ass).resolve(), Path(bg_png).resolve(), Path(out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    fps = int(cfg.get("recap.fps", 30))
    crf = str(cfg.get("recap.crf", 22))
    preset = str(cfg.get("recap.preset", "veryfast"))
    timeout = int(cfg.get("recap.render_timeout", 3600))
    total = float(total)
    inputs = ["-framerate", str(fps), "-i", str(bg)]
    graph = [f"[0:v]format=yuv420p,loop=loop=-1:size=1:start=0,trim=duration={total:.3f},"
             f"setpts=PTS-STARTPTS[bgl]"]
    wm = render._watermark_path(cfg)
    if wm and not render._logo_ok(wm):
        log.warning("Watermark %s is not a valid image; rendering without it", wm)
        wm = None
    last, n = "bgl", 1
    if wm:
        opacity = float(cfg.get("branding.watermark_opacity", 0.85))
        inputs += ["-i", str(wm)]
        graph.append("[1:v]scale=64:64:force_original_aspect_ratio=decrease,format=rgba,"
                     f"colorchannelmixer=aa={opacity:.2f}[wm]")
        graph.append("[bgl][wm]overlay=x=W-60-w:y=40[ov]")
        last, n = "ov", 2
    graph.append(f"[{last}]format=yuv420p,ass={ass.name}[v]")
    graph.append(f"[{n}:a]apad=pad_dur={render.PAD_SECONDS}[a]")
    tmp = out.with_name(f"{out.stem}.tmp{out.suffix}")
    cmd = _ff(*inputs, "-i", str(wav), "-filter_complex_threads", "2", "-filter_complex", ";".join(graph),
              "-map", "[v]", "-map", "[a]", "-t", f"{total:.3f}",
              "-c:v", "libx264", "-preset", preset, "-crf", crf, "-maxrate", "8M", "-bufsize", "16M",
              "-pix_fmt", "yuv420p", "-r", str(fps), "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
              *THREADS, "-movflags", "+faststart", str(tmp))
    try:
        _run(cmd, cwd=ass.parent, timeout=timeout, what="ffmpeg final pass")
        if not tmp.exists():
            raise RuntimeError("ffmpeg final pass produced no output")
        tmp.replace(out)
    finally:
        tmp.unlink(missing_ok=True)
    return out
