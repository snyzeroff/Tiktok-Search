"""API web (FastAPI) + interface de recherche."""

from __future__ import annotations

import hmac
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import analyze, browser_sync, categorize, importer, ocr, pipeline, tiktok_oauth, transcribe, whispercpp
from .config import get_settings
from .db import FTS_COLUMNS, Database
from .search import FIELD_LABELS, search, segment_hits

settings = get_settings()
db = Database(settings.db_path)
job = pipeline.Job()
STATIC = Path(__file__).parent / "static"

log = logging.getLogger(__name__)
MAX_UPLOAD = 1024 * 1024 * 1024  # 1 Go
ALLOWED_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"} | {
    h.strip().lower() for h in os.getenv("TTS_ALLOWED_HOSTS", "").split(",") if h.strip()}



class AccountJob:
    """Synchro des favoris par le navigateur, en tâche de fond."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.message = ""
        self.error = False

    def as_dict(self) -> dict:
        return {"running": self.running, "message": self.message, "error": self.error,
                **browser_sync.account_state(db, browser_sync.default_profile_dir())}


account = AccountJob()


def _start_account_sync(login: bool = False, full: bool = False, collections: bool = False) -> bool:
    with account.lock:
        if account.running:
            return False
        account.running, account.error = True, False
        account.message = "Ouverture du navigateur…"

    def progress(msg: str) -> None:
        account.message = msg

    def run() -> None:
        try:
            if collections:
                r = browser_sync.sync_collections_into_db(db, settings, progress=progress)
                n = len(r["collections"])
                account.message = (f"{n} collection{'s' if n > 1 else ''} TikTok importée{'s' if n > 1 else ''}."
                                   if n else "Aucune collection trouvée dans tes favoris TikTok.")
                _start_processing()  # vidéos de collections pas encore dans les favoris
                return
            r = browser_sync.sync_into_db(db, settings, login=login, full=full, progress=progress,
                                          on_added=lambda n: _start_processing())
            account.message = (f"{r['added']} nouveaux favoris importés." if r["added"]
                               else "Tes favoris sont à jour.")
            if r["added"]:
                _start_processing()
        except browser_sync.SyncError as e:
            account.message, account.error = str(e), True
        except Exception as e:  # noqa: BLE001
            log.exception("synchro des favoris interrompue")
            account.message, account.error = f"Synchro interrompue : {e}", True
        finally:
            account.running = False

    threading.Thread(target=run, daemon=True, name="synchro-favoris").start()
    return True


def _sync_due() -> bool:
    state = browser_sync.account_state(db, browser_sync.default_profile_dir())
    if not (state["available"] and state["has_session"]):
        return False
    return not state["last_sync"] or time.time() - state["last_sync"] >= settings.sync_interval_min * 60


@asynccontextmanager
async def lifespan(_app):
    # À chaque ouverture de l'app : nouveaux favoris récupérés automatiquement.
    if settings.auto_sync and _sync_due():
        _start_account_sync()
    yield
    whispercpp.shutdown()


app = FastAPI(title="TikTok Search", lifespan=lifespan)
app.mount("/media", StaticFiles(directory=settings.media_dir), name="media")
app.mount("/static", StaticFiles(directory=STATIC), name="static")
_oauth_states: list[str] = []


@app.middleware("http")
async def security(request: Request, call_next):
    """- Host autorisé uniquement (bloque le « DNS rebinding »).
    - Requêtes POST de l'API : en-tête X-TTS obligatoire, qu'un site tiers ne peut pas
      envoyer sans autorisation CORS (bloque le CSRF : personne ne peut lancer une analyse
      payante ou une réindexation depuis une autre page ouverte dans ton navigateur).
    - Jeton d'accès optionnel (TTS_AUTH_TOKEN) si le serveur est exposé sur le réseau."""
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].lower()
    if host and host not in ALLOWED_HOSTS and "*" not in ALLOWED_HOSTS:
        return PlainTextResponse("Hôte non autorisé (ajoute-le à TTS_ALLOWED_HOSTS)", status_code=400)
    if settings.auth_token:
        token = (request.query_params.get("token") or request.cookies.get("tts_token")
                 or request.headers.get("authorization", "").removeprefix("Bearer ").strip())
        if not hmac.compare_digest(token.encode(), settings.auth_token.encode()):
            return PlainTextResponse("Accès refusé : ajoute ?token=… à l'adresse", status_code=401)
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/") \
            and request.headers.get("x-tts") != "1":
        return JSONResponse({"detail": "en-tête X-TTS manquant"}, status_code=403)
    response = await call_next(request)
    if settings.auth_token and request.query_params.get("token"):
        response.set_cookie("tts_token", settings.auth_token, httponly=True, samesite="strict", max_age=30 * 86400)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    if request.url.path == "/":
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https:; media-src 'self'; font-src 'self'; frame-src https://www.tiktok.com; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    return response


