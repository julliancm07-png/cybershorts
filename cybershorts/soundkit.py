"""Brand sound kit generator (ElevenLabs sound effects + music).

Run via scripts/make_sound_kit.py. The kit lives in data/<sound.folder> (default
brand/sound), is gitignored with the rest of data/, and is stored as mp3 192k.

Everything is configurable under `sound.*`; module-level defaults below are used when
the config does not define a key.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from .alerts import scrub

log = logging.getLogger(__name__)

SFX_URL = "https://api.elevenlabs.io/v1/sound-generation"
MUSIC_URL = "https://api.elevenlabs.io/v1/music"
SUBSCRIPTION_URL = "https://api.elevenlabs.io/v1/user/subscription"

DEFAULT_FOLDER = "brand/sound"
DEFAULT_SFX_MODEL = "eleven_text_to_sound_v3"
DEFAULT_MUSIC_MODEL = "music_v2"
DEFAULT_OUTPUT_FORMAT = "mp3_44100_192"
DEFAULT_INFLUENCE = 0.5
DEFAULT_CREDITS = {"sfx_per_second": 40, "music_per_minute": 900}
SAFETY_FACTOR = 1.5  # refuse to start when remaining credits < estimate * this
LOOP_FADE = 1.5  # seconds of tail-into-head crossfade for loop beds

DEFAULT_KIT = [
    {"name": "hook", "kind": "sfx", "seconds": 1.0, "loop": False,
     "prompt": "short digital glitch impact, punchy opening hit for a cybersecurity news video"},
    {"name": "whoosh", "kind": "sfx", "seconds": 0.6, "loop": False,
     "prompt": "fast airy transition whoosh, clean, digital"},
    {"name": "breaking", "kind": "sfx", "seconds": 1.5, "loop": False,
     "prompt": "urgent alert tone, two-note digital warning, breaking news"},
    {"name": "card", "kind": "sfx", "seconds": 0.5, "loop": False,
     "prompt": "UI data blip, soft digital notification tick"},
    {"name": "outro", "kind": "sfx", "seconds": 2.0, "loop": False,
     "prompt": "signature sting, short dark synth resolve with a soft digital tail"},
    {"name": "theme", "kind": "music", "seconds": 8, "loop": False,
     "prompt": "dark synth cyber signature intro, short, memorable, instrumental, no vocals"},
    {"name": "bed1", "kind": "music", "seconds": 75, "loop": True, "prompt": "instrumental, dark minimal techno bed to sit under narration, steady pulse, sparse, no vocals, no lead melody, no drops, low dynamics"},
    {"name": "bed2", "kind": "music", "seconds": 90, "loop": True, "prompt": "instrumental, subtle synthwave underscore for tech news, warm pads, light arpeggio, steady, no vocals, no drops, low dynamics"},
    {"name": "bed3", "kind": "music", "seconds": 60, "loop": True, "prompt": "instrumental, tense low-key electronic underscore, cybersecurity documentary feel, soft ticking percussion, no vocals, no drops, low dynamics"},
]

MUSIC_TERMS = ("Music terms reminder: ElevenLabs Music output may be used commercially on paid "
               "plans (not for film/TV/radio/studio games), no attribution needed, but no music "
               "libraries or resale. Follow YouTube / TikTok AI-content labelling rules when posting.")


class KitFatal(RuntimeError):
    """The run cannot continue (bad key, no credits, no quota, ffmpeg missing...)."""


class KitItemError(RuntimeError):
    """One item failed; the run continues with the next."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# ---------- config helpers ----------
def kit_items(cfg) -> list[dict]:
    raw = cfg.get("sound.kit") or DEFAULT_KIT
    items = []
    for it in raw:
        items.append({
            "name": str(it["name"]),
            "kind": str(it.get("kind", "sfx")).lower(),
            "prompt": str(it.get("prompt", "")),
            "seconds": float(it.get("seconds", 1.0)),
            "loop": bool(it.get("loop", False)),
        })
    return items


def kit_dir(cfg) -> Path:
    folder = str(cfg.get("sound.folder", DEFAULT_FOLDER) or DEFAULT_FOLDER)
    return Path(cfg.path()) / folder


def _credits(cfg) -> dict:
    out = dict(DEFAULT_CREDITS)
    out.update(cfg.get("sound.credits") or {})
    return out


