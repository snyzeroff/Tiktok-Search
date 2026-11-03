"""Lecture directe d'une page TikTok (vidéo OU carrousel photo), sans clé API.

Chaque page TikTok embarque un JSON (``__UNIVERSAL_DATA_FOR_REHYDRATION__``, anciennement
``SIGI_STATE``) qui contient tout ce qu'il nous faut en une seule requête : légende,
hashtags, auteur, durée, miniature, **sous-titres horodatés** et, pour un carrousel, la
liste des **images**. C'est aussi ce qu'utilise yt-dlp en interne, mais yt-dlp ne gère
pas les carrousels photo.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024   # une image/miniature/sous-titre ne dépasse jamais ça
RETRY_STATUSES = {408, 425, 500, 502, 503, 504}


class VideoUnavailable(Exception):
    """Vidéo supprimée, privée ou restreinte : inutile de réessayer."""


class Blocked(Exception):
    """TikTok limite ou bloque nos requêtes (429/403/captcha) : il faut faire une pause."""

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8", "Referer": "https://www.tiktok.com/"}

_SCRIPT_RE = re.compile(
    r'<script[^>]+id="(__UNIVERSAL_DATA_FOR_REHYDRATION__|SIGI_STATE)"[^>]*>(.*?)</script>', re.S
)


@dataclass
class PageItem:
    video_id: str
    kind: str = "video"                       # video | photo
    author: str = ""
    description: str = ""
    hashtags: list[str] = field(default_factory=list)
    cover: str = ""
    duration: float | None = None
    image_urls: list[str] = field(default_factory=list)
    subtitles: list[dict] = field(default_factory=list)   # [{lang, url, format, source}]
    play_url: str = ""                        # lien direct du MP4 (expire en quelques heures)


def _first(urls) -> str:
    if isinstance(urls, list) and urls:
        return urls[0]
    return urls if isinstance(urls, str) else ""


# Codes renvoyés par TikTok quand la publication n'est plus accessible
UNAVAILABLE_CODES = {10101, 10202, 10203, 10204, 10215, 10216, 10217, 10219, 10222}


def extract_item_struct(html: str, video_id: str) -> dict | None:
    """Renvoie l'``itemStruct`` ; lève ``VideoUnavailable`` si TikTok dit que la vidéo n'existe plus."""
    m = _SCRIPT_RE.search(html)
    if not m:
        return None
    try:
        data = json.loads(m.group(2))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if m.group(1) == "SIGI_STATE":
        item = (data.get("ItemModule") or {}).get(video_id)
        return item if isinstance(item, dict) else None
    scope = data.get("__DEFAULT_SCOPE__") or {}
    detail = scope.get("webapp.video-detail") or scope.get("webapp.reflow.video.detail") or {}
    item = (detail.get("itemInfo") or {}).get("itemStruct") if isinstance(detail, dict) else None
    if isinstance(item, dict) and item:
        return item
    code = detail.get("statusCode") if isinstance(detail, dict) else None
    if code in UNAVAILABLE_CODES or (isinstance(code, int) and code != 0 and detail.get("statusMsg")):
        raise VideoUnavailable(f"TikTok : {detail.get('statusMsg') or 'publication indisponible'} ({code})")
    return None


def _dicts(x) -> list[dict]:
    return [i for i in x if isinstance(i, dict)] if isinstance(x, list) else []


def parse_item_struct(item: dict, video_id: str) -> PageItem:
    author = item.get("author")
    author_id = author.get("uniqueId", "") if isinstance(author, dict) else (author or "")
    video = item.get("video") if isinstance(item.get("video"), dict) else {}
    image_post = item.get("imagePost") if isinstance(item.get("imagePost"), dict) else {}

    desc = str(item.get("desc") or "")
    title = str(image_post.get("title") or "")
    if title and title not in desc:
        desc = f"{title}\n{desc}".strip()

    tags = {str(t["hashtagName"]) for t in _dicts(item.get("textExtra")) if t.get("hashtagName")}
    tags |= {str(c["title"]) for c in _dicts(item.get("challenges")) if c.get("title")}
    tags |= set(re.findall(r"#([\wÀ-ɏ]+)", desc))

    images = []
    for img in _dicts(image_post.get("images")):
        url = _first((img.get("imageURL") or {}).get("urlList") if isinstance(img.get("imageURL"), dict) else None)
        if url:
            images.append(url)

    subtitles = [
        {
            "lang": s.get("LanguageCodeName", ""),
            "url": s.get("Url", ""),
            "format": s.get("Format", ""),
            "source": s.get("Source", ""),
        }
        for s in _dicts(video.get("subtitleInfos"))
        if s.get("Url")
    ]
    play_url = video.get("playAddr") or video.get("downloadAddr") or ""
    if not isinstance(play_url, str) or not play_url.startswith("http"):
        play_url = ""
    for b in _dicts(video.get("bitrateInfo")):
        if play_url:
            break
        addr = b.get("PlayAddr") if isinstance(b.get("PlayAddr"), dict) else {}
        play_url = _first(addr.get("UrlList")) or ""
    cover = video.get("cover") or video.get("originCover") or ""
    if not cover and images:
        cover = images[0]
    return PageItem(
        video_id=video_id,
        kind="photo" if images else "video",
        author=author_id,
        description=desc,
        hashtags=sorted(tags, key=str.lower),
        cover=cover,
        duration=_float(video.get("duration")) if not images else None,
        image_urls=images,
        subtitles=subtitles,
        play_url="" if images else play_url,
    )


def _float(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _looks_blocked(r: httpx.Response) -> bool:
    if r.status_code in (403, 429):
        return True
    ct = r.headers.get("content-type", "")
    return "text/html" in ct and ("captcha" in r.text[:20000].lower() and "__UNIVERSAL_DATA" not in r.text)


class TikTokPageClient:
    """Client HTTP réutilisé entre les vidéos (garde les cookies que TikTok exige)."""

    def __init__(self, timeout: float = 30, retries: int = 3):
        self.http = httpx.Client(
            headers=HEADERS, follow_redirects=True,
            timeout=httpx.Timeout(timeout, connect=15),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
        self.retries = retries
        self._warm = False

    def close(self) -> None:
        self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _request(self, method: str, url: str, classify: bool = False, **kw) -> httpx.Response:
        """Requête avec nouvelles tentatives (délai croissant) sur les erreurs passagères.
        ``classify`` (page TikTok seulement) : 403/429/captcha → Blocked, 404 → VideoUnavailable.
        Pour un lien CDN signé, un 403/404 veut juste dire « lien expiré »."""
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                r = self.http.request(method, url, **kw)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
                last = e
            else:
                if classify and _looks_blocked(r):
                    raise Blocked(f"TikTok refuse les requêtes ({r.status_code})")
                if classify and r.status_code == 404:
                    raise VideoUnavailable("introuvable (404)")
                if r.status_code not in RETRY_STATUSES:
                    r.raise_for_status()
                    return r
                last = httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
            if attempt < self.retries:
                time.sleep(min(30, 1.5 * 2 ** attempt))
        raise last or RuntimeError("échec réseau")

    def fetch(self, video_id: str) -> PageItem:
        if not str(video_id).isdigit():
            raise VideoUnavailable(f"identifiant invalide : {video_id!r}")
        if not self._warm:
            try:
                self.http.get("https://www.tiktok.com/")  # récupère les cookies exigés par TikTok
            except httpx.HTTPError:
                pass
            self._warm = True
        r = self._request("GET", f"https://www.tiktok.com/@/video/{video_id}", classify=True)
        item = extract_item_struct(r.text, video_id)
        if not item:
            raise ValueError("Données de la page TikTok introuvables (page modifiée ou protection anti-robot).")
        return parse_item_struct(item, video_id)

    def get_text(self, url: str, max_bytes: int = 5 * 1024 * 1024) -> str:
        """Texte (sous-titres…) lu en flux et plafonné : jamais plus de ``max_bytes`` en mémoire."""
        with self.http.stream("GET", url) as r:
            r.raise_for_status()
            buf = bytearray()
            for chunk in r.iter_bytes(64 * 1024):
                buf += chunk
                if len(buf) > max_bytes:
                    raise ValueError("réponse trop volumineuse")
            return bytes(buf).decode(r.encoding or "utf-8", errors="replace")

    def download(self, url: str, dest, max_bytes: int = MAX_DOWNLOAD_BYTES, expect: str = "image/") -> Path:
        """Téléchargement en flux, limité en taille, écrit dans un fichier temporaire puis renommé
        (jamais de fichier à moitié écrit si ça coupe)."""
        dest = Path(dest)
        tmp = dest.with_name(dest.name + ".part")
        for attempt in range(self.retries + 1):
            try:
                with self.http.stream("GET", url) as r:
                    if r.status_code in RETRY_STATUSES and attempt < self.retries:
                        raise httpx.HTTPStatusError(str(r.status_code), request=r.request, response=r)
                    r.raise_for_status()
                    ct = r.headers.get("content-type", "")
                    if expect and ct and not ct.startswith(expect) and "octet-stream" not in ct:
                        raise ValueError(f"type inattendu : {ct}")
                    size = 0
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_bytes(64 * 1024):
                            size += len(chunk)
                            if size > max_bytes:
                                raise ValueError("fichier trop volumineux")
                            f.write(chunk)
                os.replace(tmp, dest)
                return dest
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as e:
                tmp.unlink(missing_ok=True)
                if attempt >= self.retries or (isinstance(e, httpx.HTTPStatusError)
                                               and e.response.status_code not in RETRY_STATUSES):
                    raise
                time.sleep(min(30, 1.5 * 2 ** attempt))
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
        raise RuntimeError("échec du téléchargement")
