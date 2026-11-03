"""Orchestration : import des favoris puis enrichissement (métadonnées → transcription → OCR)."""

from __future__ import annotations

import logging
import shutil
import threading
from dataclasses import dataclass, field
from typing import Callable

from . import enrich, ocr, transcribe
from .config import Settings
from .db import Database
from .importer import FavoriteItem

log = logging.getLogger(__name__)


def import_items(db: Database, items: list[FavoriteItem], source: str) -> int:
    return sum(db.add_favorite(i.video_id, i.url, i.date, f"{source}:{i.kind}") for i in items)


def process_video(db: Database, settings: Settings, video: dict) -> None:
    vid = video["id"]
    try:
        try:
            meta = enrich.fetch_metadata(vid)
        except Exception as e:  # yt-dlp peut casser quand TikTok change son site
            log.warning("yt-dlp a échoué pour %s (%s), repli sur oEmbed", vid, e)
            meta = enrich.fetch_oembed(vid)

        fields = {
            "author": meta.author,
            "description": meta.description,
            "hashtags": " ".join(f"#{t}" for t in meta.hashtags),
            "thumbnail": meta.thumbnail,
            "duration": meta.duration,
            "transcript": meta.subtitles_text,
        }

        need_whisper = (
            settings.transcribe == "auto" and not meta.subtitles_text and transcribe.is_available()
        )
        need_ocr = settings.ocr and ocr.is_available()
        if need_whisper or need_ocr:
            work = settings.tmp_dir / vid
            try:
                video_path = enrich.download_video(vid, work)
                if need_whisper:
                    fields["transcript"] = transcribe.transcribe(
                        enrich.extract_audio(video_path), settings.whisper_model
                    )
                if need_ocr:
                    fields["ocr_text"] = ocr.ocr_frames(enrich.extract_frames(video_path), settings.ocr_lang)
            finally:
                shutil.rmtree(work, ignore_errors=True)

        db.save_video(vid, **fields, status="done", error="")
    except Exception as e:
        log.exception("Échec du traitement de %s", vid)
        db.save_video(vid, status="error", error=str(e)[:500])


@dataclass
class Job:
    running: bool = False
    done: int = 0
    total: int = 0
    current: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def as_dict(self) -> dict:
        return {"running": self.running, "done": self.done, "total": self.total, "current": self.current}


def process_all(
    db: Database,
    settings: Settings,
    retry_errors: bool = False,
    limit: int | None = None,
    job: Job | None = None,
    on_progress: Callable[[int, int, dict], None] | None = None,
) -> int:
    videos = db.videos_to_process(retry_errors=retry_errors, limit=limit)
    job = job or Job()
    job.running, job.done, job.total = True, 0, len(videos)
    try:
        for i, video in enumerate(videos, 1):
            job.current = video["id"]
            process_video(db, settings, video)
            job.done = i
            if on_progress:
                on_progress(i, len(videos), video)
    finally:
        job.running, job.current = False, ""
    return len(videos)