def _start_processing(retry_errors: bool = False) -> bool:
    with job.lock:
        if job.running:
            return False
        job.running = True

    def run() -> None:
        try:
            pipeline.process_all(db, settings, retry_errors=retry_errors, job=job)
        except Exception as e:  # noqa: BLE001
            log.exception("indexation interrompue")
            job.message = f"Indexation interrompue : {e}"
        finally:
            job.running = False

    threading.Thread(target=run, daemon=True, name="indexation").start()
    return True


@app.post("/api/process/cancel")
def cancel_processing() -> dict:
    job.cancel = True
    return {"cancelling": job.running}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
def status() -> dict:
    return {
        "stats": db.stats(),
        "job": job.as_dict(),
        "features": {
            "whisper": settings.transcribe == "auto" and transcribe.available(settings),
            "whisper_engine": transcribe.engine_label(settings),
            "ocr": settings.ocr and ocr.is_available(),
            "tiktok_oauth": bool(settings.tiktok_client_key),
            "tiktok_connected": tiktok_oauth.is_connected(db),
            "browser_sync": browser_sync.is_available(),
            "llm": settings.llm_provider if settings.llm_provider in analyze.PROVIDERS else "none",
            "llm_model": analyze.model_name(settings),
            "keep_video": settings.keep_video,
            "ffmpeg": pipeline.ffmpeg_available(),
        },
        "account": account.as_dict(),
        "fields": FIELD_LABELS,
    }


@app.post("/api/import")
async def import_export(file: UploadFile = File(...), likes: bool = Form(False), process: bool = Form(True)) -> dict:
    raw = await file.read(MAX_UPLOAD + 1)
    if len(raw) > MAX_UPLOAD:
        raise HTTPException(413, "Fichier trop volumineux (1 Go max).")
    try:
        data = importer.load_export_bytes(raw)
    except Exception as e:
        raise HTTPException(400, f"Fichier illisible : {e}") from e
    items = importer.parse_export(data, include_likes=likes)
    added = pipeline.import_items(db, items, "export")
    if process and added:
        _start_processing()
    return {"found": len(items), "added": added}


@app.post("/api/links")
def import_links(text: str = Form(..., max_length=2_000_000), process: bool = Form(True)) -> dict:
    items = importer.parse_links_text(importer.resolve_short_links(text))
    added = pipeline.import_items(db, items, "links")
    if process and added:
        _start_processing()
    return {"found": len(items), "added": added}


@app.post("/api/process")
def process(retry_errors: bool = False) -> dict:
    return {"started": _start_processing(retry_errors)}


@app.post("/api/reindex")
def reindex() -> dict:
    n = db.reset_all()
    return {"reset": n, "started": _start_processing()}


def _public(v: dict) -> dict:
    return {
        "id": v["id"],
        "kind": v.get("kind") or "video",
        "url": importer.canonical_url(v["id"], v.get("author") or "", v.get("kind") or "video"),
        "author": v.get("author") or "",
        "description": v.get("description") or "",
        "hashtags": v.get("hashtags") or "",
        "thumbnail": v.get("thumbnail") or "",
        "images": v.get("images") or [],
        "media_path": v.get("media_path") or "",
        "duration": v.get("duration"),
        "favorited_at": v.get("favorited_at") or "",
        "entities": v.get("entities") or "",
    }


