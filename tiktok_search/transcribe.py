"""Transcription audio locale avec faster-whisper (optionnel : pip install '.[transcription]')."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path


class TranscriptionUnavailable(RuntimeError):
    pass


def is_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return True


@lru_cache(maxsize=2)
def _model(name: str):
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:  # pragma: no cover
        raise TranscriptionUnavailable("faster-whisper n'est pas installé") from e
    return WhisperModel(name, device="auto", compute_type="int8")


def transcribe(audio_path: Path, model_name: str = "small") -> str:
    segments, _info = _model(model_name).transcribe(str(audio_path), vad_filter=True)
    return " ".join(seg.text.strip() for seg in segments).strip()
