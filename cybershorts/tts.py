"""Piper TTS wrapper (lazy imports)."""
from __future__ import annotations

import logging
import subprocess
import sys
import wave
from pathlib import Path

log = logging.getLogger(__name__)


def ensure_voice(cfg) -> Path:
    name = cfg.get("voice.piper_voice", "en_US-ryan-high")
    voices_dir = Path(cfg.path("voices"))
    voices_dir.mkdir(parents=True, exist_ok=True)
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


def synthesize(cfg, text: str, out_wav) -> Path:
    from piper import PiperVoice, SynthesisConfig  # lazy

    onnx = ensure_voice(cfg)
    voice = PiperVoice.load(str(onnx))
    syn = SynthesisConfig(length_scale=float(cfg.get("voice.length_scale", 0.92)))
    out_wav = Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_wav), "wb") as wf:
        voice.synthesize_wav(text, wf, syn_config=syn)
    if not out_wav.exists() or out_wav.stat().st_size < 1000:
        raise RuntimeError("Piper produced no audio")
    return out_wav
