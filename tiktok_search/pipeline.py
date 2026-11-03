"""Orchestration : import des favoris puis indexation (métadonnées → transcription → OCR).

Robustesse :
- erreurs passagères : nouvelles tentatives dans le client HTTP ;
- vidéo supprimée/privée → statut ``unavailable`` (pas retentée en boucle) ;
- TikTok qui bloque → pause croissante, puis arrêt propre (les vidéos restent en attente) ;
- un échec de transcription ou d'OCR n'empêche pas d'indexer la légende et le reste ;
- téléchargements atomiques, fichiers déjà présents réutilisés, dossier temporaire nettoyé.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import enrich, ocr, screens, transcribe
from .config import Settings
from .db import Database
from .enrich import VideoMeta
from .importer import FavoriteItem, kind_from_url
from .tiktok_page import Blocked, TikTokPageClient, VideoUnavailable

log = logging.getLogger(__name__)

_whisper_lock = threading.Lock()  # un seul modèle Whisper en mémoire, utilisé à tour de rôle


def import_items(db: Database, items: list[FavoriteItem], source: str) -> int:
    return sum(db.add_favorite(i.video_id, i.url, i.date, f"{source}:{i.kind}", kind_from_url(i.url))
               for i in items)


def media_url(settings: Settings, path: Path) -> str:
    return "/media/" + path.relative_to(settings.media_dir).as_posix()


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def fetch_meta(client: TikTokPageClient, vid: str) -> VideoMeta:
    try:
        page = client.fetch(vid)
    except (VideoUnavailable, Blocked):
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("page TikTok illisible pour %s (%s), repli sur yt-dlp", vid, e)
    else:
        meta = VideoMeta.from_page(page)
        sub = enrich.pick_subtitle(page.subtitles)
        if sub:
            try:
                meta.subtitle_segments = enrich.parse_vtt_segments(client.get_text(sub["url"]))
            except Blocked:
                raise
            except Exception as e:  # noqa: BLE001
                log.info("sous-titres indisponibles pour %s : %s", vid, e)
        return meta
    try:
        return enrich.fetch_metadata_ytdlp(vid)
    except Exception as e:  # noqa: BLE001
        msg = str(e).lower()
        if any(k in msg for k in ("private", "removed", "not available", "unavailable", "status code 10")):
            raise VideoUnavailable(str(e)[:200]) from e
        log.warning("yt-dlp a échoué pour %s (%s), repli sur oEmbed", vid, e)
    return enrich.fetch_oembed(vid)


def _ok_file(p: Path) -> bool:
    return p.is_file() and p.stat().st_size > 0


def ocr_screens(shots: list[screens.Screen], lang: str) -> list[dict]:
    """OCR des captures ; un même texte affiché plusieurs secondes ne donne qu'un segment."""
    segs: list[dict] = []
    for shot in shots:
        try:
            text = ocr.ocr_image(shot.path, lang)
        except Exception as e:  # noqa: BLE001
            log.info("OCR échoué sur %s : %s", shot.path.name, e)
            continue
        if len(text) < 3:
            continue
        if segs and segs[-1]["text"] == text:
            segs[-1]["end"] = shot.t + 1
            continue
        segs.append({"kind": "ocr", "start": shot.t, "end": shot.t + 1, "text": text})
    return segs


