"""Lecture de l'export de données TikTok (« Télécharger tes données », format JSON).

TikTok livre un fichier ``user_data_tiktok.json`` (parfois dans un .zip). La structure
a changé plusieurs fois au fil des années ; on cherche donc récursivement toute liste
dont la clé ressemble à « Favorite Videos » / « FavoriteVideoList » (et, en option,
« Like List »), contenant des objets avec un champ ``Link``/``link`` et ``Date``/``date``.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

VIDEO_ID_RE = re.compile(r"(?:/video/|/v/|item_id=|share/video/)(\d{8,25})")

FAVORITE_KEYS = ("favorite video", "favoritevideo")
LIKE_KEYS = ("like list", "itemfavoritelist", "likelist")


@dataclass(frozen=True)
class FavoriteItem:
    video_id: str
    url: str
    date: str
    kind: str  # "favorite" | "like"


def extract_video_id(url: str) -> str | None:
    m = VIDEO_ID_RE.search(url or "")
    return m.group(1) if m else None


def canonical_url(video_id: str) -> str:
    # TikTok redirige /@/video/<id> vers la bonne URL quel que soit l'auteur.
    return f"https://www.tiktok.com/@/video/{video_id}"


def _norm(key: str) -> str:
    return re.sub(r"[\s_\-]+", " ", key).strip().lower()


def _classify(path: tuple[str, ...]) -> str | None:
    joined = " / ".join(_norm(p) for p in path)
    compact = joined.replace(" ", "")
    if any(k.replace(" ", "") in compact for k in FAVORITE_KEYS):
        return "favorite"
    if any(k.replace(" ", "") in compact for k in LIKE_KEYS):
        return "like"
    return None


def _get(d: dict, *names: str) -> str:
    lower = {k.lower(): v for k, v in d.items()}
    for n in names:
        v = lower.get(n.lower())
        if isinstance(v, str) and v:
            return v
    return ""


def _walk(node: Any, path: tuple[str, ...]) -> Iterable[FavoriteItem]:
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk(v, path + (str(k),))
    elif isinstance(node, list):
        kind = _classify(path)
        for item in node:
            if isinstance(item, dict) and kind:
                link = _get(item, "Link", "VideoLink", "url")
                vid = extract_video_id(link)
                if vid:
                    yield FavoriteItem(vid, link, _get(item, "Date", "FavoriteDate", "time"), kind)
            else:
                yield from _walk(item, path)


def parse_export(data: dict, include_likes: bool = False) -> list[FavoriteItem]:
    seen: dict[str, FavoriteItem] = {}
    for item in _walk(data, ()):
        if item.kind == "like" and not include_likes:
            continue
        # Un favori prime sur un like pour la même vidéo.
        if item.video_id not in seen or (item.kind == "favorite" and seen[item.video_id].kind == "like"):
            seen[item.video_id] = item
    return list(seen.values())


def load_export_bytes(raw: bytes) -> dict:
    """Accepte un .json ou un .zip contenant le JSON de l'export."""
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".json")]
            if not names:
                raise ValueError("Aucun fichier .json trouvé dans l'archive.")
            names.sort(key=lambda n: ("user_data" not in n.lower(), -zf.getinfo(n).file_size))
            raw = zf.read(names[0])
    return json.loads(raw.decode("utf-8-sig"))


def load_export(path: Path | str) -> dict:
    return load_export_bytes(Path(path).read_bytes())


def parse_links_text(text: str) -> list[FavoriteItem]:
    """Liste de liens collés à la main (un par ligne)."""
    items: dict[str, FavoriteItem] = {}
    for token in re.split(r"\s+", text):
        vid = extract_video_id(token)
        if vid and vid not in items:
            items[vid] = FavoriteItem(vid, token, "", "favorite")
    return list(items.values())
