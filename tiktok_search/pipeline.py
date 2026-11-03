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


def _media_needs(settings: Settings, has_transcript: bool, has_ocr: bool) -> tuple[bool, bool]:
    """(transcrire avec Whisper ?, OCR ?) pour une vidéo. Whisper lit le MP4 lui-même : seul l'OCR
    (captures d'écran) a besoin de ffmpeg."""
    need_whisper = settings.transcribe == "auto" and not has_transcript and transcribe.is_available()
    do_ocr = settings.ocr and not has_ocr and ocr.is_available() and ffmpeg_available()
    return need_whisper, do_ocr


def _download_video(client: TikTokPageClient, vid: str, play_url: str, work: Path) -> Path:
    """Lien direct de la page d'abord (une seule requête, avec les cookies déjà obtenus) ;
    yt-dlp seulement si ce lien manque ou a expiré (il relit toute la page, c'est plus lent)."""
    if play_url:
        work.mkdir(parents=True, exist_ok=True)
        dest = work / f"{vid}.mp4"
        try:
            client.download(play_url, dest, max_bytes=300 * 1024 * 1024, expect="video/")
            if _ok_file(dest):
                return dest
        except Blocked:
            raise
        except Exception as e:  # noqa: BLE001 - lien expiré, refusé… : repli yt-dlp
            log.info("lien direct inutilisable pour %s (%s), repli yt-dlp", vid, e)
    return enrich.download_video(vid, work)


def _media_work(settings: Settings, client: TikTokPageClient, vid: str, play_url: str,
                need_whisper: bool, do_ocr: bool) -> tuple[list[dict], list[str], bool]:
    """Télécharge la vidéo puis transcrit / fait l'OCR. → (segments, avertissements, vidéo gardée ?)."""
    segments: list[dict] = []
    warnings: list[str] = []
    local_video = settings.media_dir / vid / "video.mp4"
    if not (need_whisper or do_ocr or (settings.keep_video and not _ok_file(local_video))):
        return segments, warnings, _ok_file(local_video)
    work = settings.tmp_dir / vid
    try:
        video_path = local_video if _ok_file(local_video) else _download_video(client, vid, play_url, work)
        if need_whisper:
            try:
                with _whisper_lock:
                    segs = transcribe.transcribe_segments(video_path, settings.whisper_model,
                                                          settings.whisper_lang, settings.whisper_threads)
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
            local_video.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(video_path), local_video)
    except (VideoUnavailable, Blocked):
        raise
    except Exception as e:  # noqa: BLE001
        warnings.append(f"vidéo non téléchargée : {e}"[:120])
        log.warning("téléchargement échoué pour %s : %s", vid, e)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return segments, warnings, _ok_file(local_video)


def _text_fields(segments: list[dict]) -> dict:
    return {
        "transcript": " ".join(s["text"] for s in segments if s["kind"] == "transcript"),
        "ocr_text": " | ".join((f"[p.{s['page']}] " if s.get("page") else "") + s["text"]
                               for s in segments if s["kind"] == "ocr" and s["text"]),
    }


def process_video(db: Database, settings: Settings, video: dict, client: TikTokPageClient,
                  defer_media: bool = False, play_urls: dict[str, str] | None = None) -> str:
    """Indexe une vidéo. Renvoie le statut final ; lève ``Blocked`` si TikTok bloque.

    Avec ``defer_media``, s'arrête après la page (légende, hashtags, sous-titres, images) : la
    vidéo est tout de suite cherchable, et la transcription Whisper / l'OCR sont marqués à faire
    (``media_pending``) pour la 2e passe, ``process_media``."""
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
            "media_pending": 0,
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
            need_whisper, do_ocr_video = _media_needs(settings, bool(meta.subtitle_segments), False)
            if do_ocr and not ffmpeg_available():
                warnings.append("ffmpeg absent : OCR ignoré")
            if defer_media and (need_whisper or do_ocr_video or settings.keep_video):
                fields["media_pending"] = 1
                if play_urls is not None and meta.play_url:
                    play_urls[vid] = meta.play_url
            else:
                segs, warns, _kept = _media_work(settings, client, vid, meta.play_url, need_whisper, do_ocr_video)
                segments += segs
                warnings += warns
            if _ok_file(media / "video.mp4"):
                fields["media_path"] = media_url(settings, media / "video.mp4")

        fields.update(_text_fields(segments))
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


def process_media(db: Database, settings: Settings, video: dict, client: TikTokPageClient,
                  play_url: str = "") -> str:
    """2e passe : transcription Whisper / OCR d'une vidéo déjà indexée. Ne touche pas à la légende ;
    ajoute les nouveaux segments à ceux déjà présents (sous-titres, etc.)."""
    vid = video["id"]
    try:
        old = [{k: s[k] for k in ("kind", "start", "end", "page", "text")} for s in db.get_segments(vid)]
        need_whisper, do_ocr = _media_needs(settings, any(s["kind"] == "transcript" for s in old),
                                            any(s["kind"] == "ocr" for s in old))
        segs, warnings, kept = _media_work(settings, client, vid, play_url, need_whisper, do_ocr)
    except Blocked:
        raise
    except Exception as e:  # noqa: BLE001 - la vidéo reste cherchable par sa légende
        log.exception("transcription/OCR impossible pour %s", vid)
        db.save_video(vid, media_pending=0, error=f"{video.get('error') or ''} · {e}".strip(" ·")[:500])
        return "error"
    segments = old + segs
    fields = {"media_pending": 0, **_text_fields(segments)}
    if kept:
        fields["media_path"] = media_url(settings, settings.media_dir / vid / "video.mp4")
    if warnings:
        fields["error"] = " · ".join([e for e in [video.get("error") or ""] if e] + warnings)[:500]
    db.save_video(vid, segments=segments, **fields)
    return "done"