def estimate(item: dict, variants: int = 1, credits: dict | None = None) -> int:
    """Estimated credits for `variants` generations of one kit item."""
    c = dict(DEFAULT_CREDITS)
    c.update(credits or {})
    secs = float(item.get("seconds", 1.0))
    if str(item.get("kind", "sfx")).lower() == "music":
        one = secs / 60.0 * float(c["music_per_minute"])
    else:
        one = secs * float(c["sfx_per_second"])
    return int(round(one * max(int(variants), 1)))


# ---------- ElevenLabs client ----------
class SoundClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.key = getattr(cfg, "elevenlabs_api_key", "") or ""
        if not self.key:
            raise KitFatal("ELEVENLABS_API_KEY is not set")
        self.fmt = str(cfg.get("sound.output_format", DEFAULT_OUTPUT_FORMAT))
        self.influence = float(cfg.get("sound.prompt_influence", DEFAULT_INFLUENCE))
        self.sfx_model = str(cfg.get("sound.sfx_model", DEFAULT_SFX_MODEL))
        self.music_model = str(cfg.get("sound.music_model", DEFAULT_MUSIC_MODEL))

    def _safe(self, text: str) -> str:
        return scrub(self.cfg, str(text).replace(self.key, "***"))[:200]

    def _headers(self) -> dict:
        return {"xi-api-key": self.key, "Content-Type": "application/json"}

    def _post(self, url: str, body: dict, timeout: int) -> bytes:
        last = None
        status = None
        for attempt in range(3):
            try:
                r = requests.post(url, params={"output_format": self.fmt}, headers=self._headers(),
                                  json=body, timeout=timeout)
            except requests.RequestException as e:
                last = f"network error ({type(e).__name__})"
                status = None
                time.sleep(2 + 3 * attempt)
                continue
            status = r.status_code
            if status == 200:
                data = r.content
                if not isinstance(data, (bytes, bytearray)) or len(data) < 1000 or data[:1] in (b"{", b"["):
                    raise KitItemError("ElevenLabs returned 200 but no audio", 200)
                return bytes(data)
            detail = self._detail(r)
            if status in (401, 402, 403) or "quota" in detail.lower():
                hint = ""
                if "permission" in detail.lower():
                    hint = (" - your API key lacks the Sound Effects / Music permission: elevenlabs.io > "
                            "Developers > API Keys > edit the key and enable them (or make a new key)")
                raise KitFatal(f"ElevenLabs HTTP {status}: {detail or 'not allowed'}{hint}")
            last = f"HTTP {status}: {detail}"
            if status in (429, 500, 502, 503, 504):
                time.sleep(3 + 5 * attempt)
                continue
            break
        raise KitItemError(f"ElevenLabs failed: {last}", status)

    def _detail(self, r) -> str:
        try:
            d = r.json().get("detail")
            if isinstance(d, dict):
                detail = str(d.get("status") or d.get("message") or d)
            elif isinstance(d, list):
                detail = "; ".join(str(x.get("msg", x)) if isinstance(x, dict) else str(x) for x in d)
            else:
                detail = str(d or "")
        except (ValueError, AttributeError):
            detail = str(getattr(r, "text", ""))
        return self._safe(detail)[:120]

    def sfx(self, text: str, seconds: float, *, loop: bool = False, influence: float | None = None,
            model: str | None = None) -> bytes:
        body = {"text": text, "model_id": model or self.sfx_model,
                "duration_seconds": min(max(float(seconds), 0.5), 30.0),
                "prompt_influence": self.influence if influence is None else influence}
        if loop:
            body["loop"] = True
        return self._post(SFX_URL, body, 120)

    def music(self, prompt: str, ms: int, *, instrumental: bool = True, model: str | None = None) -> bytes:
        body = {"prompt": prompt, "model_id": model or self.music_model,
                "music_length_ms": min(max(int(ms), 3000), 600000)}
        if instrumental:
            body["force_instrumental"] = True
        try:
            return self._post(MUSIC_URL, body, 300)
        except KitItemError as e:
            if e.status == 422 and instrumental:
                log.warning("Music request rejected (422); retrying once without force_instrumental")
                body.pop("force_instrumental", None)
                return self._post(MUSIC_URL, body, 300)
            raise

    def balance(self) -> int | None:
        """Remaining credits this period, or None if it cannot be read."""
        try:
            r = requests.get(SUBSCRIPTION_URL, timeout=15, headers={"xi-api-key": self.key})
            if not r.ok:
                return None
            j = r.json()
            return int(j["character_limit"]) - int(j["character_count"])
        except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError):
            return None


# ---------- ffmpeg post-processing ----------
def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except FileNotFoundError:
        raise KitFatal(f"{cmd[0]} is not installed") from None
    except (OSError, subprocess.SubprocessError) as e:
        raise KitItemError(f"{cmd[0]} failed: {type(e).__name__}") from None
    return r


