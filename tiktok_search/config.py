"""Configuration lue depuis les variables d'environnement (ou un fichier .env)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path = Path(".env")) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in {"1", "on", "true", "yes", "oui"}


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("TTS_DATA_DIR", "./data")))
    transcribe: str = field(default_factory=lambda: os.getenv("TTS_TRANSCRIBE", "auto"))
    whisper_model: str = field(default_factory=lambda: os.getenv("TTS_WHISPER_MODEL", "small"))
    ocr: bool = field(default_factory=lambda: _bool("TTS_OCR", False))
    ocr_lang: str = field(default_factory=lambda: os.getenv("TTS_OCR_LANG", "fra+eng"))
    keep_video: bool = field(default_factory=lambda: _bool("TTS_KEEP_VIDEO", False))
    request_delay: float = field(default_factory=lambda: float(os.getenv("TTS_REQUEST_DELAY", "1.0")))
    # Analyse IA : "gemini" (palier gratuit Google AI Studio), "ollama" (100 % local), "none" (extraction locale seule)
    llm_provider: str = field(default_factory=lambda: os.getenv("TTS_LLM", "gemini" if os.getenv("GEMINI_API_KEY") else "none"))
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest"))
    ollama_url: str = field(default_factory=lambda: os.getenv("OLLAMA_URL", "http://localhost:11434"))
    ollama_model: str = field(default_factory=lambda: os.getenv("OLLAMA_MODEL", "qwen2.5vl:7b"))
    analysis_max_screens: int = field(default_factory=lambda: int(os.getenv("TTS_MAX_SCREENS", "8")))
    tiktok_client_key: str = field(default_factory=lambda: os.getenv("TIKTOK_CLIENT_KEY", ""))
    tiktok_client_secret: str = field(default_factory=lambda: os.getenv("TIKTOK_CLIENT_SECRET", ""))
    tiktok_redirect_uri: str = field(
        default_factory=lambda: os.getenv("TIKTOK_REDIRECT_URI", "http://localhost:8000/auth/tiktok/callback")
    )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "tiktok_search.db"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"


def get_settings() -> Settings:
    _load_dotenv()
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.tmp_dir.mkdir(parents=True, exist_ok=True)
    s.media_dir.mkdir(parents=True, exist_ok=True)
    return s
