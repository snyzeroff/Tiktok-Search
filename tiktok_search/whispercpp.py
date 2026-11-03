"""Transcription sur carte graphique AMD (ou Intel, NVIDIA) avec whisper.cpp + Vulkan.

faster-whisper ne sait utiliser que les cartes NVIDIA. whisper.cpp, compilé avec Vulkan, utilise
n'importe quelle carte récente sous Windows, dont les Radeon. L'app lance une fois
``whisper-server`` (le modèle reste chargé sur la carte), puis lui envoie chaque vidéo.

Installation : télécharger le .zip « whisper.cpp Vulkan » des Releases du dépôt (compilé par
GitHub Actions, voir ``.github/workflows/whisper-vulkan.yml``) et le décompresser dans le dossier
``whisper-vulkan`` du projet (ou indiquer le dossier dans ``TTS_WHISPERCPP_DIR``). Le modèle est
téléchargé automatiquement au premier usage.
"""

from __future__ import annotations

import atexit
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{name}.bin"
EXE = "whisper-server.exe" if sys.platform == "win32" else "whisper-server"


class WhisperCppError(RuntimeError):
    """Échec sur une vidéo (pas de piste audio, réponse invalide…)."""


class WhisperCppUnavailable(WhisperCppError):
    """Le moteur lui-même est inutilisable (programme absent, ne démarre pas, modèle introuvable)."""


def find_server(directory: str | Path) -> Path | None:
    """Cherche whisper-server dans le dossier (ou un sous-dossier, selon le .zip)."""
    d = Path(directory).expanduser()
    if not d.is_dir():
        return None
    direct = d / EXE
    if direct.is_file():
        return direct
    found = sorted(d.rglob(EXE))
    return found[0] if found else None


def to_wav16k(media: Path, dest: Path) -> Path:
    """Piste audio de la vidéo → WAV mono 16 kHz 16 bits (le format attendu par whisper.cpp),
    avec PyAV (installé avec faster-whisper) : pas besoin de ffmpeg."""
    import av

    resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=16000)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with av.open(str(media)) as container, wave.open(str(dest), "wb") as out:
        if not container.streams.audio:
            raise WhisperCppError("pas de piste audio")
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        for frame in container.decode(audio=0):
            for f in resampler.resample(frame):
                out.writeframes(bytes(f.planes[0])[: f.samples * 2])
        for f in resampler.resample(None):  # vide le tampon du rééchantillonneur
            out.writeframes(bytes(f.planes[0])[: f.samples * 2])
    return dest


def ensure_model(name_or_path: str, models_dir: Path) -> Path:
    """Chemin d'un fichier .bin, ou nom de modèle (ex. large-v3-turbo-q5_0) téléchargé une fois."""
    p = Path(name_or_path).expanduser()
    if p.suffix == ".bin" and p.is_file():
        return p
    dest = models_dir / f"ggml-{name_or_path}.bin"
    if dest.is_file() and dest.stat().st_size > 0:
        return dest
    models_dir.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    url = MODEL_URL.format(name=name_or_path)
    log.warning("téléchargement du modèle whisper.cpp %s (une seule fois)…", name_or_path)
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=httpx.Timeout(60, connect=20)) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
        os.replace(tmp, dest)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise WhisperCppUnavailable(f"modèle {name_or_path} non téléchargé ({url}) : {e}. Autre nom possible : "
                                    "TTS_WHISPERCPP_MODEL=large-v3-turbo, ou le chemin d'un fichier .bin") from e
    return dest


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    """Un whisper-server lancé à la demande et gardé ouvert (modèle chargé une seule fois)."""

    def __init__(self, exe: Path, model: Path, language: str = "", log_dir: Path | None = None):
        self.exe, self.model, self.language, self.log_dir = exe, model, language, log_dir
        self.proc: subprocess.Popen | None = None
        self.port = 0
        self._lock = threading.Lock()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self, timeout: float = 180) -> None:
        with self._lock:
            if self.proc and self.proc.poll() is None:
                return
            self.port = _free_port()
            logf = open(self.log_dir / "whisper-server.log", "ab") if self.log_dir else subprocess.DEVNULL
            cmd = [str(self.exe), "-m", str(self.model), "--host", "127.0.0.1", "--port", str(self.port),
                   "-l", self.language or "auto",  # le serveur suppose l'anglais par défaut
                   "-t", str(max(1, min(8, (os.cpu_count() or 4) - 1)))]
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            try:
                self.proc = subprocess.Popen(cmd, cwd=str(self.exe.parent), stdout=logf, stderr=subprocess.STDOUT,
                                             creationflags=flags)
            except OSError as e:
                raise WhisperCppUnavailable(f"whisper-server ne se lance pas : {e}") from e
            atexit.register(self.stop)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self.proc.poll() is not None:
                    raise WhisperCppUnavailable(f"whisper-server s'est arrêté au démarrage (code {self.proc.returncode})"
                                          + (f", voir {self.log_dir / 'whisper-server.log'}" if self.log_dir else ""))
                if self._ready():
                    return
                time.sleep(0.5)
            self.stop()
            raise WhisperCppUnavailable("whisper-server ne répond pas (chargement du modèle trop long ?)")

    def _ready(self) -> bool:
        try:
            r = httpx.get(self.url + "/health", timeout=2)
        except httpx.HTTPError:
            return False
        return r.status_code == 200

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def transcribe_wav(self, wav: Path, language: str = "") -> list[dict]:
        self.start()
        with open(wav, "rb") as f:
            r = httpx.post(self.url + "/inference", timeout=httpx.Timeout(600, connect=10),
                           files={"file": (wav.name, f, "audio/wav")},
                           data={"response_format": "verbose_json", "temperature": "0.0",
                                 "language": language or self.language or "auto"})
        if r.status_code != 200:
            raise WhisperCppError(f"whisper-server : {r.status_code} {r.text[:200]}")
        return parse_verbose_json(r.json())


def parse_verbose_json(data: dict) -> list[dict]:
    out: list[dict] = []
    for s in data.get("segments") or []:
        text = str(s.get("text") or "").strip()
        if not text or (out and out[-1]["text"] == text):  # hallucination répétée
            continue
        out.append({"start": round(float(s.get("start") or 0), 2), "end": round(float(s.get("end") or 0), 2),
                    "text": text})
    if not out and str(data.get("text") or "").strip():
        out.append({"start": 0.0, "end": 0.0, "text": str(data["text"]).strip()})
    return out


_server: Server | None = None
_server_key: tuple | None = None
_lock = threading.Lock()


def transcribe(media: Path, *, server_dir: str | Path, model: str, models_dir: Path, work_dir: Path,
               language: str = "", log_dir: Path | None = None) -> list[dict]:
    """→ [{start, end, text}] en secondes, sur la carte graphique."""
    global _server, _server_key
    exe = find_server(server_dir)
    if not exe:
        raise WhisperCppUnavailable(f"{EXE} introuvable dans {server_dir}")
    with _lock:
        model_path = ensure_model(model, models_dir)
        key = (exe, model_path, language)
        if _server is None or _server_key != key:
            if _server:
                _server.stop()
            _server, _server_key = Server(exe, model_path, language, log_dir), key
        server = _server
    wav = to_wav16k(media, work_dir / (Path(media).stem + ".16k.wav"))
    try:
        return server.transcribe_wav(wav, language)
    finally:
        wav.unlink(missing_ok=True)


def shutdown() -> None:
    if _server:
        _server.stop()

