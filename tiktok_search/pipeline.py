"""Orchestration : import des favoris puis indexation (métadonnées → transcription → OCR)."""

from __future__ import annotations

import logging
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import enrich, ocr, screens, transcribe
from .config import Settings
from .db import Database
from .enrich import VideoMeta
from .importer import FavoriteItem, kind_from_url
from .tiktok_page import TikTokPageClient

log = logging.getLogger(__name__)


def import_items(db: Database, items: list[FavoriteItem], source: str) -> int:
    return sum(db.add_favorite(i.video_id, i.url, i.date, f"{source}:{i.kind}", kind_from_url(i.url))
               for i in items)


def media_url(settings: Settings, path: Path) -> str:
    return "/media/" + path.relative_to(settings.media_dir).as_posix()


def fetch_meta(client: TikTokPageClient, vid: str) -> VideoMeta:
    try:
        page = client.fetch(vid)
        meta = VideoMeta.from_page(page)
        sub = enrich.pick_subtitle(page.subtitles)
        if sub:
            try:
                meta.subtitle_segments = enrich.parse_vtt_segments(client.get_text(sub["url"]))
            except Exception as e:  # noqa: BLE001
                log.info("sous-titres indisponibles pour %s : %s", vid, e)
        return meta
    except Exception as e:  # noqa: BLE001
        log.warning("page TikTok illisible pour %s (%s), repli sur yt-dlp", vid, e)
    try:
        return enrich.fetch_metadata_ytdlp(vid)
    except Exception as e:  # noqa: BLE001
        log.warning("yt-dlp a échoué pour %s (%s), repli sur oEmbed", vid, e)
    return enrich.fetch_oembed(vid)


def ocr_screens(shots: list[screens.Screen], lang: str) -> list[dict]:
    """OCR des captures ; un même texte affiché plusieurs secondes ne donne qu'un segment."""
    segs: list[dict] = []
    for shot in shots:
        text = ocr.ocr_image(shot.path, lang)
        if len(text) < 3:
            continue
        if segs and segs[-1]["text"] == text:
            segs[-1]["end"] = shot.t + 1
            continue
        segs.append({"kind": "ocr", "start": shot.t, "end": shot.t + 1, "text": text})
    return segs


def process_video(db: Database, settings: Settings, video: dict, client: TikTokPageClient) -> None:
    vid = video["id"]
    try:
        meta = fetch_meta(client, vid)
        media = settings.media_dir / vid
        media.mkdir(parents=True, exist_ok=True)
        segments: list[dict] = [{"kind": "transcript", **s} for s in meta.subtitle_segments]
        fields: dict = {
            "kind": meta.kind,
            "author": meta.author,
            "description": meta.description,
            "hashtags": " ".join(f"#{t}" for t in meta.hashtags),
            "thumbnail": meta.thumbnail,
            "duration": meta.duration,
        }

        # Miniature en local : les URL du CDN TikTok expirent au bout de quelques heures.
        if meta.thumbnail:
            try:
                client.download(meta.thumbnail, media / "cover.jpg")
                fields["thumbnail"] = media_url(settings, media / "cover.jpg")
            except Exception as e:  # noqa: BLE001
                log.info("miniature non téléchargée pour %s : %s", vid, e)

        do_ocr = settings.ocr and ocr.is_available()

        if meta.kind == "photo":
            images = []
            for i, url in enumerate(meta.image_urls, 1):
                dest = media / f"page_{i:02d}.jpg"
                client.download(url, dest)
                images.append(media_url(settings, dest))
                if do_ocr:
                    segments.append({"kind": "ocr", "page": i, "text": ocr.ocr_image(dest, settings.ocr_lang)})
            fields["images"] = images
        else:
            need_whisper = (settings.transcribe == "auto" and not meta.subtitle_segments
                            and transcribe.is_available())
            if need_whisper or do_ocr or settings.keep_video:
                work = settings.tmp_dir / vid
                try:
                    video_path = enrich.download_video(vid, work)
                    if need_whisper:
                        segments += [{"kind": "transcript", **s} for s in transcribe.transcribe_segments(
                            enrich.extract_audio(video_path), settings.whisper_model)]
                    if do_ocr:
                        segments += ocr_screens(screens.smart_screens(video_path, max_screens=60), settings.ocr_lang)
                    if settings.keep_video:
                        dest = media / "video.mp4"
                        shutil.move(str(video_path), dest)
                        fields["media_path"] = media_url(settings, dest)
                finally:
                    shutil.rmtree(work, ignore_errors=True)

        fields["transcript"] = " ".join(s["text"] for s in segments if s["kind"] == "transcript")
        fields["ocr_text"] = " | ".join(
            (f"[p.{s['page']}] " if s.get("page") else "") + s["text"]
            for s in segments if s["kind"] == "ocr" and s["text"]
        )
        db.save_video(vid, segments=segments, **fields, status="done", error="")
    except Exception as e:  # noqa: BLE001
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
    client = TikTokPageClient()
    try:
        for i, video in enumerate(videos, 1):
            job.current = video["id"]
            process_video(db, settings, video, client)
            job.done = i
            if on_progress:
                on_progress(i, len(videos), video)
            if i < len(videos) and settings.request_delay:
                time.sleep(settings.request_delay)
    finally:
        client.close()
        job.running, job.current = False, ""
    return len(videos)
