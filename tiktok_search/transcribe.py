"""Transcription audio locale et gratuite avec faster-whisper (pip install '.[transcription]')."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path


def is_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return True


@lru_cache(maxsize=2)
def _model(name: str):
    from faster_whisper import WhisperModel

    return WhisperModel(name, device="auto", compute_type="int8")


def transcribe_segments(audio_path: Path, model_name: str = "small") -> list[dict]:
    """→ [{start, end, text}] en secondes."""
    segments, _info = _model(model_name).transcribe(str(audio_path), vad_filter=True)
    return [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()}
            for s in segments if s.text.strip()]
