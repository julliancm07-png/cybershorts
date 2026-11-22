"""Text-to-speech with natural pacing.

Engines (voice.engine):
  kokoro  - Kokoro-82M via kokoro-onnx. Much more natural; ~1x real time on a laptop CPU. Default.
  piper   - Piper. Faster, flatter.

Both engines synthesize one sentence at a time and join them with short silences
(voice.sentence_pause) and longer ones between paragraphs (voice.paragraph_pause),
which is where most of the "human" rhythm comes from. A pronunciation table
(voice.pronunciations) is applied to the spoken text only; captions keep the script's
spelling. The result is loudness-normalized with ffmpeg (voice.polish).
"""
from __future__ import annotations

import json
import logging
import re
import time
import subprocess
import sys
import wave
from array import array
from pathlib import Path

import requests

log = logging.getLogger(__name__)

KOKORO_BASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
KOKORO_FILES = {"model": "kokoro-v1.0.int8.onnx", "voices": "voices-v1.0.bin"}

_SENT_SPLIT = re.compile(r"(?<=[.!?])[\"')\]]*\s+(?=[\"'(\[]?[A-Z0-9])")


# ---------- text prep (pure, unit-tested) ----------
def split_beats(text: str) -> list[list[str]]:
    """Paragraphs (blank-line or newline separated) -> sentences."""
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n", text or "") if p.strip()]
    out = []
    for p in paras:
        sents = [s.strip() for s in _SENT_SPLIT.split(p) if s.strip()]
        if sents:
            out.append(sents)
    return out


def apply_pronunciations(text: str, table: dict | None) -> str:
    """Whole-word, case-sensitive replacements for the spoken text only."""
    for src, dst in (table or {}).items():
        if not src:
            continue
        text = re.sub(rf"(?<![\w-]){re.escape(str(src))}(?![\w-])", str(dst), text)
    return text


def clean_for_speech(text: str) -> str:
    text = re.sub(r"https?://\S+", "", text)
    text = text.replace("&", " and ").replace("%", " percent").replace("/", " or ")
    text = re.sub(r"[()\[\]{}*_#]", "", text)
    return re.sub(r"[ \t]+", " ", text).strip()


# ---------- model files ----------
def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    tmp.replace(dest)


def _kokoro_paths(cfg) -> tuple[Path, Path]:
    d = Path(cfg.path("voices")) / "kokoro"
    return d / KOKORO_FILES["model"], d / KOKORO_FILES["voices"]


def ensure_voice(cfg, engine: str | None = None) -> Path | None:
    """Make sure local voice files exist. ElevenLabs needs none (returns None)."""
    engine = str(engine or cfg.get("voice.engine", "kokoro")).lower()
    if engine == "elevenlabs":
        fb = str(cfg.get("voice.fallback_engine", "kokoro") or "").lower()
        return ensure_voice(cfg, fb) if fb and fb != "elevenlabs" else None
    if engine == "kokoro":
        model, voices = _kokoro_paths(cfg)
        for p in (model, voices):
            if not p.exists():
                log.info("Downloading Kokoro file %s", p.name)
                try:
                    _download(KOKORO_BASE + p.name, p)
                except Exception as e:  # noqa: BLE001
                    raise RuntimeError(f"Could not download Kokoro file {p.name}: {e}") from e
        return model
    name = cfg.get("voice.piper_voice", "en_US-ryan-high")
    voices_dir = Path(cfg.path("voices"))
    onnx = voices_dir / f"{name}.onnx"
    if not onnx.exists():
        log.info("Downloading Piper voice %s", name)
        try:
            subprocess.run(
                [sys.executable, "-m", "piper.download_voices", name, "--data-dir", str(voices_dir)],
                check=True,
                timeout=900,
            )
        except (subprocess.SubprocessError, OSError) as e:
            raise RuntimeError(f"Could not download Piper voice {name}: {e}") from e
        if not onnx.exists():
            raise RuntimeError(f"Piper voice download finished but {onnx} is missing")
    return onnx


# ---------- engines: each returns (int16 samples as array('h'), sample_rate) per sentence ----------
class EngineUnavailable(RuntimeError):
    """The engine can't be used right now (no key, out of credits, service down)."""


