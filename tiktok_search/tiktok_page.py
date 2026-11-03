"""Lecture directe d'une page TikTok (vidéo OU carrousel photo), sans clé API.

Chaque page TikTok embarque un JSON (``__UNIVERSAL_DATA_FOR_REHYDRATION__``, anciennement
``SIGI_STATE``) qui contient tout ce qu'il nous faut en une seule requête : légende,
hashtags, auteur, durée, miniature, **sous-titres horodatés** et, pour un carrousel, la
liste des **images**. C'est aussi ce qu'utilise yt-dlp en interne, mais yt-dlp ne gère
pas les carrousels photo.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import httpx

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


def _first(urls) -> str:
    if isinstance(urls, list) and urls:
        return urls[0]
    return urls if isinstance(urls, str) else ""


def extract_item_struct(html: str, video_id: str) -> dict | None:
    m = _SCRIPT_RE.search(html)
    if not m:
        return None
    data = json.loads(m.group(2))
    if m.group(1) == "SIGI_STATE":
        return (data.get("ItemModule") or {}).get(video_id)
    scope = data.get("__DEFAULT_SCOPE__", {})
    detail = scope.get("webapp.video-detail") or scope.get("webapp.reflow.video.detail") or {}
    return (detail.get("itemInfo") or {}).get("itemStruct")


def parse_item_struct(item: dict, video_id: str) -> PageItem:
    author = item.get("author")
    author_id = author.get("uniqueId", "") if isinstance(author, dict) else (author or "")
    video = item.get("video") or {}
    image_post = item.get("imagePost") or {}

    desc = item.get("desc") or ""
    title = image_post.get("title") or ""
    if title and title not in desc:
        desc = f"{title}\n{desc}".strip()

    tags = {t.get("hashtagName") for t in item.get("textExtra") or [] if t.get("hashtagName")}
    tags |= {c.get("title") for c in item.get("challenges") or [] if c.get("title")}
    tags |= set(re.findall(r"#([\wÀ-ɏ]+)", desc))

    images = []
    for img in image_post.get("images") or []:
        url = _first((img.get("imageURL") or {}).get("urlList"))
        if url:
            images.append(url)

    subtitles = [
        {
            "lang": s.get("LanguageCodeName", ""),
            "url": s.get("Url", ""),
            "format": s.get("Format", ""),
            "source": s.get("Source", ""),
        }
        for s in video.get("subtitleInfos") or []
        if s.get("Url")
    ]
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
        duration=float(video["duration"]) if video.get("duration") and not images else None,
        image_urls=images,
        subtitles=subtitles,
    )


class TikTokPageClient:
    """Client HTTP réutilisé entre les vidéos (garde les cookies que TikTok exige)."""

    def __init__(self, timeout: float = 30):
        self.http = httpx.Client(headers=HEADERS, follow_redirects=True, timeout=timeout)
        self._warm = False

    def close(self) -> None:
        self.http.close()

    def fetch(self, video_id: str) -> PageItem:
        if not self._warm:
            try:
                self.http.get("https://www.tiktok.com/")
            except httpx.HTTPError:
                pass
            self._warm = True
        r = self.http.get(f"https://www.tiktok.com/@/video/{video_id}")
        r.raise_for_status()
        item = extract_item_struct(r.text, video_id)
        if not item:
            raise ValueError("Données de la page TikTok introuvables (vidéo supprimée, privée ou page modifiée).")
        return parse_item_struct(item, video_id)

    def get_text(self, url: str) -> str:
        r = self.http.get(url)
        r.raise_for_status()
        return r.text

    def download(self, url: str, dest) -> None:
        with self.http.stream("GET", url) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
