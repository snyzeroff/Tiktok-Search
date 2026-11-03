"""Récupération des métadonnées, sous-titres, audio et images d'une vidéo TikTok via yt-dlp."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .importer import canonical_url

HASHTAG_RE = re.compile(r"#([\wÀ-ɏ]+)", re.UNICODE)


@dataclass
class VideoMeta:
    video_id: str
    author: str = ""
    description: str = ""
    hashtags: list[str] = field(default_factory=list)
    thumbnail: str = ""
    duration: float | None = None
    subtitles_text: str = ""


def _ydl(opts: dict | None = None):
    import yt_dlp  # import tardif : lourd à charger

    base = {"quiet": True, "no_warnings": True, "noprogress": True, "socket_timeout": 30}
    base.update(opts or {})
    return yt_dlp.YoutubeDL(base)


def parse_vtt(text: str) -> str:
    """Convertit un WebVTT/SRT en texte brut, sans horodatages ni doublons consécutifs."""
    lines: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if (
            not line
            or line == "WEBVTT"
            or "-->" in line
            or line.isdigit()
            or line.startswith(("NOTE", "Kind:", "Language:", "STYLE"))
        ):
            continue
        line = re.sub(r"<[^>]+>", "", line).strip()
        if line and (not lines or lines[-1] != line):
            lines.append(line)
    return " ".join(lines)


def _pick_subtitles(info: dict, preferred: tuple[str, ...] = ("fr", "en")) -> str:
    tracks = {**(info.get("automatic_captions") or {}), **(info.get("subtitles") or {})}
    if not tracks:
        return ""
    langs = sorted(
        tracks,
        key=lambda lang: next((i for i, p in enumerate(preferred) if lang.lower().startswith(p)), len(preferred)),
    )
    for lang in langs:
        for fmt in tracks[lang]:
            url = fmt.get("url")
            if not url or fmt.get("ext") not in (None, "vtt", "srt", "webvtt"):
                continue
            try:
                r = httpx.get(url, timeout=30, follow_redirects=True)
                r.raise_for_status()
                text = parse_vtt(r.text)
                if text:
                    return text
            except httpx.HTTPError:
                continue
    return ""


def fetch_metadata(video_id: str) -> VideoMeta:
    with _ydl({"skip_download": True}) as ydl:
        info = ydl.extract_info(canonical_url(video_id), download=False)
    description = info.get("description") or info.get("title") or ""
    tags = set(HASHTAG_RE.findall(description))
    tags.update(t.lstrip("#") for t in (info.get("tags") or []) if t)
    return VideoMeta(
        video_id=video_id,
        author=info.get("uploader") or info.get("creator") or info.get("channel") or "",
        description=description,
        hashtags=sorted(tags, key=str.lower),
        thumbnail=info.get("thumbnail") or "",
        duration=info.get("duration"),
        subtitles_text=_pick_subtitles(info),
    )


def fetch_oembed(video_id: str) -> VideoMeta:
    """Repli léger (sans yt-dlp) : l'oEmbed public de TikTok donne légende + auteur."""
    r = httpx.get(
        "https://www.tiktok.com/oembed",
        params={"url": f"https://www.tiktok.com/@/video/{video_id}"},
        timeout=20,
    )
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


def download_video(video_id: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    template = str(out_dir / f"{video_id}.%(ext)s")
    with _ydl({"outtmpl": template, "format": "worst[ext=mp4]/worst/best"}) as ydl:
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


def extract_frames(video_path: Path, every_seconds: float = 2.0, max_frames: int = 30) -> list[Path]:
    frames_dir = video_path.parent / f"{video_path.stem}_frames"
    frames_dir.mkdir(exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error", "-i", str(video_path),
            "-vf", f"fps=1/{every_seconds},scale=720:-2", "-frames:v", str(max_frames),
            str(frames_dir / "f_%03d.png"),
        ],
        check=True,
    )
    return sorted(frames_dir.glob("f_*.png"))
