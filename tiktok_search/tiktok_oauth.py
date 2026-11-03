"""Connexion du compte TikTok : Login Kit (OAuth 2.0) + Data Portability API.

L'API officielle « Display » de TikTok ne donne PAS accès aux favoris. La seule voie
officielle est la *Data Portability API*, qui produit le même export que
« Télécharger tes données » (catégorie *activity* : favoris, likes, historique…).
Elle demande une application approuvée par TikTok et n'est ouverte qu'aux
utilisateurs de l'EEE / du Royaume-Uni (cadre du DMA).

⚠️ Les URL et noms de champs ci-dessous suivent la documentation TikTok telle que
connue lors de l'écriture de ce module ; ils n'ont pas pu être testés sans clé
développeur approuvée. Vérifie-les sur https://developers.tiktok.com/doc/ avant la
mise en production — ils sont regroupés ici pour être faciles à ajuster.
"""

from __future__ import annotations

import json
import secrets
import time
from urllib.parse import urlencode

import httpx

from .config import Settings
from .db import Database

AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
API = "https://open.tiktokapis.com/v2"
SCOPES = ["user.info.basic", "portability.activity.single"]

TOKEN_KEY = "tiktok_token"
REQUEST_KEY = "tiktok_portability_request"


class TikTokNotConfigured(RuntimeError):
    pass


def _require(settings: Settings) -> None:
    if not (settings.tiktok_client_key and settings.tiktok_client_secret):
        raise TikTokNotConfigured(
            "Renseigne TIKTOK_CLIENT_KEY et TIKTOK_CLIENT_SECRET (voir .env.example)."
        )


def authorize_url(settings: Settings) -> tuple[str, str]:
    _require(settings)
    state = secrets.token_urlsafe(24)
    params = {
        "client_key": settings.tiktok_client_key,
        "response_type": "code",
        "scope": ",".join(SCOPES),
        "redirect_uri": settings.tiktok_redirect_uri,
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}", state


def _store_token(db: Database, data: dict) -> dict:
    if "access_token" not in data:
        raise RuntimeError(f"Réponse OAuth inattendue : {data}")
    data["obtained_at"] = time.time()
    db.kv_set(TOKEN_KEY, json.dumps(data))
    return data


def exchange_code(db: Database, settings: Settings, code: str) -> dict:
    _require(settings)
    r = httpx.post(
        TOKEN_URL,
        data={
            "client_key": settings.tiktok_client_key,
            "client_secret": settings.tiktok_client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": settings.tiktok_redirect_uri,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    r.raise_for_status()
    return _store_token(db, r.json())


def _access_token(db: Database, settings: Settings) -> str:
    raw = db.kv_get(TOKEN_KEY)
    if not raw:
        raise RuntimeError("Compte TikTok non connecté.")
    tok = json.loads(raw)
    if time.time() > tok["obtained_at"] + tok.get("expires_in", 0) - 60:
        r = httpx.post(
            TOKEN_URL,
            data={
                "client_key": settings.tiktok_client_key,
                "client_secret": settings.tiktok_client_secret,
                "grant_type": "refresh_token",
                "refresh_token": tok["refresh_token"],
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        r.raise_for_status()
        tok = _store_token(db, r.json())
    return tok["access_token"]


def is_connected(db: Database) -> bool:
    return db.kv_get(TOKEN_KEY) is not None


def _post(db: Database, settings: Settings, path: str, body: dict, fields: str | None = None) -> httpx.Response:
    params = {"fields": fields} if fields else None
    r = httpx.post(
        f"{API}{path}",
        params=params,
        json=body,
        headers={"Authorization": f"Bearer {_access_token(db, settings)}"},
        timeout=120,
    )
    r.raise_for_status()
    return r


def request_export(db: Database, settings: Settings) -> str:
    """Demande à TikTok de préparer l'export (asynchrone, peut prendre plusieurs heures)."""
    r = _post(
        db, settings, "/user/data/add/",
        {"data_format": "json", "category_selection_list": ["activity"]},
        fields="request_id",
    )
    request_id = str(r.json()["data"]["request_id"])
    db.kv_set(REQUEST_KEY, request_id)
    return request_id


def export_status(db: Database, settings: Settings) -> str:
    request_id = db.kv_get(REQUEST_KEY)
    if not request_id:
        return "none"
    r = _post(db, settings, "/user/data/check/", {"request_id": int(request_id)}, fields="status")
    return r.json()["data"]["status"]


def download_export(db: Database, settings: Settings) -> bytes:
    """Renvoie l'archive .zip de l'export (à passer à importer.load_export_bytes)."""
    request_id = db.kv_get(REQUEST_KEY)
    if not request_id:
        raise RuntimeError("Aucune demande d'export en cours.")
    return _post(db, settings, "/user/data/download/", {"request_id": int(request_id)}).content
