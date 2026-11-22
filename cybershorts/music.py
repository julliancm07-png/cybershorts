"""Background music bed: a quiet track under the narration that ducks while the voice talks.

Put licensed tracks (e.g. from the YouTube Studio Audio Library) in data/music/.
The clean (no-music) render stays the master; a music version is mixed from it in a
few seconds (video is copied, only audio is re-encoded). By default the music version
goes to YouTube and the Telegram preview, and TikTok gets the clean version so you can
add a TikTok-licensed sound in the app.
"""
from __future__ import annotations

import logging
import random
import re
import subprocess
from pathlib import Path

log = logging.getLogger("cybershorts")

AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac", ".opus"}


def music_dir(cfg) -> Path:
    d = Path(str(cfg.get("music.folder", "music")))
    return d if d.is_absolute() else Path(cfg.path(str(d)))


def list_tracks(cfg) -> list[Path]:
    d = music_dir(cfg)
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXTS)


KIT_BED_EXTS = {".mp3", ".wav", ".m4a", ".ogg", ".flac"}
_BED_RE = re.compile(r"^bed\d*\.(mp3|wav|m4a|ogg|flac)$", re.I)


def kit_beds(cfg) -> list[Path]:
    """Sound-kit beds: files named bed.<ext> or bed<N>.<ext> in the sound kit folder, sorted.

    Variants such as bed1.v2.mp3 or partial files like bed2.tmp.mp3 are not beds.
    """
    d = cfg.path() / cfg.get("sound.folder", "brand/sound")
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir()
                  if p.is_file() and _BED_RE.match(p.name))


def _pick_from(cfg, tracks: list[Path]) -> Path:
    """Random choice, never the same track twice in a row (remembered in music_last.txt)."""
    last_file = Path(cfg.path("music_last.txt"))
    last = last_file.read_text().strip() if last_file.exists() else ""
    pool = [t for t in tracks if t.name != last] or tracks
    choice = random.choice(pool)
    try:
        last_file.write_text(choice.name)
    except OSError:
        pass
    return choice


def pick_track(cfg) -> Path | None:
    """Random track, never the same one twice in a row.

    With the sound kit on (sound.enabled, music.prefer_kit) and bed* files present, the choice
    is among the kit beds; otherwise the music folder is used as before.
    """
    if cfg.get("sound.enabled", True) and cfg.get("music.prefer_kit", True):
        beds = kit_beds(cfg)
        if beds:
            return _pick_from(cfg, beds)
    tracks = list_tracks(cfg)
    if not tracks:
        return None
    return _pick_from(cfg, tracks)


def _duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                       capture_output=True, text=True, timeout=60)
    try:
        return float(r.stdout.strip())
    except ValueError as e:
        raise RuntimeError(f"ffprobe could not read {path.name}") from e


def mix(cfg, video: Path | str, track: Path | str, out: Path | str) -> Path:
    """Copy the video stream and mix `track` under its audio, ducked by the voice."""
    video, track, out = Path(video), Path(track), Path(out)
    dur = _duration(video)
    vol = float(cfg.get("music.volume_db", -20))
    fade = min(1.5, dur / 4)
    if cfg.get("music.duck", True):
        bed = ("[0:a]asplit=2[voice][key];"
               f"[1:a]volume={vol}dB,afade=t=in:d={fade:.2f}[m];"
               "[m][key]sidechaincompress=threshold=0.02:ratio=6:attack=15:release=450[bed];")
    else:
        bed = f"[0:a]anull[voice];[1:a]volume={vol}dB,afade=t=in:d={fade:.2f}[bed];"
    graph = (bed + "[voice][bed]amix=inputs=2:duration=first:normalize=0,"
             f"afade=t=out:st={max(dur - fade, 0):.2f}:d={fade:.2f},alimiter=limit=0.95[a]")
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(video), "-stream_loop", "-1", "-i", str(track),
           "-filter_complex", graph, "-map", "0:v", "-map", "[a]",
           "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-ar", "44100",
           "-t", f"{dur:.3f}", "-movflags", "+faststart", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"music mix failed: {r.stderr.strip()[-600:]}")
    return out


def music_version(cfg, draft_id: int) -> Path:
    return Path(cfg.path("work")) / f"draft_{draft_id}_music.mp4"


def add_music(cfg, draft_id: int, clean: Path) -> tuple[Path | None, str]:
    """Make the music version of a draft. Returns (path or None, track name or reason)."""
    if not cfg.get("music.enabled", True):
        return None, "off"
    track = pick_track(cfg)
    if track is None:
        return None, "no tracks in " + str(music_dir(cfg))
    try:
        out = mix(cfg, clean, track, music_version(cfg, draft_id))
        log.info("Music bed: %s", track.name)
        return out, track.stem
    except Exception as e:  # noqa: BLE001  (music is optional; never fail a draft over it)
        log.warning("Music mix failed (%s); using the clean version", e)
        return None, "mix failed"
