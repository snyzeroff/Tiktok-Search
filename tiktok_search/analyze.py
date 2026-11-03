"""Analyse d'une ou plusieurs vidéos : outils, sites, produits, comptes, étapes, codes promo…

Pensée pour coûter le moins possible :

1. **Extraction locale gratuite** (toujours faite) : liens, noms de domaine — y compris
   prononcés à l'oral (« notion point so ») —, @comptes, hashtags, e-mails, codes promo, prix.
2. **IA sur le texte seulement** (mode ``text``, par défaut) : légende + transcription +
   texte à l'écran déjà extraits → quelques milliers de tokens, dans le palier gratuit de
   Gemini, ou 0 € avec Ollama en local.
3. **Smart screens** (mode ``screens``) : seulement si utile, on envoie quelques captures
   *différentes* (voir ``screens.py``), réduites à 768 px — jamais la vidéo entière.
   Pour un carrousel, ce sont directement les pages.
4. **Cache** : un résultat n'est recalculé que si le contenu, le mode ou le modèle change.
   Une analyse multi-vidéos réutilise les analyses individuelles et les fusionne en local
   (aucun appel IA supplémentaire).
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import logging
import re
import shutil
import time
import unicodedata
from pathlib import Path

import httpx

from . import enrich, screens
from .config import Settings
from .db import Database
from .importer import canonical_url

log = logging.getLogger(__name__)

CATEGORIES = {
    "tools": "Outils & applis",
    "websites": "Sites web",
    "products": "Produits, livres, formations",
    "people": "Comptes, personnes & marques",
    "promo_codes": "Codes promo",
}
MAX_CONTEXT_CHARS = 14000

# --- 1. extraction locale -----------------------------------------------------

TLDS = ("com|fr|io|ai|app|co|net|org|dev|gg|me|so|xyz|tv|shop|store|tech|be|ch|ca|uk|de|es|it|eu|"
        "info|link|site|tools|club|pro|studio|design|ly|to|cc|us|online")
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
DOMAIN_RE = re.compile(rf"\b(?:[a-z0-9][a-z0-9-]*\.)+(?:{TLDS})\b(?:/[^\s<>\"')\]]*)?", re.I)
SPOKEN_DOMAIN_RE = re.compile(rf"\b([a-z0-9][a-z0-9-]{{1,40}})\s+(?:point|dot)\s+({TLDS})\b", re.I)
MENTION_RE = re.compile(r"(?<![\w.])@([A-Za-z0-9_.]{2,30})(?<!\.)")
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
PROMO_RE = re.compile(r"\bcode(?:\s+promo)?\s*[:\-]?\s*[«\"']?([A-Z0-9][A-Z0-9_-]{3,19})\b")
PRICE_RE = re.compile(r"(?:[€$£]\s?\d+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?\s?(?:€|euros?|\$|dollars?|£))", re.I)


def fmt_time(t: float | None) -> str:
    if t is None:
        return ""
    t = int(t)
    return f"{t // 60}:{t % 60:02d}"


def _where(seg: dict) -> str:
    if seg.get("page"):
        return f"page {seg['page']}"
    if seg.get("start") is not None:
        return fmt_time(seg["start"])
    return seg.get("label", "")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower().strip()
    s = re.sub(r"^https?://", "", s)
    s = re.sub(r"^www\.", "", s)
    return s.rstrip("/.,;:!?")


def local_extract(video: dict, segments: list[dict]) -> dict:
    """Extraction par expressions régulières, instantanée et gratuite."""
    sources = [{"label": "légende", "text": video.get("description") or ""}] + segments
    found: dict[str, dict[str, dict]] = {k: {} for k in ("links", "domains", "mentions", "emails", "promo_codes", "prices")}

    def add(cat: str, value: str, seg: dict) -> None:
        value = value.strip().rstrip(".,;:!?)")
        if not value:
            return
        key = _norm(value)
        entry = found[cat].setdefault(key, {"value": value, "where": []})
        w = _where(seg)
        if w and w not in entry["where"]:
            entry["where"].append(w)

    for seg in sources:
        text = seg.get("text") or ""
        emails = set(EMAIL_RE.findall(text))
        for e in emails:
            add("emails", e, seg)
        for u in URL_RE.findall(text):
            add("links", u, seg)
            add("domains", _norm(u).split("/")[0], seg)
        for d in DOMAIN_RE.findall(text):
            if not any(d in e for e in emails):
                add("domains", _norm(d).split("/")[0], seg)
        for name, tld in SPOKEN_DOMAIN_RE.findall(text):
            add("domains", f"{name.lower()}.{tld.lower()}", seg)
        for m in MENTION_RE.findall(text):
            if not any(f"@{m}" in e for e in emails):
                add("mentions", "@" + m, seg)
        for c in PROMO_RE.findall(text):
            add("promo_codes", c, seg)
        for p in PRICE_RE.findall(text):
            add("prices", p, seg)
    out = {k: list(v.values()) for k, v in found.items()}
    out["hashtags"] = [h for h in (video.get("hashtags") or "").split() if h]
    return out


# --- 2. contexte texte pour l'IA ------------------------------------------------

def build_context(video: dict, segments: list[dict]) -> str:
    parts = [
        f"Type : {'carrousel photo' if video.get('kind') == 'photo' else 'vidéo'}",
        f"Auteur : @{video.get('author') or '?'}",
        f"Légende : {video.get('description') or '(aucune)'}",
        f"Hashtags : {video.get('hashtags') or '(aucun)'}",
    ]
    tr = [s for s in segments if s["kind"] == "transcript"]
    if tr:
        parts.append("Transcription :\n" + "\n".join(f"[{_where(s)}] {s['text']}" for s in tr))
    oc = [s for s in segments if s["kind"] == "ocr"]
    if oc:
        parts.append("Texte affiché à l'écran :\n" + "\n".join(f"[{_where(s)}] {s['text']}" for s in oc))
    text = "\n\n".join(parts)
    if len(text) > MAX_CONTEXT_CHARS:
        text = text[:MAX_CONTEXT_CHARS] + "\n[…tronqué]"
    return text


PROMPT = """Tu analyses une publication TikTok enregistrée en favori. À partir des informations
ci-dessous (et des captures d'écran éventuelles, chacune précédée de son timecode ou numéro de page),
extrais de façon exhaustive mais sans rien inventer :
- tools : logiciels, applications, outils IA, extensions, services cités ou montrés
- websites : sites web cités ou montrés
- products : produits, livres, formations, matériel
- people : comptes, créateurs, personnes, marques cités
- promo_codes : codes promo / réductions
- key_points : les informations ou conseils clés, en phrases courtes
- steps : les étapes si c'est un tutoriel (sinon liste vide)
Pour chaque élément : "where" = timecode (m:ss) ou "page N" où il apparaît si connu ;
"url" = seulement si elle est affichée/prononcée ou s'il s'agit sans ambiguïté du site officiel, sinon "" ;
"details" = à quoi ça sert / ce qui en est dit (une phrase). Réponds en français.
La transcription automatique peut déformer les noms propres : corrige-les quand c'est évident
(ex. « chat j'ai pété » → ChatGPT).

"""


def _entity(upper: bool) -> dict:
    t = (lambda x: x.upper()) if upper else (lambda x: x)
    return {
        "type": t("object"),
        "properties": {k: {"type": t("string")} for k in ("name", "category", "url", "details", "where")},
        "required": ["name"],
    }


def response_schema(upper: bool = True) -> dict:
    t = (lambda x: x.upper()) if upper else (lambda x: x)
    arr_e = {"type": t("array"), "items": _entity(upper)}
    arr_s = {"type": t("array"), "items": {"type": t("string")}}
    return {
        "type": t("object"),
        "properties": {
            "summary": {"type": t("string")},
            "topic": {"type": t("string")},
            **{k: arr_e for k in CATEGORIES},
            "key_points": arr_s,
            "steps": arr_s,
        },
        "required": ["summary", "topic", *CATEGORIES, "key_points", "steps"],
    }


# --- 3. fournisseurs IA ---------------------------------------------------------

class LLMError(RuntimeError):
    pass


def _post_with_retry(url: str, **kw) -> httpx.Response:
    for attempt in range(4):
        r = httpx.post(url, timeout=180, **kw)
        if r.status_code in (429, 500, 503) and attempt < 3:
            time.sleep(2 ** attempt * 3)  # limites du palier gratuit : on patiente
            continue
        if r.status_code >= 400:
            raise LLMError(f"{r.status_code} : {r.text[:300]}")
        return r
    raise LLMError("trop de tentatives")


def call_gemini(settings: Settings, text: str, images: list[tuple[str, bytes]]) -> tuple[dict, dict]:
    if not settings.gemini_api_key:
        raise LLMError("GEMINI_API_KEY manquante (gratuite sur https://aistudio.google.com/apikey).")
    parts: list[dict] = [{"text": PROMPT + text}]
    for label, data in images:
        parts.append({"text": f"Capture {label} :"})
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(data).decode()}})
    r = _post_with_retry(
        f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_model}:generateContent",
        headers={"x-goog-api-key": settings.gemini_api_key},
        json={
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": response_schema(upper=True),
                "temperature": 0.2,
            },
        },
    )
    data = r.json()
    try:
        out = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
    except (KeyError, IndexError) as e:
        raise LLMError(f"Réponse Gemini inattendue : {json.dumps(data)[:300]}") from e
    u = data.get("usageMetadata", {})
    return json.loads(out), {"input_tokens": u.get("promptTokenCount"), "output_tokens": u.get("candidatesTokenCount")}


def call_ollama(settings: Settings, text: str, images: list[tuple[str, bytes]]) -> tuple[dict, dict]:
    labels = "".join(f"\nImage {i + 1} = capture {label}" for i, (label, _) in enumerate(images))
    r = _post_with_retry(
        f"{settings.ollama_url.rstrip('/')}/api/chat",
        json={
            "model": settings.ollama_model,
            "stream": False,
            "format": response_schema(upper=False),
            "options": {"temperature": 0.2},
            "messages": [{
                "role": "user",
                "content": PROMPT + text + labels,
                "images": [base64.b64encode(d).decode() for _, d in images],
            }],
        },
    )
    data = r.json()
    return json.loads(data["message"]["content"]), {
        "input_tokens": data.get("prompt_eval_count"), "output_tokens": data.get("eval_count")}


PROVIDERS = {"gemini": call_gemini, "ollama": call_ollama}


def model_name(settings: Settings) -> str:
    return {"gemini": settings.gemini_model, "ollama": settings.ollama_model}.get(settings.llm_provider, "")


# --- 4. captures --------------------------------------------------------------

def collect_screens(settings: Settings, video: dict) -> list[tuple[str, Path]]:
    """Captures à envoyer à l'IA, copiées dans data/media/<id>/screen_XX.jpg (affichables)."""
    vid = video["id"]
    media = settings.media_dir / vid
    media.mkdir(parents=True, exist_ok=True)
    n = settings.analysis_max_screens
    if video.get("kind") == "photo":
        pages = sorted(media.glob("page_*.jpg"))
        if len(pages) > n:  # échantillonnage régulier
            pages = [pages[int(i * len(pages) / n)] for i in range(n)]
        return [(f"page {int(p.stem.split('_')[1])}", p) for p in pages]

    for old in media.glob("screen_*.jpg"):
        old.unlink()
    work = settings.tmp_dir / f"screens_{vid}"
    try:
        local = media / "video.mp4"
        video_path = local if local.exists() else enrich.download_video(vid, work)
        if video_path == local:
            work.mkdir(parents=True, exist_ok=True)
            video_path = Path(shutil.copy(local, work / "video.mp4"))
        out = []
        for i, shot in enumerate(screens.smart_screens(video_path, max_screens=n, threshold=12), 1):
            dest = media / f"screen_{i:02d}_{int(shot.t)}.jpg"
            dest.write_bytes(screens.to_jpeg_bytes(shot.path))
            out.append((fmt_time(shot.t), dest))
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --- 5. analyse d'une vidéo -----------------------------------------------------

def choose_mode(mode: str, video: dict, segments: list[dict]) -> str:
    if mode != "auto":
        return mode
    text_len = sum(len(s["text"]) for s in segments) + len(video.get("description") or "")
    if video.get("kind") == "photo" and not any(s["kind"] == "ocr" for s in segments):
        return "screens"  # carrousel sans OCR : les pages SONT le contenu
    return "screens" if text_len < 150 else "text"


def analyze_video(db: Database, settings: Settings, video_id: str, mode: str = "text",
                  force: bool = False) -> dict:
    video = db.get_video(video_id)
    if not video:
        raise KeyError(video_id)
    segments = db.get_segments(video_id)
    mode = choose_mode(mode, video, segments)
    provider = settings.llm_provider if settings.llm_provider in PROVIDERS else "none"
    model = model_name(settings)
    context = build_context(video, segments)
    input_hash = hashlib.sha256(f"{provider}|{model}|{mode}|{context}".encode()).hexdigest()[:16]

    cached = db.get_analysis(video_id, mode)
    if cached and cached["input_hash"] == input_hash and not force:
        return {**cached["result"], "cached": True}

    result: dict = {
        "video_id": video_id,
        "mode": mode,
        "provider": provider,
        "model": model,
        "local": local_extract(video, segments),
        "ai": None,
        "usage": None,
        "screens": [],
        "error": "",
    }
    if provider != "none":
        shots: list[tuple[str, Path]] = []
        try:
            if mode == "screens":
                shots = collect_screens(settings, video)
                result["screens"] = [{"label": lbl, "url": "/media/" + p.relative_to(settings.media_dir).as_posix()}
                                     for lbl, p in shots]
            images = [(lbl, screens.to_jpeg_bytes(p)) for lbl, p in shots]
            result["ai"], result["usage"] = PROVIDERS[provider](settings, context, images)
        except Exception as e:  # noqa: BLE001
            log.exception("analyse IA échouée pour %s", video_id)
            result["error"] = str(e)[:400]

    if not result["error"]:
        db.save_analysis(video_id, mode, input_hash, provider, model, result)
    names = [d["value"] for d in result["local"]["domains"]] + [m["value"] for m in result["local"]["mentions"]]
    for cat in CATEGORIES:
        names += [e.get("name", "") for e in (result["ai"] or {}).get(cat, [])]
    db.save_video(video_id, entities=" · ".join(dict.fromkeys(n for n in names if n)))
    return {**result, "cached": False}


# --- 6. plusieurs vidéos : fusion locale --------------------------------------

def aggregate(db: Database, results: list[dict]) -> dict:
    """Fusionne les analyses par nom (sans accents/casse) et garde d'où vient chaque élément."""
    merged: dict[str, dict[str, dict]] = {k: {} for k in CATEGORIES}
    merged["domains"] = {}
    points: list[dict] = []
    for res in results:
        video = db.get_video(res["video_id"]) or {}
        src_base = {"video_id": res["video_id"], "author": video.get("author", ""),
                    "url": canonical_url(res["video_id"], video.get("author", ""), video.get("kind", "video"))}
        ai = res.get("ai") or {}
        for cat in CATEGORIES:
            for e in ai.get(cat, []):
                name = (e.get("name") or "").strip()
                if not name:
                    continue
                item = merged[cat].setdefault(_norm(name), {
                    "name": name, "category": e.get("category", ""), "url": e.get("url", ""),
                    "details": e.get("details", ""), "sources": []})
                item["url"] = item["url"] or e.get("url", "")
                item["details"] = item["details"] or e.get("details", "")
                item["sources"].append({**src_base, "where": e.get("where", "")})
        for d in res.get("local", {}).get("domains", []):
            item = merged["domains"].setdefault(_norm(d["value"]), {"name": d["value"], "url": "https://" + d["value"],
                                                                    "sources": []})
            item["sources"].append({**src_base, "where": ", ".join(d["where"])})
        if ai.get("summary"):
            points.append({**src_base, "summary": ai["summary"], "topic": ai.get("topic", ""),
                           "key_points": ai.get("key_points", []), "steps": ai.get("steps", [])})
    out = {cat: sorted(items.values(), key=lambda x: (-len(x["sources"]), x["name"].lower()))
           for cat, items in merged.items()}
    out["videos"] = points
    out["count"] = len(results)
    return out


def analyze_many(db: Database, settings: Settings, ids: list[str], mode: str = "text",
                 force: bool = False) -> dict:
    results = [analyze_video(db, settings, vid, mode, force) for vid in ids]
    return {"results": results, "aggregate": aggregate(db, results)}


# --- 7. export ----------------------------------------------------------------

def to_csv(agg: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["catégorie", "nom", "type", "url", "détails", "auteur", "où", "lien vidéo"])
    for cat, label in {**CATEGORIES, "domains": "Domaines détectés"}.items():
        for item in agg.get(cat, []):
            for s in item["sources"]:
                w.writerow([label, item["name"], item.get("category", ""), item.get("url", ""),
                            item.get("details", ""), s["author"], s["where"], s["url"]])
    return buf.getvalue()


def to_markdown(agg: dict) -> str:
    lines = [f"# Analyse de {agg['count']} publication(s) TikTok", ""]
    for cat, label in {**CATEGORIES, "domains": "Domaines détectés"}.items():
        items = agg.get(cat) or []
        if not items:
            continue
        lines += [f"## {label}", ""]
        for it in items:
            name = f"[{it['name']}]({it['url']})" if it.get("url") else it["name"]
            refs = ", ".join(f"[@{s['author'] or '?'}{' ' + s['where'] if s['where'] else ''}]({s['url']})"
                             for s in it["sources"])
            details = f" — {it['details']}" if it.get("details") else ""
            lines.append(f"- **{name}**{details} ({refs})")
        lines.append("")
    if agg.get("videos"):
        lines += ["## Résumés", ""]
        for v in agg["videos"]:
            lines += [f"### [@{v['author'] or '?'}]({v['url']}) — {v['topic']}", "", v["summary"], ""]
            lines += [f"- {p}" for p in v["key_points"]]
            if v["steps"]:
                lines += ["", "Étapes :"] + [f"{i}. {s}" for i, s in enumerate(v["steps"], 1)]
            lines.append("")
    return "\n".join(lines)
