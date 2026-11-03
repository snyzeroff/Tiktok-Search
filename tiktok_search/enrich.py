"""Récupération des métadonnées, sous-titres et médias d'une vidéo/carrousel TikTok.

Ordre des sources, de la plus complète à la plus légère :
1. page TikTok (``tiktok_page``) — gère vidéos ET carrousels, sous-titres horodatés
2. yt-dlp — repli pour les vidéos si la page n'est pas lisible
3. oEmbed public — dernier repli (légende + auteur seulement)
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .importer import canonical_url
from .tiktok_page import PageItem

HASHTAG_RE = re.compile(r"#([\wÀ-ɏ]+)", re.UNICODE)
_TS_RE = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")


@dataclass
class VideoMeta:
    video_id: str
    kind: str = "video"
    author: str = ""
    description: str = ""
    hashtags: list[str] = field(default_factory=list)
    thumbnail: str = ""
    duration: float | None = None
    image_urls: list[str] = field(default_factory=list)
    subtitle_segments: list[dict] = field(default_factory=list)  # [{start, end, text}]

    @classmethod
    def from_page(cls, p: PageItem) -> "VideoMeta":
        return cls(p.video_id, p.kind, p.author, p.description, p.hashtags, p.cover, p.duration, p.image_urls)


# --- sous-titres --------------------------------------------------------------

def _ts(s: str) -> float:
    m = _TS_RE.search(s)
    if not m:
        return 0.0
    h, mi, se, ms = m.groups()
    return int(h or 0) * 3600 + int(mi) * 60 + int(se) + int(ms.ljust(3, "0")) / 1000


def parse_vtt_segments(text: str) -> list[dict]:
    """WebVTT/SRT → [{start, end, text}], sans balises ni doublons consécutifs."""
    segments: list[dict] = []
    current: dict | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if "-->" in line:
            a, _, b = line.partition("-->")
            current = {"start": _ts(a), "end": _ts(b), "text": ""}
            segments.append(current)
            continue
        if (not line or line == "WEBVTT" or line.isdigit()
                or line.startswith(("NOTE", "Kind:", "Language:", "STYLE")) or current is None):
            continue
        line = re.sub(r"<[^>]+>", "", line).strip()
        if line:
            current["text"] = f"{current['text']} {line}".strip()
    out: list[dict] = []
    for s in segments:
        if not s["text"]:
            continue
        if out and out[-1]["text"] == s["text"]:
            out[-1]["end"] = s["end"]
        else:
            out.append(s)
    return out


def parse_vtt(text: str) -> str:
    return " ".join(s["text"] for s in parse_vtt_segments(text))


def pick_subtitle(subs: list[dict], preferred: tuple[str, ...] = ("fr", "en")) -> dict | None:
    if not subs:
        return None

    def rank(s: dict) -> tuple[int, int]:
        lang = (s.get("lang") or "").lower()
        pref = next((i for i, p in enumerate(preferred) if lang.startswith(p)), len(preferred))
        return pref, 0 if (s.get("format") or "").lower() in ("webvtt", "vtt", "") else 1

    return sorted(subs, key=rank)[0]


# --- yt-dlp / oEmbed ----------------------------------------------------------

def _ydl(opts: dict | None = None):
    import yt_dlp  # import tardif : lourd à charger

    base = {"quiet": True, "no_warnings": True, "noprogress": True, "socket_timeout": 30}
    base.update(opts or {})
    return yt_dlp.YoutubeDL(base)


def fetch_metadata_ytdlp(video_id: str) -> VideoMeta:
    with _ydl({"skip_download": True}) as ydl:
        info = ydl.extract_info(canonical_url(video_id), download=False)
    description = info.get("description") or info.get("title") or ""
    tags = set(HASHTAG_RE.findall(description))
    tags.update(t.lstrip("#") for t in (info.get("tags") or []) if t)
    segments: list[dict] = []
    tracks = {**(info.get("automatic_captions") or {}), **(info.get("subtitles") or {})}
    subs = [{"lang": lang, "url": f.get("url"), "format": f.get("ext", "")}
            for lang, fmts in tracks.items() for f in fmts if f.get("url")]
    best = pick_subtitle(subs)
    if best:
        try:
            segments = parse_vtt_segments(httpx.get(best["url"], timeout=30, follow_redirects=True).text)
        except httpx.HTTPError:
            pass
    return VideoMeta(
        video_id=video_id,
        author=info.get("uploader") or info.get("creator") or info.get("channel") or "",
        description=description,
        hashtags=sorted(tags, key=str.lower),
        thumbnail=info.get("thumbnail") or "",
        duration=info.get("duration"),
        subtitle_segments=segments,
    )


def fetch_oembed(video_id: str) -> VideoMeta:
    """Repli léger : l'oEmbed public de TikTok donne légende + auteur + miniature."""
    r = httpx.get("https://www.tiktok.com/oembed",
                  params={"url": f"https://www.tiktok.com/@/video/{video_id}"}, timeout=20)
    r.raise_for_status()
    data = r.json()
    description = data.get("title", "")
    return VideoMeta(
        video_id=video_id,
        author=data.get("author_unique_id") or data.get("author_name", ""),
        description=description,
        hashtags=sorted(set(HASHTAG_RE.findall(description)), key=str.lower),
        thumbnail=data.get("thumbnail_url", ""),
    )


# --- médias -------------------------------------------------------------------

def download_video(video_id: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    template = str(out_dir / f"{video_id}.%(ext)s")
    # Qualité modeste : suffisante pour Whisper/OCR et bien plus légère.
    with _ydl({"outtmpl": template, "format": "best[height<=720][ext=mp4]/best[ext=mp4]/best"}) as ydl:
        info = ydl.extract_info(canonical_url(video_id), download=True)
        return Path(ydl.prepare_filename(info))


def extract_audio(video_path: Path) -> Path:
    """WAV mono 16 kHz, le format attendu par Whisper."""
    audio = video_path.with_suffix(".wav")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000", str(audio)],
        check=True,
    )
    return audio