@app.get("/api/search")
def api_search(
    q: str = Query(..., max_length=500),
    field: list[str] = Query(default=[]),
    any: bool = False,
    kind: str = "",
    sort: str = "relevance",
    category: int = 0,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    fields = [f for f in field if f in FTS_COLUMNS] or None
    results = search(db, q, fields=fields, any_term=any, limit=limit, offset=offset, kind=kind or None, sort=sort,
                     category=category or None)
    cats = db.categories_for([r.video["id"] for r in results])
    return {
        "query": q,
        "results": [{**_public(r.video), "score": round(r.score, 3), "matches": r.matches, "hits": r.hits,
                     "categories": cats.get(r.video["id"], [])} for r in results],
    }


@app.get("/api/videos")
def api_videos(kind: str = "", category: int = 0, limit: int = Query(60, ge=1, le=200),
               offset: int = Query(0, ge=0)) -> dict:
    videos = db.list_videos(limit=limit, offset=offset, kind=kind or None, category=category or None)
    cats = db.categories_for([v["id"] for v in videos])
    return {"results": [{**_public(v), "matches": {}, "hits": [], "categories": cats.get(v["id"], [])}
                        for v in videos]}


# --- Catégories -------------------------------------------------------------------

class CategorySet(BaseModel):
    id: str = "perso"
    name: str = Field("Mes catégories", max_length=200)
    description: str = Field("", max_length=1000)
    categories: list[dict] = Field(..., max_length=60)


@app.get("/api/categories")
def api_categories() -> dict:
    return {"categories": [{k: c[k] for k in ("id", "name", "source", "set_name", "keywords", "count")}
                           for c in db.list_categories()],
            "auto_set": categorize.saved_set(db)}


@app.post("/api/categories/tiktok")
def api_categories_tiktok() -> dict:
    """Importe tes collections TikTok comme catégories (en tâche de fond, comme la synchro)."""
    if not browser_sync.account_state(db, browser_sync.default_profile_dir())["has_session"]:
        raise HTTPException(400, "Connecte d'abord ton compte TikTok (Mes favoris → Se connecter à TikTok).")
    return {"started": _start_account_sync(collections=True)}


@app.post("/api/categories/propose")
def api_categories_propose() -> dict:
    return categorize.propose(db, settings)


@app.post("/api/categories/preview")
def api_categories_preview(cset: CategorySet) -> dict:
    return categorize.preview(db, cset.model_dump())


@app.post("/api/categories/apply")
def api_categories_apply(cset: CategorySet) -> dict:
    try:
        return categorize.apply(db, cset.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/categories/clear")
def api_categories_clear() -> dict:
    return {"deleted": categorize.clear(db)}


@app.get("/api/video/{video_id}")
def api_video(video_id: str, q: str = "") -> dict:
    video = db.get_video(video_id)
    if not video:
        raise HTTPException(404, "Vidéo inconnue")
    segs = segment_hits(db, video_id, q, all_segments=True) if q.strip() else [
        {**s, "match": False} for s in db.get_segments(video_id)]
    latest = db.latest_analysis(video_id)
    return {"video": _public(video), "segments": segs, "analysis": latest["result"] if latest else None}


class AnalyzeRequest(BaseModel):
    ids: list[str] = Field(..., max_length=50)
    mode: str = "text"   # text | screens | auto
    force: bool = False
    cached_only: bool = False  # afficher une analyse existante sans jamais rien facturer


@app.post("/api/analyze")
def api_analyze(req: AnalyzeRequest) -> dict:
    if not req.ids:
        raise HTTPException(400, "Aucune vidéo sélectionnée")
    if len(req.ids) > 50:
        raise HTTPException(400, "50 vidéos maximum par analyse")
    if req.mode not in ("text", "screens", "auto"):
        raise HTTPException(400, "mode invalide")
    try:
        return analyze.analyze_many(db, settings, req.ids, req.mode, req.force, cached_only=req.cached_only)
    except KeyError as e:
        raise HTTPException(404, f"Vidéo inconnue : {e}") from e


@app.get("/api/analyze/export")
def api_analyze_export(ids: str = Query(..., max_length=10000), format: str = "md", mode: str = "text"):
    if mode not in ("text", "screens", "auto") or format not in ("md", "csv", "json"):
        raise HTTPException(400, "mode ou format invalide")
    id_list = [i for i in ids.split(",") if i][:200]
    try:
        data = analyze.analyze_many(db, settings, id_list, mode, cached_only=True)  # export = jamais payant
    except KeyError as e:
        raise HTTPException(404, f"Vidéo inconnue : {e}") from e
    if format == "json":
        return data
    if format == "csv":
        return PlainTextResponse(analyze.to_csv(data["aggregate"]), media_type="text/csv; charset=utf-8",
                                 headers={"Content-Disposition": 'attachment; filename="analyse-tiktok.csv"'})
    return PlainTextResponse(analyze.to_markdown(data["aggregate"]), media_type="text/markdown; charset=utf-8",
                             headers={"Content-Disposition": 'attachment; filename="analyse-tiktok.md"'})


# --- Compte TikTok par le navigateur ------------------------------------------

@app.post("/api/account/login")
def account_login() -> dict:
    """Ouvre une fenêtre de navigateur sur ton ordinateur pour te connecter à TikTok."""
    if not browser_sync.is_available():
        raise HTTPException(400, "Playwright n'est pas installé : pip install playwright")
    return {"started": _start_account_sync(login=True)}


@app.post("/api/account/sync")
def account_sync(full: bool = False, auto: bool = False) -> dict:
    """``auto`` : appelé à l'ouverture de la page, ne fait rien si la dernière synchro est récente."""
    if auto and not (settings.auto_sync and _sync_due()):
        return {"started": False, "skipped": True}
    if not browser_sync.account_state(db, browser_sync.default_profile_dir())["has_session"]:
        raise HTTPException(400, "Connecte d'abord ton compte TikTok.")
    return {"started": _start_account_sync(full=full)}


@app.post("/api/account/logout")
def account_logout() -> dict:
    if account.running:
        raise HTTPException(409, "Une synchro est en cours, réessaie dans un instant.")
    browser_sync.forget_session(browser_sync.default_profile_dir())
    db.kv_delete(browser_sync.KV_USERNAME, browser_sync.KV_LAST_SYNC, browser_sync.KV_FULL_DONE,
                 browser_sync.KV_SESSION)
    account.message, account.error = "Déconnecté. Tes favoris déjà importés restent dans l'index.", False
    return {"ok": True}


# --- Connexion TikTok (API officielle) -----------------------------------------

@app.get("/auth/tiktok/login")
def tiktok_login() -> RedirectResponse:
    try:
        url, state = tiktok_oauth.authorize_url(settings)
    except tiktok_oauth.TikTokNotConfigured as e:
        raise HTTPException(400, str(e)) from e
    _oauth_states.append(state)
    del _oauth_states[:-20]
    return RedirectResponse(url)


@app.get("/auth/tiktok/callback")
def tiktok_callback(request: Request, code: str = "", state: str = "", error: str = "") -> RedirectResponse:
    if error or not code:
        raise HTTPException(400, f"Connexion TikTok refusée : {error or 'code manquant'}")
    if state not in _oauth_states:
        raise HTTPException(400, "Paramètre state invalide.")
    _oauth_states.remove(state)
    tiktok_oauth.exchange_code(db, settings, code)
    tiktok_oauth.request_export(db, settings)
    return RedirectResponse("/?connected=1")


@app.post("/api/tiktok/sync")
def tiktok_sync() -> dict:
    """Vérifie l'export demandé à TikTok ; s'il est prêt, l'importe et lance l'indexation."""
    try:
        state = tiktok_oauth.export_status(db, settings)
        if state == "none":
            return {"status": "requested", "request_id": tiktok_oauth.request_export(db, settings)}
        if state.lower() not in {"downloading", "ready", "completed", "succeeded"}:
            return {"status": state}
        data = importer.load_export_bytes(tiktok_oauth.download_export(db, settings))
    except Exception as e:
        raise HTTPException(502, f"Erreur API TikTok : {e}") from e
    items = importer.parse_export(data)
    added = pipeline.import_items(db, items, "tiktok-api")
    if added:
        _start_processing()
    return {"status": "imported", "found": len(items), "added": added}