def _probe_duration(path) -> float | None:
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", str(path)],
                           capture_output=True, text=True, timeout=60)
        return float(str(r.stdout).strip())
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None


_OUT = ["-c:a", "libmp3lame", "-b:a", "192k", "-ar", "44100", "-f", "mp3"]
_BASE = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
SFX_TRIM = "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.02"
MUSIC_NORM = "loudnorm=I=-16:TP=-1.5"


def loop_filter(duration: float, fade: float = LOOP_FADE) -> str:
    """filter_complex that crossfades (afade + amix; ffmpeg's acrossfade returns nothing when
    the overlap equals both clips' full length) the tail into the head so the clip loops seamlessly.
    Output is `duration - fade` long; its last sample flows straight into its first."""
    d = float(duration)
    f = float(fade)
    return (f"[0:a]asplit=3[a][b][c];"
            f"[a]atrim=start={f:g}:end={d - f:.3f},asetpts=PTS-STARTPTS[mid];"
            f"[b]atrim=start={d - f:.3f}:end={d:.3f},asetpts=PTS-STARTPTS,afade=t=out:st=0:d={f:g}[tail];"
            f"[c]atrim=start=0:end={f:g},asetpts=PTS-STARTPTS,afade=t=in:st=0:d={f:g}[head];"
            f"[tail][head]amix=inputs=2:duration=longest:normalize=0[x];"
            f"[mid][x]concat=n=2:v=0:a=1,{MUSIC_NORM}[o]")


