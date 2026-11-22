"""Sound kit mixing: SFX over the narration (Shorts) and theme/bed/stings (weekly recap).

Everything here is an audio-only ffmpeg pass on the voice WAV, done before the video render.
Every failure is non-fatal: it is logged (scrubbed) and the caller falls back to the plain voice.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from . import alerts, captions

log = logging.getLogger("cybershorts")

AUDIO_EXTS = (".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac", ".opus")

# Defaults mirror the `sound:` block of config.DEFAULTS (added separately).
DEFAULT_FOLDER = "brand/sound"
DEFAULT_GAIN_DB = {"hook": -10.0, "whoosh": -16.0, "breaking": -12.0, "card": -18.0, "outro": -10.0}
DEFAULT_RECAP = {"theme_db": -12.0, "bed_db": -22.0, "duck": True}
DEFAULT_WHOOSH_LEAD = 0.12
DEFAULT_MIN_GAP = 0.3
DEFAULT_LIMIT = 0.95
# Higher wins when two events sit closer than min_gap.
PRIORITY = {"outro": 5, "breaking": 4, "hook": 3, "card": 2, "whoosh": 1}

_FF = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]


def enabled(cfg) -> bool:
    return bool(cfg.get("sound.enabled", True))


def kit_dir(cfg) -> Path:
    d = Path(str(cfg.get("sound.folder", DEFAULT_FOLDER) or DEFAULT_FOLDER))
    return d if d.is_absolute() else Path(cfg.path(str(d)))


def kit_file(cfg, name: str) -> Path | None:
    d = kit_dir(cfg)
    if not d.is_dir():
        return None
    for ext in AUDIO_EXTS:
        p = d / f"{name}{ext}"
        try:
            if p.is_file() and p.stat().st_size > 0:
                return p
        except OSError:
            continue
    return None


def _gain(cfg, name: str) -> float:
    try:
        return float(cfg.get(f"sound.gain_db.{name}", DEFAULT_GAIN_DB[name]))
    except (TypeError, ValueError):
        return DEFAULT_GAIN_DB[name]


def _f(cfg, key: str, default: float) -> float:
    try:
        return float(cfg.get(f"sound.{key}", default))
    except (TypeError, ValueError):
        return default


def _select(cands: list[tuple[float, str]], min_gap: float) -> list[tuple[float, str]]:
    """Greedy by priority (then time); drop an event within min_gap of an already kept one."""
    kept: list[tuple[float, str]] = []
    for t, name in sorted(cands, key=lambda e: (-PRIORITY[e[1]], e[0])):
        if all(abs(t - k) >= min_gap for k, _n in kept):
            kept.append((t, name))
    return sorted(kept, key=lambda e: (e[0], -PRIORITY[e[1]]))


def short_events(cfg, beats: list[float] | None, duration: float, *, title: str | None = None,
                 badge: str | None = None, card: dict | None = None) -> list[tuple[float, str]]:
    """[(time_s, kit_name)] for a Short, sorted by time. Timings come from captions.overlay_times."""
    ot = captions.overlay_times(cfg, duration, title=title, beats=beats, badge=badge, card=card)
    lead = _f(cfg, "whoosh_lead", DEFAULT_WHOOSH_LEAD)
    gap = _f(cfg, "min_gap", DEFAULT_MIN_GAP)
    cands: list[tuple[float, str]] = []
    if ot["badge_end"] is not None:
        cands.append((0.0, "breaking"))
    elif ot["hook_end"] > 0:
        cands.append((0.0, "hook"))
    for b in list(beats or [])[1:]:
        t = float(b) - lead
        if 0 <= t < duration:
            cands.append((t, "whoosh"))
    if ot["card"] is not None:
        cands.append((float(ot["card"][0]), "card"))
    if ot["end_card"] is not None:
        cands.append((float(ot["end_card"]), "outro"))
    cands = [e for e in cands if kit_file(cfg, e[1]) is not None]
    return _select(cands, gap)


def _afmt(layout: str) -> str:
    return f"aformat=sample_rates=44100:channel_layouts={layout}"


def _event_branches(events: list[tuple[float, Path, float]], first_input: int, layout: str,
                    ) -> tuple[list[str], list[str], list[str]]:
    """(extra -i args, filter chains, output labels): one input per distinct file, asplit per use."""
    files: list[Path] = []
    for _t, p, _g in events:
        if p not in files:
            files.append(p)
    uses = {p: sum(1 for _t, q, _g in events if q == p) for p in files}
    inputs: list[str] = []
    chains: list[str] = []
    labels: dict[Path, list[str]] = {}
    for i, p in enumerate(files):
        idx = first_input + i
        inputs += ["-i", str(p)]
        n = uses[p]
        labels[p] = [f"s{idx}_{k}" for k in range(n)]
        if n == 1:
            chains.append(f"[{idx}:a]anull[{labels[p][0]}]")
        else:
            chains.append(f"[{idx}:a]asplit={n}" + "".join(f"[{x}]" for x in labels[p]))
    outs: list[str] = []
    for j, (t, p, g) in enumerate(events):
        src = labels[p].pop(0)
        ms = max(0, int(round(float(t) * 1000)))
        chains.append(f"[{src}]{_afmt(layout)},volume={float(g):g}dB,adelay={ms}:all=1[e{j}]")
        outs.append(f"e{j}")
    return inputs, chains, outs


def build_sfx_cmd(cfg, voice: Path | str, events: list[tuple[float, Path, float]],
                  out: Path | str) -> list[str]:
    """Pure argv: voice (unity gain) + event SFX, summed (no normalisation) and peak-limited."""
    limit = _f(cfg, "limit", DEFAULT_LIMIT)
    inputs, chains, outs = _event_branches(events, 1, "mono")
    graph = [f"[0:a]{_afmt('mono')}[v]"] + chains
    graph.append("[v]" + "".join(f"[{o}]" for o in outs)
                 + f"amix=inputs={len(outs) + 1}:duration=first:normalize=0:dropout_transition=0,"
                 + f"alimiter=limit={limit:g}:level=disabled:latency=1[a]")
    return [*_FF, "-i", str(voice), *inputs, "-filter_complex", ";".join(graph), "-map", "[a]",
            "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le", str(out)]


def _run(cfg, cmd: list[str], what: str, timeout: int = 180) -> bool:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        log.warning("%s failed (%s)", what, alerts.scrub(cfg, str(e)))
        return False
    if r.returncode != 0:
        log.warning("%s failed: %s", what, alerts.scrub(cfg, (r.stderr or "").strip()[-400:]))
        return False
    return True


def _unlink(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


def sfx_version(cfg, draft_id: int) -> Path:
    return Path(cfg.path("work")) / f"draft_{draft_id}_sfx.wav"


def short_sfx(cfg, draft_id: int, wav: Path | str, *, beats: list[float] | None, duration: float,
              title: str | None = None, overlay: dict | None = None) -> Path | None:
    """Voice WAV with SFX mixed in -> work/draft_{id}_sfx.wav, or None (off / nothing to add / failed)."""
    try:
        if not enabled(cfg):
            return None
        ov = overlay or {}
        evs = short_events(cfg, beats, duration, title=title, badge=ov.get("badge"), card=ov.get("card"))
        events = []
        for t, name in evs:
            p = kit_file(cfg, name)
            if p is not None:
                events.append((t, p, _gain(cfg, name)))
        if not events:
            return None
        out = sfx_version(cfg, draft_id)
        out.parent.mkdir(parents=True, exist_ok=True)
        if not _run(cfg, build_sfx_cmd(cfg, wav, events, out), "SFX mix"):
            _unlink(out)
            return None
        log.info("SFX: %s", ", ".join(f"{n}@{t:.2f}" for t, n in evs))
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("SFX mix failed (%s)", alerts.scrub(cfg, str(e)))
        return None


def tiktok_version(cfg, draft_id: int) -> Path:
    return Path(cfg.path("work")) / f"draft_{draft_id}_tiktok.mp4"


def make_clean_tiktok(cfg, master: Path | str, voice: Path | str, out: Path | str) -> Path | None:
    """Master video (copied) with the plain voice instead of the SFX mix."""
    from .render import PAD_SECONDS
    out = Path(out)
    cmd = [*_FF, "-i", str(master), "-i", str(voice), "-map", "0:v", "-map", "1:a", "-c:v", "copy",
           "-af", f"apad=pad_dur={PAD_SECONDS}", "-c:a", "aac", "-b:a", "160k", "-ar", "44100",
           "-shortest", "-movflags", "+faststart", str(out)]
    if _run(cfg, cmd, "Clean TikTok remux", 300):
        return out
    _unlink(out)
    return None


# ---------- weekly recap ----------
def _intro_end(parts: list[dict]) -> float | None:
    for p in parts:
        if p.get("kind") == "intro":
            return float(p["end"])
    return None


def recap_events(cfg, parts: list[dict], duration: float, *, sting_len: float | None = None,
                 ) -> list[tuple[float, str]]:
    """Whoosh at each story start - lead, outro sting at max(outro.start, duration - sting_len)."""
    lead = _f(cfg, "whoosh_lead", DEFAULT_WHOOSH_LEAD)
    evs: list[tuple[float, str]] = []
    if kit_file(cfg, "whoosh") is not None:
        for p in parts:
            if p.get("kind") == "story":
                t = float(p["start"]) - lead
                if 0 <= t < duration:
                    evs.append((t, "whoosh"))
    if kit_file(cfg, "outro") is not None:
        for p in parts:
            if p.get("kind") == "outro":
                t = float(p["start"])
                if sting_len:
                    t = max(t, duration - float(sting_len))
                if 0 <= t < duration:
                    evs.append((t, "outro"))
                break
    return sorted(evs, key=lambda e: e[0])


def _probe(path: Path | str) -> float | None:
    from .render import probe_duration
    try:
        return probe_duration(path)
    except Exception:  # noqa: BLE001
        return None


def recap_audio(cfg, voice_wav: Path | str, parts: list[dict], out_wav: Path | str,
                ) -> tuple[Path | None, bool]:
    """Theme + ducked bed + whooshes + outro sting around the voice. -> (path|None, has_bed)."""
    try:
        if not enabled(cfg):
            return None, False
        from . import music

        beds = []
        if cfg.get("music.enabled", True):
            for b in music.kit_beds(cfg):
                try:
                    if b.stat().st_size > 0:
                        beds.append(b)
                except OSError:
                    continue
        if not (beds or any(kit_file(cfg, n) for n in ("theme", "whoosh", "outro"))):
            return None, False
        dur = _probe(voice_wav)
        if not dur:
            return None, False
        out = Path(out_wav)
        ie = _intro_end(parts)
        theme = kit_file(cfg, "theme") if ie else None
        bed = music._pick_from(cfg, beds) if beds else None
        sting = kit_file(cfg, "outro")
        sting_len = _probe(sting) if sting else None
        evs = [(t, kit_file(cfg, n), _gain(cfg, n)) for t, n in recap_events(cfg, parts, dur, sting_len=sting_len)]
        evs = [e for e in evs if e[1] is not None]
        if not (theme or bed or evs):
            return None, False
        rc = cfg.get("sound.recap", {}) or {}
        theme_db = float(rc.get("theme_db", DEFAULT_RECAP["theme_db"]))
        bed_db = float(rc.get("bed_db", DEFAULT_RECAP["bed_db"]))
        duck = bool(rc.get("duck", DEFAULT_RECAP["duck"]))
        inputs: list[str] = ["-i", str(voice_wav)]
        graph: list[str] = []
        mix_in: list[str] = []
        n = 1
        stereo = _afmt("stereo")
        if bed:
            graph.append(f"[0:a]{stereo},asplit=2[v][key]")
        else:
            graph.append(f"[0:a]{stereo}[v]")
        mix_in.append("v")
        if theme:
            inputs += ["-i", str(theme)]
            ie_f = float(ie)  # type: ignore[arg-type]
            fade = min(1.0, ie_f)
            graph.append(f"[{n}:a]{stereo},volume={theme_db:g}dB,atrim=0:{ie_f:.3f},asetpts=PTS-STARTPTS,"
                         f"afade=t=out:st={max(ie_f - fade, 0):.3f}:d={fade:.3f}[th]")
            mix_in.append("th")
            n += 1
        if bed:
            start = float(ie) if ie else 0.0
            span = max(dur - start, 0.5)
            fade = min(1.5, span / 4)
            inputs += ["-stream_loop", "-1", "-i", str(bed)]
            ms = int(round(start * 1000))
            graph.append(f"[{n}:a]{stereo},volume={bed_db:g}dB,atrim=0:{span:.3f},asetpts=PTS-STARTPTS,"
                         f"afade=t=in:d={fade:.2f},afade=t=out:st={max(span - fade, 0):.3f}:d={fade:.2f},"
                         f"adelay={ms}:all=1[bd]")
            if duck:
                graph.append("[bd][key]sidechaincompress=threshold=0.02:ratio=6:attack=15:release=450[bed]")
                mix_in.append("bed")
            else:
                mix_in.append("bd")
                graph.append("[key]anullsink")
            n += 1
        if evs:
            ev_in, chains, outs = _event_branches(evs, n, "stereo")  # type: ignore[arg-type]
            inputs += ev_in
            graph += chains
            mix_in += outs
        limit = _f(cfg, "limit", DEFAULT_LIMIT)
        graph.append("".join(f"[{m}]" for m in mix_in)
                     + f"amix=inputs={len(mix_in)}:duration=first:normalize=0:dropout_transition=0,"
                     + f"alimiter=limit={limit:g}:level=disabled:latency=1[a]")
        cmd = [*_FF, *inputs, "-filter_complex", ";".join(graph), "-map", "[a]", "-t", f"{dur:.3f}",
               "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", str(out)]
        out.parent.mkdir(parents=True, exist_ok=True)
        if not _run(cfg, cmd, "Recap audio mix", 300):
            _unlink(out)
            return None, False
        return out, bool(bed)
    except Exception as e:  # noqa: BLE001
        log.warning("Recap audio mix failed (%s)", alerts.scrub(cfg, str(e)))
        return None, False
