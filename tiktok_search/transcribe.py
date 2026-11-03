"""Transcription audio locale et gratuite avec faster-whisper (pip install '.[transcription]')."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
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
    """→ [{start, end, text}] en secondes.

    Réglages pour des vidéos courtes : décodage glouton (beam_size=1, ~2× plus rapide),
    VAD pour sauter la musique/les silences, et pas de conditionnement sur le texte
    précédent (évite les boucles de répétition typiques de Whisper)."""
    segments, _info = _model(model_name).transcribe(
        str(audio_path), vad_filter=True, beam_size=1, condition_on_previous_text=False,
    )
    out: list[dict] = []
    for s in segments:
        text = s.text.strip()
        if not text or (out and out[-1]["text"] == text):  # hallucination répétée
            continue
        out.append({"start": round(s.start, 2), "end": round(s.end, 2), "text": text})
    return out