def postprocess(raw: bytes | Path, out, item: dict) -> Path:
    """Raw API audio -> mp3 192k at `out` (sfx: trimmed + peak -1 dBFS; music: loudnorm,
    seamless loop for loop beds)."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_raw = None
    if isinstance(raw, (bytes, bytearray)):
        tmp_raw = out.with_name(out.stem + ".raw")
        tmp_raw.write_bytes(bytes(raw))
        src = tmp_raw
    else:
        src = Path(raw)
    try:
        if str(item.get("kind", "sfx")).lower() == "music":
            if item.get("loop"):
                dur = _probe_duration(src)
                if not dur or dur <= 2 * LOOP_FADE + 1:
                    raise KitItemError("could not measure music length for loop")
                cmd = _BASE + ["-i", str(src), "-filter_complex", loop_filter(dur), "-map", "[o]"]
            else:
                cmd = _BASE + ["-i", str(src), "-af", MUSIC_NORM]
        else:
            gain = ""
            det = _run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(src),
                        "-af", SFX_TRIM + ",volumedetect", "-f", "null", "-"])
            m = re.search(r"max_volume:\s*(-?[\d.]+) dB", det.stderr or "")
            if det.returncode == 0 and m:
                gain = f",volume={-1.0 - float(m.group(1)):.2f}dB"
            cmd = _BASE + ["-i", str(src), "-af", SFX_TRIM + gain]
        r = _run(cmd + _OUT + [str(out)])
        if r.returncode != 0:
            raise KitItemError("ffmpeg failed: " + (r.stderr or "")[-200:])
        if not out.exists() or out.stat().st_size < 500:
            raise KitItemError("ffmpeg produced no audio")
        return out
    finally:
        if tmp_raw is not None:
            tmp_raw.unlink(missing_ok=True)


# ---------- generation ----------
def _variant_path(d: Path, name: str, n: int) -> Path:
    return d / (f"{name}.mp3" if n <= 1 else f"{name}.v{n}.mp3")


def _swap(d: Path, name: str, k: int) -> None:
    a, b = _variant_path(d, name, 1), _variant_path(d, name, k)
    if k <= 1:
        return
    if not b.exists():
        raise KitFatal(f"{b.name} does not exist; generate variants first")
    if not a.exists():
        b.replace(a)
        return
    tmp = d / f".{name}.swap"
    a.replace(tmp)
    b.replace(a)
    tmp.replace(b)


def _load_manifest(path: Path) -> dict:
    try:
        m = json.loads(path.read_text(encoding="utf-8"))
        return {e["name"]: e for e in m if isinstance(e, dict) and "name" in e}
    except (OSError, ValueError, TypeError, KeyError):
        return {}


def generate(cfg, *, only=None, variants: int = 1, dry_run: bool = False, use: dict | None = None,
             tg=None, client: SoundClient | None = None) -> list[dict]:
    """Generate the kit. Returns one dict per item: name, kind, status
    (ok/skipped/error/dry-run), estimate, files, credits_used, error.

    `use={"hook": 2}` swaps variant 2 into hook.mp3; with no `only` that is all it does.
    Raises KitFatal when the run must stop (auth/credits/unknown names/balance too low).
    """
    variants = max(int(variants or 1), 1)
    items = kit_items(cfg)
    by_name = {it["name"]: it for it in items}
    only = [str(n) for n in only] if only else None
    for n in list(only or []) + list((use or {}).keys()):
        if n not in by_name:
            raise KitFatal(f"unknown kit item {n!r} (known: {', '.join(by_name)})")
    d = kit_dir(cfg)
    credits = _credits(cfg)

    if use and not dry_run:
        d.mkdir(parents=True, exist_ok=True)
        for n, k in use.items():
            _swap(d, n, int(k))
            log.info("%s: variant %s is now %s.mp3", n, k, n)
        if not only:
            return [{"name": n, "kind": by_name[n]["kind"], "status": "swapped", "estimate": 0,
                     "files": [f"{n}.mp3"], "credits_used": None, "error": None} for n in use]

    results: list[dict] = []
    todo: list[dict] = []
    for it in items:
        row = {"name": it["name"], "kind": it["kind"], "seconds": it["seconds"],
               "estimate": estimate(it, variants, credits), "files": [], "credits_used": None, "error": None}
        want = it["name"] in only if only else not _variant_path(d, it["name"], 1).exists()
        if not want:
            row.update(status="skipped", estimate=0)
        else:
            row["status"] = "dry-run" if dry_run else "pending"
            todo.append(it)
        results.append(row)
    if dry_run or not todo:
        return results

    total = sum(r["estimate"] for r in results if r["status"] == "pending")
    client = client or SoundClient(cfg)
    before = client.balance()
    if before is None:
        log.warning("Could not read the ElevenLabs credit balance; continuing without a safety check")
    elif before < SAFETY_FACTOR * total:
        raise KitFatal(f"Not enough credits: {before:,} left, estimated need {total:,} "
                       f"(x{SAFETY_FACTOR} safety = {int(SAFETY_FACTOR * total):,})")

    d.mkdir(parents=True, exist_ok=True)
    manifest_path = d / "manifest.json"
    manifest = _load_manifest(manifest_path)
    try:
        for row in results:
            if row["status"] != "pending":
                continue
            it = by_name[row["name"]]
            made, err, used = [], None, 0
            used_known = before is not None
            for v in range(1, variants + 1):
                final = _variant_path(d, it["name"], v)
                tmp = final.with_name(final.stem + ".tmp.mp3")
                try:
                    if it["kind"] == "music":
                        raw = client.music(it["prompt"], int(round(it["seconds"] * 1000)), instrumental=True)
                    else:
                        raw = client.sfx(it["prompt"], it["seconds"], loop=it["loop"])
                    postprocess(raw, tmp, it)
                    tmp.replace(final)
                    made.append(final)
                except KitItemError as e:
                    err = scrub(cfg, str(e))
                    log.warning("%s v%d failed: %s", it["name"], v, err)
                finally:
                    tmp.unlink(missing_ok=True)
                after = client.balance() if before is not None else None
                if before is not None and after is not None:
                    used += max(before - after, 0)
                    before = after
                else:
                    used_known = False
                    before = after
            row["files"] = [p.name for p in made]
            row["credits_used"] = used if used_known else None
            if made:
                row["status"] = "ok"
                row["error"] = err
                manifest[it["name"]] = {
                    "name": it["name"], "kind": it["kind"], "prompt": it["prompt"],
                    "seconds": it["seconds"], "duration": _probe_duration(made[0]),
                    "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "model": cfg.get("sound.music_model", DEFAULT_MUSIC_MODEL) if it["kind"] == "music"
                    else cfg.get("sound.sfx_model", DEFAULT_SFX_MODEL),
                    "credits_used": row["credits_used"], "variants": row["files"],
                }
                if tg is not None:
                    for p in made:
                        try:
                            tg.send_audio(p, caption=f"{p.stem}", title=p.stem, performer="cybershorts")
                        except Exception as e:  # noqa: BLE001
                            log.warning("Could not send %s to Telegram: %s", p.name, scrub(cfg, str(e)))
            else:
                row["status"] = "error"
                row["error"] = err or "no audio produced"
    finally:
        if manifest:
            tmpm = manifest_path.with_suffix(".tmp")
            tmpm.write_text(json.dumps(list(manifest.values()), indent=1), encoding="utf-8")
            tmpm.replace(manifest_path)
    return results
