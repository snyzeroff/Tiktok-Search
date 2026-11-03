"""OCR du texte incrusté dans la vidéo (optionnel : tesseract + pip install '.[ocr]')."""

from __future__ import annotations

import re
import shutil
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def is_available() -> bool:
    if shutil.which("tesseract") is None:
        return False
    try:
        import pytesseract  # noqa: F401
    except ImportError:
        return False
    return True


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    # Les lignes de bruit OCR sont surtout faites de symboles isolés.
    words = [w for w in text.split(" ") if sum(c.isalnum() for c in w) >= max(1, len(w) // 2)]
    return " ".join(words)


def _safe_lang(lang: str) -> str:
    return lang if re.fullmatch(r"[A-Za-z_]+(\+[A-Za-z_]+)*", lang or "") else "eng"


def ocr_image(path: Path, lang: str = "fra+eng") -> str:
    """OCR adapté aux incrustations TikTok : niveaux de gris, contraste auto, agrandissement
    des petites images, et ``--psm 11`` (texte épars, pas de mise en page de document)."""
    import pytesseract
    from PIL import Image, ImageOps

    with Image.open(path) as im:
        g = ImageOps.autocontrast(ImageOps.grayscale(im))
        if g.width < 1000:
            g = g.resize((g.width * 2, g.height * 2), Image.Resampling.LANCZOS)
        text = pytesseract.image_to_string(g, lang=_safe_lang(lang), config="--psm 11", timeout=60)
    return _clean(text)
