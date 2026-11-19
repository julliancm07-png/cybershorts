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

import logging
import re
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


def ensure_voice(cfg) -> Path:
    engine = str(cfg.get("voice.engine", "kokoro")).lower()
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

        self.v = PiperVoice.load(str(ensure_voice(cfg)))
        self.syn = SynthesisConfig(length_scale=float(cfg.get("voice.length_scale", 0.92)))

    def say(self, sentence: str):
        pcm, sr = array("h"), 22050
        for chunk in self.v.synthesize(sentence, syn_config=self.syn):
            sr = chunk.sample_rate
            pcm.frombytes(chunk.audio_int16_bytes)
        return pcm, sr


def _polish(raw: Path, out: Path) -> None:
    """Gentle cleanup: trim edges, de-rumble, light compression, loudness to -16 LUFS."""
    af = ("silenceremove=start_periods=1:start_threshold=-50dB,"
          "highpass=f=70,"
          "acompressor=threshold=-20dB:ratio=2.5:attack=5:release=120:makeup=2,"
          "loudnorm=I=-16:TP=-1.5:LRA=9")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw),
           "-af", af, "-ar", "24000", "-ac", "1", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg polish failed: " + r.stderr[-400:])


def synthesize(cfg, text: str, out_wav) -> Path:
    engine_name = str(cfg.get("voice.engine", "kokoro")).lower()
    ensure_voice(cfg)
    engine = _Kokoro(cfg) if engine_name == "kokoro" else _Piper(cfg)
    s_pause = float(cfg.get("voice.sentence_pause", 0.22))
    p_pause = float(cfg.get("voice.paragraph_pause", 0.45))
    spoken = apply_pronunciations(clean_for_speech(text), cfg.get("voice.pronunciations") or {})

    pcm, sr = array("h"), None
    beats = split_beats(spoken)
    for pi, para in enumerate(beats):
        for si, sentence in enumerate(para):
            seg, seg_sr = engine.say(sentence)
            if sr is None:
                sr = seg_sr
            pcm.extend(seg)
            last = pi == len(beats) - 1 and si == len(para) - 1
            if not last:
                gap = p_pause if si == len(para) - 1 else s_pause
                pcm.extend(array("h", bytes(2 * int(sr * gap))))
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
    return out_wav
