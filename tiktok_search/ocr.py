"""OCR du texte incrusté dans la vidéo (optionnel : tesseract + pip install '.[ocr]')."""

from __future__ import annotations

import re
import shutil
from pathlib import Path


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


def ocr_image(path: Path, lang: str = "fra+eng") -> str:
    import pytesseract
    from PIL import Image

    with Image.open(path) as im:
        return _clean(pytesseract.image_to_string(im, lang=lang))
