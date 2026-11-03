"""Transcription audio locale et gratuite avec faster-whisper (pip install '.[transcription]')."""

from __future__ import annotations

import os
import sys
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


_GPU_ERRORS = ("cuda", "cublas", "cudnn", "libcu", "nvrtc", "gpu")
_cpu_only = False  # passe à True si le GPU est détecté mais inutilisable (bibliothèques CUDA absentes)


@lru_cache(maxsize=1)
def _add_cuda_dlls() -> None:
    """Windows : les DLL CUDA installées par pip (nvidia-cublas-cu12, nvidia-cudnn-cu12) ne sont pas
    dans le PATH ; on les rend visibles pour que le GPU soit utilisable sans autre installation."""
    if sys.platform != "win32":
        return
    import importlib.util

    for pkg in ("nvidia.cublas", "nvidia.cudnn", "nvidia.cuda_runtime", "nvidia.cuda_nvrtc"):
        try:
            spec = importlib.util.find_spec(pkg)
        except (ImportError, ValueError):
            continue
        for root in (spec.submodule_search_locations or []) if spec else []:
            bin_dir = Path(root) / "bin"
            if bin_dir.is_dir():
                os.add_dll_directory(str(bin_dir))
                os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")


@lru_cache(maxsize=2)
def _model(name: str, threads: int = 0, device: str = "auto"):
    _add_cuda_dlls()
    from faster_whisper import WhisperModel

    # int8 : le plus rapide sur CPU ; sur carte NVIDIA, int8_float16 garde la vitesse du GPU.
    compute = "int8_float16" if device == "cuda" else "int8"
    return WhisperModel(name, device=device, compute_type=compute, cpu_threads=threads or default_threads())


def _run(media_path: Path, model_name: str, language: str, threads: int, device: str) -> list[dict]:
    segments, _info = _model(model_name, threads, device).transcribe(
        str(media_path), vad_filter=True, beam_size=1, condition_on_previous_text=False,
        language=language or None,
    )
    out: list[dict] = []
    for s in segments:  # la transcription se fait ici, au fil de la lecture
        text = s.text.strip()
        if not text or (out and out[-1]["text"] == text):  # hallucination répétée
            continue
        out.append({"start": round(s.start, 2), "end": round(s.end, 2), "text": text})
    return out


def transcribe_segments(media_path: Path, model_name: str = "small", language: str = "",
                        threads: int = 0, device: str = "auto") -> list[dict]:
    """→ [{start, end, text}] en secondes. ``media_path`` peut être directement la vidéo MP4 :
    faster-whisper en extrait l'audio lui-même (pas besoin de passer par ffmpeg et un WAV).

    Réglages pour des vidéos courtes : décodage glouton (beam_size=1, ~2× plus rapide),
    VAD pour sauter la musique/les silences, pas de conditionnement sur le texte précédent
    (évite les boucles de répétition typiques de Whisper). ``language`` (ex. « fr ») évite
    l'étape de détection de la langue.

    ``device="auto"`` utilise une carte NVIDIA si elle est utilisable (10 à 20× plus rapide) ;
    si les bibliothèques CUDA manquent, on repasse sur le CPU au lieu d'échouer à chaque vidéo."""
    global _cpu_only
    if _cpu_only and device == "auto":
        device = "cpu"
    try:
        return _run(media_path, model_name, language, threads, device)
    except Exception as e:  # noqa: BLE001
        if device != "auto" or not any(k in str(e).lower() for k in _GPU_ERRORS):
            raise
        _cpu_only = True
        return _run(media_path, model_name, language, threads, "cpu")
