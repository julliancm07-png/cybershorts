"""Word-timed captions -> ASS subtitle file. Pure helpers import without faster-whisper."""
from __future__ import annotations

import logging
from pathlib import Path

from .config import Config

log = logging.getLogger("cybershorts")

Word = tuple[float, float, str]
Chunk = tuple[float, float, str]

_BREAK_CHARS = (".", "!", "?", ",")


def transcribe_words(cfg: Config, wav: Path | str, prompt: str = "") -> list[Word]:
    from faster_whisper import WhisperModel  # lazy

    model = WhisperModel(
        cfg.get("captions.whisper_model", "base.en"),
        device="cpu",
        compute_type="int8",
        download_root=str(cfg.path("models")),
    )
    segments, _info = model.transcribe(
        str(wav), word_timestamps=True, initial_prompt=(prompt or "")[:800] or None
    )
    words: list[Word] = []
    for seg in segments:
        for w in seg.words or []:
            text = w.word.strip()
            if text:
                words.append((float(w.start), float(w.end), text))
    return words


def chunk_words(words: list[Word], n: int = 3) -> list[Chunk]:
    """Group words into chunks of at most n; also break after words ending in . ! ? ,"""
    n = max(1, int(n))
    chunks: list[Chunk] = []
    cur: list[Word] = []

    def flush() -> None:
        if cur:
            chunks.append((cur[0][0], cur[-1][1], " ".join(w[2] for w in cur)))
            cur.clear()

    for w in words:
        cur.append(w)
        if len(cur) >= n or w[2].rstrip().endswith(_BREAK_CHARS):
            flush()
    flush()
    return chunks


def format_ass_time(t: float) -> str:
    """Seconds -> H:MM:SS.cc"""
    cs = int(round(max(0.0, t) * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def _escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\r", "").replace("\n", "\\N")
    )


def build_ass(chunks: list[Chunk], cfg: Config, title: str, handle: str, duration: float) -> str:
    w = int(cfg.get("video.width", 1080))
    h = int(cfg.get("video.height", 1920))
    font = cfg.get("captions.font", "DejaVu Sans")
    size = int(cfg.get("captions.font_size", 92))
    # ASS colours are &HAABBGGRR
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {w}\nPlayResY: {h}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Caption,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,0,5,60,60,0,1\n"
        f"Style: Title,{font},{int(size * 0.62)},&H0000E5FF,&H0000E5FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,5,0,8,70,70,260,1\n"
        f"Style: Handle,{font},{int(size * 0.4)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,0,2,60,60,150,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    end_all = format_ass_time(max(duration, 0.1))
    lines = [header]
    if title:
        lines.append(f"Dialogue: 0,{format_ass_time(0)},{end_all},Title,,0,0,0,,{_escape(title.upper())}\n")
    if handle:
        lines.append(f"Dialogue: 0,{format_ass_time(0)},{end_all},Handle,,0,0,0,,{_escape(handle)}\n")
    # Caption centred around y ~1150: Alignment 5 (middle-centre) with \pos
    cx, cy = w // 2, int(h * 0.6)
    for start, end, text in chunks:
        lines.append(
            f"Dialogue: 1,{format_ass_time(start)},{format_ass_time(end)},Caption,,0,0,0,,"
            f"{{\\pos({cx},{cy})}}{_escape(text.upper())}\n"
        )
    return "".join(lines)


def write_ass(chunks: list[Chunk], cfg: Config, title: str, handle: str, duration: float,
              out_path: Path | str) -> Path:
    out = Path(out_path)
    out.write_text(build_ass(chunks, cfg, title, handle, duration), encoding="utf-8")
    return out