class _ElevenLabs:
    """ElevenLabs API. Uses previous/next text so prosody flows across beats."""

    context = True
    URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice}"

    def __init__(self, cfg):
        self.key = getattr(cfg, "elevenlabs_api_key", "") or ""
        if not self.key:
            raise EngineUnavailable("ELEVENLABS_API_KEY is not set")
        self.voice = str(cfg.get("voice.elevenlabs.voice_id", "JBFqnCBsd6RMkjVDRZzb"))
        self.model = str(cfg.get("voice.elevenlabs.model", "eleven_multilingual_v2"))
        self.settings = {
            "stability": float(cfg.get("voice.elevenlabs.stability", 0.45)),
            "similarity_boost": float(cfg.get("voice.elevenlabs.similarity_boost", 0.8)),
            "style": float(cfg.get("voice.elevenlabs.style", 0.15)),
            "use_speaker_boost": bool(cfg.get("voice.elevenlabs.speaker_boost", True)),
        }
        speed = float(cfg.get("voice.elevenlabs.speed", 1.0))
        if abs(speed - 1.0) > 1e-6:
            self.settings["speed"] = speed

    def say(self, text: str, previous_text: str = "", next_text: str = ""):
        body = {"text": text, "model_id": self.model, "voice_settings": self.settings}
        if previous_text:
            body["previous_text"] = previous_text[-1000:]
        if next_text:
            body["next_text"] = next_text[:1000]
        last = None
        for attempt in range(3):
            try:
                r = requests.post(self.URL.format(voice=self.voice), params={"output_format": "pcm_24000"},
                                  headers={"xi-api-key": self.key, "Content-Type": "application/json"},
                                  json=body, timeout=120)
            except requests.RequestException as e:
                last = f"network error ({type(e).__name__})"
                time.sleep(2 + 3 * attempt)
                continue
            if r.status_code == 200 and len(r.content) > 1000:
                pcm = array("h")
                pcm.frombytes(r.content[: len(r.content) // 2 * 2])
                if sys.byteorder == "big":
                    pcm.byteswap()
                return pcm, 24000
            detail = ""
            try:
                d = r.json().get("detail")
                detail = str(d.get("status") or d.get("message") or d) if isinstance(d, dict) else str(d or "")
            except (ValueError, AttributeError):
                detail = str(getattr(r, "text", ""))
            detail = detail[:120]
            if r.status_code in (401, 402, 403) or "quota" in detail.lower():
                raise EngineUnavailable(f"ElevenLabs HTTP {r.status_code}: {detail or 'not allowed'}")
            last = f"HTTP {r.status_code}: {detail}"
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(3 + 5 * attempt)
                continue
            break
        raise EngineUnavailable(f"ElevenLabs failed: {last}")


class _Kokoro:
    def __init__(self, cfg):
        from kokoro_onnx import Kokoro  # lazy

        model, voices = _kokoro_paths(cfg)
        self.k = Kokoro(str(model), str(voices))
        self.voice = cfg.get("voice.kokoro_voice", "am_michael")
        self.speed = float(cfg.get("voice.speed", 1.05))

    def say(self, sentence: str):
        import numpy as np  # installed with kokoro-onnx

        samples, sr = self.k.create(sentence, voice=self.voice, speed=self.speed, lang="en-us")
        pcm = array("h")
        pcm.frombytes(np.clip(np.asarray(samples) * 32767, -32768, 32767).astype("<i2").tobytes())
        return pcm, int(sr)


class _Piper:
    def __init__(self, cfg):
        from piper import PiperVoice, SynthesisConfig  # lazy

        self.v = PiperVoice.load(str(ensure_voice(cfg, "piper")))
        self.syn = SynthesisConfig(length_scale=float(cfg.get("voice.length_scale", 0.92)))

    def say(self, sentence: str):
        pcm, sr = array("h"), 22050
        for chunk in self.v.synthesize(sentence, syn_config=self.syn):
            sr = chunk.sample_rate
            pcm.frombytes(chunk.audio_int16_bytes)
        return pcm, sr


def _polish(raw: Path, out: Path) -> None:
    """Gentle cleanup: trim edges, de-rumble, light compression, loudness to -16 LUFS."""
    af = ("highpass=f=70,"
          "equalizer=f=180:t=q:w=1.0:g=1.5,"          # a touch of chest warmth
          "equalizer=f=6500:t=q:w=1.2:g=-2,"          # soften harsh sibilance
          "acompressor=threshold=-22dB:ratio=2:attack=15:release=200:makeup=2,"
          "loudnorm=I=-16:TP=-1.5:LRA=9")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw),
           "-af", af, "-ar", "24000", "-ac", "1", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg polish failed: " + r.stderr[-400:])


def _shape(seg: array, sr: int, fade_in: float = 0.008, fade_out: float = 0.03,
           tail: float = 0.08, thresh: int = 300) -> array:
    """Trim dead air at both ends (with a safety margin), fade the edges so words
    never end on a click or a chopped syllable, and add a short natural tail."""
    import numpy as np

    x = np.frombuffer(seg.tobytes(), dtype="<i2").astype(np.float32)
    if x.size == 0:
        return seg
    loud = np.flatnonzero(np.abs(x) > thresh)
    if loud.size:
        margin = int(sr * 0.04)
        x = x[max(0, loud[0] - margin): min(x.size, loud[-1] + int(sr * 0.12))]
    n_in, n_out = min(int(sr * fade_in), x.size // 4), min(int(sr * fade_out), x.size // 4)
    if n_in:
        x[:n_in] *= np.linspace(0.0, 1.0, n_in, dtype=np.float32)
    if n_out:
        x[-n_out:] *= np.linspace(1.0, 0.0, n_out, dtype=np.float32) ** 2
    x = np.concatenate([x, np.zeros(int(sr * tail), dtype=np.float32)])
    out = array("h")
    out.frombytes(np.clip(x, -32768, 32767).astype("<i2").tobytes())
    return out


def _gap(sr: int, seconds: float, room_tone_db) -> array:
    """A pause filled with faint, soft room tone instead of digital silence."""
    import numpy as np

    n = int(sr * seconds)
    if room_tone_db is None or n <= 0:
        return array("h", bytes(2 * max(n, 0)))
    rng = np.random.default_rng(n)
    noise = rng.standard_normal(n + 64).astype(np.float32)
    noise = np.convolve(noise, np.ones(64, dtype=np.float32) / 8.0, mode="valid")[:n]  # soften the hiss
    noise *= (10 ** (float(room_tone_db) / 20.0) * 32767) / (np.std(noise) or 1.0)
    out = array("h")
    out.frombytes(np.clip(noise, -32768, 32767).astype("<i2").tobytes())
    return out


def _make_engine(cfg, name: str):
    if name == "elevenlabs":
        return _ElevenLabs(cfg)
    if name == "piper":
        return _Piper(cfg)
    ensure_voice(cfg, "kokoro")
    return _Kokoro(cfg)


def synthesize(cfg, text: str, out_wav) -> Path:
    engine_name = str(cfg.get("voice.engine", "kokoro")).lower()
    fallback = str(cfg.get("voice.fallback_engine", "kokoro") or "").lower()
    try:
        return _synthesize_with(cfg, engine_name, text, out_wav)
    except EngineUnavailable as e:
        if not fallback or fallback == engine_name:
            raise RuntimeError(str(e)) from None
        log.warning("%s; falling back to %s for this draft", e, fallback)
        return _synthesize_with(cfg, fallback, text, out_wav)


def _synthesize_with(cfg, engine_name: str, text: str, out_wav) -> Path:
    engine = _make_engine(cfg, engine_name)
    s_pause = float(cfg.get("voice.sentence_pause", 0.22))
    p_pause = float(cfg.get("voice.paragraph_pause", 0.45))
    room = cfg.get("voice.room_tone_db", -62)
    by_paragraph = str(cfg.get("voice.chunking", "paragraph")).lower() != "sentence"
    spoken = apply_pronunciations(clean_for_speech(text), cfg.get("voice.pronunciations") or {})

    # paragraph mode: one generation per beat, so sentences flow with natural intonation;
    # sentence mode: one generation per sentence (more control, choppier).
    beats = split_beats(spoken)
    units = [[" ".join(p)] for p in beats] if by_paragraph else beats
    pcm, sr = array("h"), None
    spans: list[list[float]] = []  # per paragraph: [start_s, end_s] of speech in the final audio
    for pi, para in enumerate(units):
        for si, chunk in enumerate(para):
            if getattr(engine, "context", False):
                prev_txt = " ".join(" ".join(u) for u in units[:pi]) + (" " + " ".join(para[:si]) if si else "")
                next_txt = " ".join(para[si + 1:]) + " " + " ".join(" ".join(u) for u in units[pi + 1:])
                seg, seg_sr = engine.say(chunk, previous_text=prev_txt.strip(), next_text=next_txt.strip())
            else:
                seg, seg_sr = engine.say(chunk)
            if sr is None:
                sr = seg_sr
            start = len(pcm)
            shaped = _shape(seg, sr)
            pcm.extend(shaped)
            speech_end = len(pcm) - int(sr * 0.08)  # minus the tail _shape added
            if si == 0:
                spans.append([start / sr, speech_end / sr])
            else:
                spans[-1][1] = speech_end / sr
            last = pi == len(units) - 1 and si == len(para) - 1
            if not last:
                gap = p_pause if si == len(para) - 1 else s_pause
                pcm.extend(_gap(sr, max(gap - 0.08, 0.0), room))  # _shape already added an 80 ms tail
    if not pcm or sr is None:
        raise RuntimeError(f"{engine_name} produced no audio")

    out_wav = Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    raw = out_wav.with_name(out_wav.stem + "_raw.wav")
    with wave.open(str(raw), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())
    if cfg.get("voice.polish", True):
        try:
            _polish(raw, out_wav)
            raw.unlink(missing_ok=True)
        except Exception as e:  # noqa: BLE001
            log.warning("Audio polish failed (%s); using raw audio", e)
            raw.replace(out_wav)
    else:
        raw.replace(out_wav)
    if out_wav.stat().st_size < 1000:
        raise RuntimeError(f"{engine_name} produced no audio")
    # Exact beat timings, used for caption alignment (captions.transcribe_by_spans)
    spans_path(out_wav).write_text(json.dumps([[round(a, 3), round(b, 3)] for a, b in spans]))
    return out_wav


def spans_path(wav) -> Path:
    wav = Path(wav)
    return wav.with_name(wav.stem + ".spans.json")


def load_spans(wav) -> list[list[float]] | None:
    p = spans_path(wav)
    try:
        spans = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(spans, list) or not all(isinstance(x, list) and len(x) == 2 for x in spans):
        return None
    return [[float(a), float(b)] for a, b in spans]
