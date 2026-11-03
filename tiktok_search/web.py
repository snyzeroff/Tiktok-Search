"""API web (FastAPI) + interface de recherche."""

from __future__ import annotations

import threading
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse

from . import importer, ocr, pipeline, tiktok_oauth, transcribe
from .config import get_settings
from .db import FTS_COLUMNS, Database
from .search import FIELD_LABELS, search

settings = get_settings()
db = Database(settings.db_path)
job = pipeline.Job()
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="TikTok Search")
_oauth_states: set[str] = set()


def _start_processing(retry_errors: bool = False) -> bool:
    with job.lock:
        if job.running:
            return False
        job.running = True
    threading.Thread(
        target=pipeline.process_all, kwargs={"db": db, "settings": settings, "retry_errors": retry_errors, "job": job},
        daemon=True,
    ).start()
    return True


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
def status() -> dict:
    return {
        "stats": db.stats(),
        "job": job.as_dict(),
        "features": {
            "whisper": settings.transcribe == "auto" and transcribe.is_available(),
            "ocr": settings.ocr and ocr.is_available(),
            "tiktok_oauth": bool(settings.tiktok_client_key),
            "tiktok_connected": tiktok_oauth.is_connected(db),
        },
        "fields": FIELD_LABELS,
    }


@app.post("/api/import")
async def import_export(file: UploadFile = File(...), likes: bool = Form(False), process: bool = Form(True)) -> dict:
    try:
        data = importer.load_export_bytes(await file.read())
    except Exception as e:
        raise HTTPException(400, f"Fichier illisible : {e}") from e
    items = importer.parse_export(data, include_likes=likes)
    added = pipeline.import_items(db, items, "export")
    if process and added:
        _start_processing()
    return {"found": len(items), "added": added}


@app.post("/api/links")
def import_links(text: str = Form(...), process: bool = Form(True)) -> dict:
    items = importer.parse_links_text(text)
    added = pipeline.import_items(db, items, "links")
    if process and added:
        _start_processing()
    return {"found": len(items), "added": added}


@app.post("/api/process")
def process(retry_errors: bool = False) -> dict:
    return {"started": _start_processing(retry_errors)}


@app.get("/api/search")
def api_search(
    q: str,
    field: list[str] = Query(default=[]),
    any: bool = False,
    limit: int = Query(50, le=200),
    offset: int = 0,
) -> dict:
    fields = [f for f in field if f in FTS_COLUMNS] or None
    results = search(db, q, fields=fields, any_term=any, limit=limit, offset=offset)
    return {
        "query": q,
        "results": [
            {
                "id": r.video["id"],
                "url": importer.canonical_url(r.video["id"]),
                "author": r.video["author"],
                "description": r.video["description"],
                "thumbnail": r.video["thumbnail"],
                "favorited_at": r.video["favorited_at"],
                "score": round(r.score, 3),
                "matches": r.matches,
            }
            for r in results
        ],
    }


# --- Connexion TikTok ---------------------------------------------------------

@app.get("/auth/tiktok/login")
def tiktok_login() -> RedirectResponse:
    try:
        url, state = tiktok_oauth.authorize_url(settings)
    except tiktok_oauth.TikTokNotConfigured as e:
        raise HTTPException(400, str(e)) from e
    _oauth_states.add(state)
    return RedirectResponse(url)


@app.get("/auth/tiktok/callback")
def tiktok_callback(request: Request, code: str = "", state: str = "", error: str = "") -> RedirectResponse:
    if error or not code:
        raise HTTPException(400, f"Connexion TikTok refusée : {error or 'code manquant'}")
    if state not in _oauth_states:
        raise HTTPException(400, "Paramètre state invalide.")
    _oauth_states.discard(state)
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
