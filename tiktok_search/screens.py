"""« Smart screens » : extraire d'une vidéo un petit nombre de captures *différentes*.

Plutôt que d'envoyer une vidéo entière à une IA (cher) ou une image toutes les N
secondes (redondant), on échantillonne 1 image/s en basse résolution, on calcule une
empreinte perceptuelle (dHash) et on ne garde une image que si elle diffère assez de la
précédente retenue. Résultat : une capture par « écran » réellement différent (nouveau
texte affiché, changement de plan…), chacune avec son timecode.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image


@dataclass
class Screen:
    path: Path
    t: float  # secondes


def dhash(img: Image.Image, size: int = 8) -> int:
    small = img.convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
    px = small.tobytes()
    bits = 0
    for row in range(size):
        for col in range(size):
            left = px[row * (size + 1) + col]
            right = px[row * (size + 1) + col + 1]
            bits = (bits << 1) | (left > right)
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def select_distinct(frames: list[tuple[Path, float]], threshold: int = 10, max_screens: int = 30) -> list[Screen]:
    """Garde les images qui diffèrent d'au moins ``threshold`` bits de la dernière retenue."""
    kept: list[Screen] = []
    last: int | None = None
    for path, t in frames:
        try:
            with Image.open(path) as im:
                h = dhash(im)
        except OSError:  # image tronquée/corrompue : on l'ignore
            continue
        if last is None or hamming(h, last) >= threshold:
            kept.append(Screen(path, t))
            last = h
    if len(kept) > max_screens:  # échantillonnage régulier pour respecter le budget
        step = len(kept) / max_screens
        kept = [kept[int(i * step)] for i in range(max_screens)]
    return kept


def sample_frames(video_path: Path, fps: float = 1.0, width: int = 720, max_frames: int = 240,
                  work_dir: Path | None = None) -> list[tuple[Path, float]]:
    from .enrich import run_ffmpeg

    out = (work_dir or video_path.parent) / f"{video_path.stem}_frames"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    # Vidéos longues : on espace les images pour rester sous max_frames.
    duration = probe_duration(video_path)
    if duration and duration * fps > max_frames:
        fps = max_frames / duration
    run_ffmpeg(["-i", str(video_path), "-vf", f"fps={fps:.4f},scale='min({width},iw)':-2",
                "-frames:v", str(max_frames), "-q:v", "4", str(out / "f_%04d.jpg")])
    files = sorted(out.glob("f_*.jpg"))
    return [(f, round(i / fps, 2)) for i, f in enumerate(files)]


def probe_duration(video_path: Path) -> float | None:
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                            str(video_path)], capture_output=True, text=True, timeout=60)
        return float(r.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def smart_screens(video_path: Path, max_screens: int = 30, threshold: int = 10,
                  work_dir: Path | None = None) -> list[Screen]:
    return select_distinct(sample_frames(video_path, work_dir=work_dir), threshold=threshold, max_screens=max_screens)


def to_jpeg_bytes(path: Path, max_side: int = 768, quality: int = 70) -> bytes:
    """Image réduite et compressée : moins de tokens facturés par l'IA."""
    import io

    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality)
        return buf.getvalue()
