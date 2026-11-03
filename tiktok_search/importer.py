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

VIDEO_ID_RE = re.compile(r"(?:/video/|/photo/|/v/|item_id=|share/video/)(\d{8,25})")

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


def canonical_url(video_id: str, author: str = "", kind: str = "video") -> str:
    # TikTok redirige /@/video/<id> vers la bonne URL quel que soit l'auteur (et vers /photo/ pour un carrousel).
    from urllib.parse import quote

    path = "photo" if kind == "photo" else "video"
    return f"https://www.tiktok.com/@{quote(author or '', safe='._-')}/{path}/{video_id}"


def kind_from_url(url: str) -> str:
    return "photo" if "/photo/" in (url or "") else "video"


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


MAX_UNCOMPRESSED = 1024 * 1024 * 1024  # 1 Go : protège contre les « zip bombs »


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_txt_list(text: str) -> list[dict]:
    """Format TXT de l'export TikTok : blocs « Date: … » / « Link: … »."""
    out: list[dict] = []
    date = ""
    for line in text.splitlines():
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key in ("date", "favorite date"):
            date = value
        elif key in ("link", "video link") or VIDEO_ID_RE.search(line):
            link = value if key in ("link", "video link") else line.strip()
            out.append({"Date": date, "Link": link})
            date = ""
    return out


def load_export_bytes(raw: bytes) -> dict:
    """Accepte un .json, un .txt, ou un .zip contenant l'export (format JSON ou TXT)."""
    if raw[:2] == b"PK":
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile as e:
            raise ValueError("Archive .zip corrompue.") from e
        with zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
            if sum(i.file_size for i in infos) > MAX_UNCOMPRESSED * 4:
                raise ValueError("Archive trop volumineuse une fois décompressée.")
            jsons = [i for i in infos if i.filename.lower().endswith(".json")]
            if jsons:
                jsons.sort(key=lambda i: ("user_data" not in i.filename.lower(), -i.file_size))
                if jsons[0].file_size > MAX_UNCOMPRESSED:
                    raise ValueError("Fichier JSON trop volumineux.")
                return _loads(zf.read(jsons[0]))
            txts = [i for i in infos if i.filename.lower().endswith(".txt")
                    and _classify((Path(i.filename).stem,)) and i.file_size < MAX_UNCOMPRESSED]
            if not txts:
                raise ValueError("Ni JSON ni « Favorite Videos.txt » trouvé dans l'archive.")
            return {Path(i.filename).stem: parse_txt_list(_decode(zf.read(i))) for i in txts}
    text = _decode(raw).strip()
    if text.startswith(("{", "[")):
        return _loads(raw)
    return {"Favorite Videos": parse_txt_list(text)}


def _loads(raw: bytes):
    try:
        data = json.loads(_decode(raw))
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON invalide : {e}") from e
    if not isinstance(data, (dict, list)):
        raise ValueError("JSON inattendu.")
    return data if isinstance(data, dict) else {"Favorite Videos": data}


def load_export(path: Path | str) -> dict:
    return load_export_bytes(Path(path).read_bytes())


SHORT_LINK_RE = re.compile(r"https?://(?:vm|vt|m)\.tiktok\.com/[\w/-]+|https?://(?:www\.)?tiktok\.com/t/[\w/-]+")


def resolve_short_links(text: str, timeout: float = 15) -> str:
    """Remplace les liens courts (vm.tiktok.com/…) par l'URL complète contenant l'ID."""
    shorts = list(dict.fromkeys(SHORT_LINK_RE.findall(text)))[:500]
    if not shorts:
        return text
    import httpx

    from .tiktok_page import HEADERS

    with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=timeout) as client:
        for short in shorts:
            try:
                final = str(client.head(short).url)
                if not extract_video_id(final):
                    final = str(client.get(short).url)
            except httpx.HTTPError:
                continue
            if extract_video_id(final):
                text = text.replace(short, final)
    return text


def parse_links_text(text: str) -> list[FavoriteItem]:
    """Liste de liens collés à la main (un par ligne)."""
    items: dict[str, FavoriteItem] = {}
    for token in re.split(r"\s+", text):
        vid = extract_video_id(token)
        if vid and vid not in items:
            items[vid] = FavoriteItem(vid, token, "", "favorite")
    return list(items.values())