@dataclass
class Job:
    running: bool = False
    done: int = 0
    total: int = 0
    current: str = ""
    phase: str = ""      # "" | "index" (légendes, sous-titres) | "media" (transcription, OCR)
    message: str = ""
    cancel: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def as_dict(self) -> dict:
        return {"running": self.running, "done": self.done, "total": self.total,
                "current": self.current, "phase": self.phase, "message": self.message}


class Throttle:
    """Espace les requêtes vers les pages TikTok, tous fils confondus (anti-blocage). Remplace
    l'ancienne pause fixe après chaque vidéo, qui faisait aussi attendre après une transcription."""

    def __init__(self, interval: float):
        self.interval, self._next, self._lock = max(0.0, interval), 0.0, threading.Lock()

    def wait(self) -> None:
        if not self.interval:
            return
        with self._lock:
            now = time.monotonic()
            at = max(now, self._next)
            self._next = at + self.interval
        if at > now:
            time.sleep(at - now)


def clean_tmp(settings: Settings) -> None:
    """Supprime les restes d'un traitement interrompu (crash, Ctrl+C…)."""
    if settings.tmp_dir.exists():
        for p in settings.tmp_dir.iterdir():
            shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
    for p in settings.media_dir.glob("*/*.part"):
        p.unlink(missing_ok=True)


MEDIA_CHUNK = 8  # vidéos transcrites entre deux vérifications des nouveaux favoris


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
    """Indexe en deux passes :
    1. toutes les vidéos en attente, page seulement (rapide, réseau, en parallèle) → cherchables tout de suite ;
    2. transcription / OCR de celles qui en ont besoin, par petits paquets ; entre deux paquets,
       les favoris arrivés entre-temps repassent devant.
    Renvoie le nombre de vidéos indexées (1re passe)."""
    job = job or Job()
    job.running, job.done, job.total, job.cancel, job.message, job.phase = True, 0, 0, False, "", ""
    state_lock = threading.Lock()
    blocked = threading.Event()
    throttle = Throttle(settings.request_delay / max(1, settings.workers))
    play_urls: dict[str, str] = {}
    pauses = 0
    indexed = 0

    def index_one(video: dict) -> str:
        throttle.wait()
        return process_video(db, settings, video, client, defer_media=True, play_urls=play_urls)

    def media_one(video: dict) -> str:
        if not play_urls.get(video["id"]):
            throttle.wait()  # repli yt-dlp probable : il relit la page TikTok
        return process_media(db, settings, video, client, play_urls.pop(video["id"], ""))

    def work(video: dict, fn: Callable[[dict], str]) -> None:
        nonlocal pauses, indexed
        while not job.cancel:
            if blocked.is_set():
                return
            try:
                status = fn(video)
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
                if fn is index_one:
                    indexed += 1
                if not blocked.is_set() and job.message.startswith("TikTok limite"):
                    job.message = ""
                done = job.done
            if on_progress:
                on_progress(done, job.total, video, status)
            return

    def run_batch(batch: list[dict], fn: Callable[[dict], str]) -> None:
        with ThreadPoolExecutor(max_workers=settings.workers, thread_name_prefix="index") as pool:
            list(pool.map(lambda v: work(v, fn), batch))

    def set_phase(phase: str, total: int) -> None:
        if job.phase != phase:
            job.phase, job.done, job.total = phase, 0, 0
        job.total += total

    client = None
    try:
        clean_tmp(settings)
        client = client_factory()
        seen: set[str] = set()
        seen_media: set[str] = set()
        while not job.cancel and not blocked.is_set():
            # 1re passe : priorité aux vidéos pas encore indexées (y compris importées pendant ce temps).
            if not (limit and seen):
                batch = [v for v in db.videos_to_process(retry_errors=retry_errors and not seen, limit=limit)
                         if v["id"] not in seen]
                if batch:
                    seen.update(v["id"] for v in batch)
                    set_phase("index", len(batch))
                    run_batch(batch, index_one)
                    continue
            # 2e passe : transcription / OCR, par petits paquets.
            pending = [v for v in db.videos_media_pending() if v["id"] not in seen_media
                       and (not limit or v["id"] in seen)]
            if not pending:
                break
            if job.phase != "media":
                set_phase("media", len(pending))
            chunk = pending[:MEDIA_CHUNK]
            seen_media.update(v["id"] for v in chunk)
            run_batch(chunk, media_one)
    finally:
        if client is not None:
            client.close()
        if job.cancel and not job.message:
            job.message = "Indexation arrêtée."
        job.running, job.current, job.phase = False, "", ""
    return indexed
