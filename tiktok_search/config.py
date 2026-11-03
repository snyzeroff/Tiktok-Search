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


def _num(name: str, default, cast):
    try:
        return cast(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("TTS_DATA_DIR", "./data")))
    transcribe: str = field(default_factory=lambda: os.getenv("TTS_TRANSCRIBE", "auto"))
    whisper_model: str = field(default_factory=lambda: os.getenv("TTS_WHISPER_MODEL", "small"))
    whisper_lang: str = field(default_factory=lambda: os.getenv("TTS_WHISPER_LANG", "").strip().lower())
    whisper_threads: int = field(default_factory=lambda: max(0, _num("TTS_WHISPER_THREADS", 0, int)))
    ocr: bool = field(default_factory=lambda: _bool("TTS_OCR", False))
    ocr_lang: str = field(default_factory=lambda: os.getenv("TTS_OCR_LANG", "fra+eng"))
    keep_video: bool = field(default_factory=lambda: _bool("TTS_KEEP_VIDEO", False))
    request_delay: float = field(default_factory=lambda: _num("TTS_REQUEST_DELAY", 1.0, float))
    workers: int = field(default_factory=lambda: max(1, min(8, _num("TTS_WORKERS", 2, int))))
    auth_token: str = field(default_factory=lambda: os.getenv("TTS_AUTH_TOKEN", ""))
    # Analyse IA : "gemini" (palier gratuit Google AI Studio), "ollama" (100 % local), "none" (extraction locale seule)
    llm_provider: str = field(default_factory=lambda: os.getenv("TTS_LLM", "gemini" if os.getenv("GEMINI_API_KEY") else "none"))
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest"))
    ollama_url: str = field(default_factory=lambda: os.getenv("OLLAMA_URL", "http://localhost:11434"))
    ollama_model: str = field(default_factory=lambda: os.getenv("OLLAMA_MODEL", "qwen2.5vl:7b"))
    analysis_max_screens: int = field(default_factory=lambda: max(1, min(30, _num("TTS_MAX_SCREENS", 8, int))))
    # Synchro des favoris par le navigateur (Playwright)
    browser: str = field(default_factory=lambda: os.getenv("TTS_BROWSER", "auto"))  # auto | msedge | chrome | chromium
    browser_headless: bool = field(default_factory=lambda: _bool("TTS_BROWSER_HEADLESS", True))
    auto_sync: bool = field(default_factory=lambda: _bool("TTS_AUTO_SYNC", True))
    sync_interval_min: float = field(default_factory=lambda: max(0.0, _num("TTS_SYNC_INTERVAL_MIN", 15, float)))
    tiktok_username: str = field(default_factory=lambda: os.getenv("TTS_TIKTOK_USERNAME", "").lstrip("@").strip())
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
