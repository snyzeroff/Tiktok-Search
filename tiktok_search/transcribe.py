"""Transcription audio locale et gratuite avec faster-whisper (pip install '.[transcription]')."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def is_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return True


def default_threads() -> int:
    """Tous les cœurs sauf un (gardé pour les téléchargements) ; faster-whisper n'en prend que 4 par défaut."""
    return max(1, min(16, (os.cpu_count() or 4) - 1))


@lru_cache(maxsize=2)
def _model(name: str, threads: int = 0):
    from faster_whisper import WhisperModel

    return WhisperModel(name, device="auto", compute_type="int8", cpu_threads=threads or default_threads())


def transcribe_segments(media_path: Path, model_name: str = "small", language: str = "",
                        threads: int = 0) -> list[dict]:
    """→ [{start, end, text}] en secondes. ``media_path`` peut être directement la vidéo MP4 :
    faster-whisper en extrait l'audio lui-même (pas besoin de passer par ffmpeg et un WAV).

    Réglages pour des vidéos courtes : décodage glouton (beam_size=1, ~2× plus rapide),
    VAD pour sauter la musique/les silences, pas de conditionnement sur le texte précédent
    (évite les boucles de répétition typiques de Whisper). ``language`` (ex. « fr ») évite
    l'étape de détection de la langue."""
    segments, _info = _model(model_name, threads).transcribe(
        str(media_path), vad_filter=True, beam_size=1, condition_on_previous_text=False,
        language=language or None,
    )
    out: list[dict] = []
    for s in segments:
        text = s.text.strip()
        if not text or (out and out[-1]["text"] == text):  # hallucination répétée
            continue
        out.append({"start": round(s.start, 2), "end": round(s.end, 2), "text": text})
    return out