def process_video(db: Database, settings: Settings, video: dict, client: TikTokPageClient) -> str:
    """Indexe une vidéo. Renvoie le statut final ; lève ``Blocked`` si TikTok bloque."""
    vid = video["id"]
    warnings: list[str] = []
    try:
        meta = fetch_meta(client, vid)
        degraded = meta.source != "page"
        if degraded and video.get("kind") == "photo":
            meta.kind = "photo"  # un repli ne sait pas reconnaître un carrousel : on garde ce qu'on savait
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
        cover = media / "cover.jpg"
        if meta.thumbnail and not _ok_file(cover):
            try:
                client.download(meta.thumbnail, cover)
            except Blocked:
                raise
            except Exception as e:  # noqa: BLE001
                log.info("miniature non téléchargée pour %s : %s", vid, e)
        if _ok_file(cover):
            fields["thumbnail"] = media_url(settings, cover)

        do_ocr = settings.ocr and ocr.is_available()

        if meta.kind == "photo" and degraded and not meta.image_urls:
            pass  # repli sans les images : on conserve les pages déjà téléchargées
        elif meta.kind == "photo":
            images = []
            for i, url in enumerate(meta.image_urls, 1):
                dest = media / f"page_{i:02d}.jpg"
                if not _ok_file(dest):
                    try:
                        client.download(url, dest)
                    except Blocked:
                        raise
                    except Exception as e:  # noqa: BLE001
                        warnings.append(f"page {i} non téléchargée")
                        log.info("page %s de %s non téléchargée : %s", i, vid, e)
                        continue
                images.append(media_url(settings, dest))
                if do_ocr:
                    try:
                        segments.append({"kind": "ocr", "page": i, "text": ocr.ocr_image(dest, settings.ocr_lang)})
                    except Exception as e:  # noqa: BLE001
                        warnings.append(f"OCR page {i} échoué")
                        log.info("OCR page %s de %s : %s", i, vid, e)
            fields["images"] = images
        else:
            need_whisper = (settings.transcribe == "auto" and not meta.subtitle_segments
                            and transcribe.is_available())
            if (need_whisper or do_ocr) and not ffmpeg_available():
                warnings.append("ffmpeg absent : transcription/OCR ignorés")
                need_whisper = do_ocr = False
            local_video = media / "video.mp4"
            if need_whisper or do_ocr or (settings.keep_video and not _ok_file(local_video)):
                work = settings.tmp_dir / vid
                try:
                    if _ok_file(local_video):
                        video_path = local_video
                    else:
                        video_path = enrich.download_video(vid, work)
                    if need_whisper:
                        try:
                            audio = enrich.extract_audio(video_path, work)
                            with _whisper_lock:
                                segs = transcribe.transcribe_segments(audio, settings.whisper_model)
                            segments += [{"kind": "transcript", **s} for s in segs]
                        except Exception as e:  # noqa: BLE001
                            warnings.append(f"transcription échouée : {e}"[:120])
                            log.warning("transcription échouée pour %s : %s", vid, e)
                    if do_ocr:
                        try:
                            shots = screens.smart_screens(video_path, max_screens=60, work_dir=work)
                            segments += ocr_screens(shots, settings.ocr_lang)
                        except Exception as e:  # noqa: BLE001
                            warnings.append(f"OCR échoué : {e}"[:120])
                            log.warning("OCR échoué pour %s : %s", vid, e)
                    if settings.keep_video and video_path != local_video:
                        shutil.move(str(video_path), local_video)
                except (VideoUnavailable, Blocked):
                    raise
                except Exception as e:  # noqa: BLE001
                    warnings.append(f"vidéo non téléchargée : {e}"[:120])
                    log.warning("téléchargement échoué pour %s : %s", vid, e)
                finally:
                    shutil.rmtree(work, ignore_errors=True)
            if _ok_file(local_video):
                fields["media_path"] = media_url(settings, local_video)

        fields["transcript"] = " ".join(s["text"] for s in segments if s["kind"] == "transcript")
        fields["ocr_text"] = " | ".join(
            (f"[p.{s['page']}] " if s.get("page") else "") + s["text"]
            for s in segments if s["kind"] == "ocr" and s["text"]
        )
        new_segments: list[dict] | None = segments
        if degraded:
            # Données partielles : ne jamais écraser ce qui était déjà indexé par du vide.
            fields = {k: v for k, v in fields.items() if v not in (None, "", []) or not video.get(k)}
            if not segments and db.get_segments(vid):
                new_segments = None
            warnings.append(f"infos partielles ({meta.source})")
        db.save_video(vid, segments=new_segments, **fields, status="done", error=" · ".join(warnings)[:500])
        return "done"
    except Blocked:
        raise
    except VideoUnavailable as e:
        db.mark_failure(vid, str(e), permanent=True)
        return "unavailable"
    except Exception as e:  # noqa: BLE001
        log.exception("Échec du traitement de %s", vid)
        db.mark_failure(vid, str(e))
        return "error"


@dataclass
class Job:
    running: bool = False
    done: int = 0
    total: int = 0
    current: str = ""
    message: str = ""
    cancel: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def as_dict(self) -> dict:
        return {"running": self.running, "done": self.done, "total": self.total,
                "current": self.current, "message": self.message}


def clean_tmp(settings: Settings) -> None:
    """Supprime les restes d'un traitement interrompu (crash, Ctrl+C…)."""
    if settings.tmp_dir.exists():
        for p in settings.tmp_dir.iterdir():
            shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
    for p in settings.media_dir.glob("*/*.part"):
        p.unlink(missing_ok=True)


def process_all(
    db: Database,
    settings: Settings,
    retry_errors: bool = False,
    limit: int | None = None,
    job: Job | None = None,
    on_progress: Callable[[int, int, dict, str], None] | None = None,
    client_factory: Callable[[], TikTokPageClient] = TikTokPageClient,
    max_block_pauses: int = 4,
) -> int:
    job = job or Job()
    job.running, job.done, job.total, job.cancel, job.message = True, 0, 0, False, ""
    state_lock = threading.Lock()
    blocked = threading.Event()
    pauses = 0

    def work(video: dict) -> None:
        nonlocal pauses
        while not job.cancel:
            if blocked.is_set():
                return
            try:
                status = process_video(db, settings, video, client)
            except Blocked as e:
                with state_lock:
                    pauses += 1
                    if pauses > max_block_pauses:
                        blocked.set()
                        job.message = f"TikTok bloque les requêtes ({e}). Réessaie plus tard : les vidéos restantes sont en attente."
                        return
                    wait = min(600, 30 * 2 ** (pauses - 1))
                    job.message = f"TikTok limite les requêtes : pause de {wait} s…"
                log.warning("bloqué par TikTok, pause %s s", wait)
                for _ in range(int(wait)):
                    if job.cancel:
                        return
                    time.sleep(1)
                continue
            with state_lock:
                job.done += 1
                if not blocked.is_set() and job.message.startswith("TikTok limite"):
                    job.message = ""
                done = job.done
            if on_progress:
                on_progress(done, job.total, video, status)
            if settings.request_delay:
                time.sleep(settings.request_delay)
            return

    client = None
    try:
        clean_tmp(settings)
        client = client_factory()
        seen: set[str] = set()
        # On reboucle tant que des vidéos arrivent (import pendant l'indexation).
        while not job.cancel and not blocked.is_set():
            batch = [v for v in db.videos_to_process(retry_errors=retry_errors and not seen, limit=limit)
                     if v["id"] not in seen]
            if not batch:
                break
            seen.update(v["id"] for v in batch)
            job.total += len(batch)
            with ThreadPoolExecutor(max_workers=settings.workers, thread_name_prefix="index") as pool:
                list(pool.map(work, batch))
            if limit:
                break
    finally:
        if client is not None:
            client.close()
        if job.cancel and not job.message:
            job.message = "Indexation arrêtée."
        job.running, job.current = False, ""
    return job.done
