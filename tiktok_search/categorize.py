"""Tri automatique des favoris en catégories.

1. ``propose`` : plusieurs jeux de catégories (chacune = un nom + des mots-clés FR/EN).
   - avec l'IA (Gemini / Ollama) si elle est configurée : **une seule requête** sur un échantillon
     de tes favoris (légendes + hashtags), qui invente des jeux adaptés à ta bibliothèque ;
   - toujours : des jeux prêts à l'emploi (« Thèmes », « Usage ») et un jeu tiré de **tes hashtags**.
2. Tu modifies les noms et mots-clés, tu ajoutes ou retires des catégories.
3. ``preview`` montre la répartition, ``apply`` range chaque vidéo dans la catégorie dont les
   mots-clés correspondent le mieux (légende, hashtags, transcription, texte à l'écran, outils
   détectés), avec l'index de recherche : **local, instantané, gratuit**, même pour 15 000 vidéos.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections import Counter

from .db import FTS_COLUMNS, Database
from .search import WEIGHTS

log = logging.getLogger(__name__)

OTHER = "Autre"
KV_SET = "auto_categories_set"
# Hashtags qui ne disent rien du sujet.
GENERIC_TAGS = {
    "fyp", "foryou", "foryoupage", "fy", "fypシ", "fypage", "pourtoi", "pourtoii", "viral", "viralvideo", "tiktok",
    "trending", "trend", "xyzbca", "parati", "explore", "explorepage", "capcut", "duet", "stitch", "pov", "fypp",
    "tiktokfrance", "france", "foru", "blowthisup", "goviral", "4u", "foryourpage", "abonnetoi", "pt", "fr",
}

PRESETS = [
    {
        "id": "themes",
        "name": "Thèmes",
        "description": "Le sujet de la publication.",
        "categories": [
            {"name": "Cuisine & recettes", "keywords": ["recette", "recipe", "cuisine", "cooking", "food", "plat", "dessert", "gateau", "cake", "ingredients", "four", "airfryer", "repas", "meal", "patisserie", "pasta", "pates", "chef"]},
            {"name": "Sport & fitness", "keywords": ["sport", "fitness", "workout", "musculation", "gym", "exercice", "exercise", "abdos", "gainage", "running", "course", "cardio", "yoga", "stretching", "entrainement", "training", "muscle"]},
            {"name": "Tech & IA", "keywords": ["ia", "ai", "chatgpt", "intelligence artificielle", "tech", "app", "application", "logiciel", "software", "iphone", "android", "ordinateur", "pc", "code", "coding", "programmation", "gadget", "outil", "tool"]},
            {"name": "Business & argent", "keywords": ["business", "argent", "money", "entrepreneur", "investir", "invest", "bourse", "crypto", "finance", "budget", "economiser", "epargne", "side hustle", "marketing", "vendre", "freelance", "startup"]},
            {"name": "Beauté & mode", "keywords": ["makeup", "maquillage", "skincare", "beaute", "beauty", "mode", "fashion", "outfit", "tenue", "style", "cheveux", "hair", "ongles", "nails", "parfum", "soin"]},
            {"name": "Voyage", "keywords": ["voyage", "travel", "vacances", "trip", "destination", "hotel", "plage", "beach", "roadtrip", "visiter", "pays", "city", "ville", "avion"]},
            {"name": "Maison & DIY", "keywords": ["maison", "home", "deco", "decoration", "diy", "bricolage", "rangement", "organisation", "menage", "cleaning", "jardin", "garden", "renovation", "appartement", "interior"]},
            {"name": "Études & apprentissage", "keywords": ["etudes", "study", "apprendre", "learn", "cours", "lecon", "examen", "bac", "revision", "anglais", "english", "langue", "maths", "histoire", "science", "education", "methode"]},
            {"name": "Santé & bien-être", "keywords": ["sante", "health", "bien etre", "wellness", "sommeil", "sleep", "stress", "anxiete", "meditation", "nutrition", "regime", "diet", "mental", "therapie"]},
            {"name": "Développement perso", "keywords": ["motivation", "productivite", "productivity", "habitude", "habit", "discipline", "mindset", "confiance", "routine", "objectif", "goal", "developpement personnel", "self improvement"]},
            {"name": "Humour", "keywords": ["humour", "funny", "drole", "mdr", "lol", "blague", "joke", "meme", "sketch", "comedy", "prank"]},
            {"name": "Musique & danse", "keywords": ["musique", "music", "song", "chanson", "danse", "dance", "chorégraphie", "rap", "guitare", "piano", "cover", "concert"]},
            {"name": "Jeux vidéo", "keywords": ["gaming", "gamer", "jeu video", "game", "minecraft", "fortnite", "playstation", "ps5", "xbox", "nintendo", "switch", "steam", "valorant", "league of legends"]},
            {"name": "Animaux", "keywords": ["chien", "dog", "chat", "cat", "animal", "animaux", "chiot", "puppy", "chaton", "kitten", "pet"]},
            {"name": "Art & design", "keywords": ["art", "dessin", "drawing", "peinture", "painting", "design", "graphisme", "illustration", "photo", "photography", "montage", "video editing", "canva", "figma"]},
            {"name": OTHER, "keywords": []},
        ],
    },
    {
        "id": "usage",
        "name": "Usage",
        "description": "Ce que la publication t'apporte.",
        "categories": [
            {"name": "Tutoriels & astuces", "keywords": ["tuto", "tutoriel", "tutorial", "astuce", "tips", "hack", "comment faire", "how to", "etape", "step", "guide", "technique", "secret", "conseil"]},
            {"name": "Recettes", "keywords": ["recette", "recipe", "ingredients", "cuisson", "preparation", "minutes au four"]},
            {"name": "Produits & achats", "keywords": ["produit", "product", "acheter", "buy", "amazon", "shein", "aliexpress", "lien", "link in bio", "promo", "code promo", "reduction", "prix", "price", "haul", "test", "avis", "review"]},
            {"name": "Outils & sites", "keywords": ["site", "website", "app", "application", "outil", "tool", "gratuit", "free", "extension", "logiciel", "ia", "ai", "com"]},
            {"name": "Idées & inspiration", "keywords": ["idee", "idea", "inspiration", "aesthetic", "moodboard", "deco", "outfit", "avant apres", "before after", "transformation"]},
            {"name": "Culture & infos", "keywords": ["savais", "fact", "histoire", "history", "science", "explication", "pourquoi", "why", "documentaire", "actualite", "news", "culture"]},
            {"name": "Divertissement", "keywords": ["funny", "drole", "humour", "mdr", "lol", "meme", "sketch", "prank", "danse", "dance", "music"]},
            {"name": OTHER, "keywords": []},
        ],
    },
]


# --- propositions ---------------------------------------------------------------

def hashtag_set(db: Database, n: int = 12) -> dict | None:
    """Jeu de catégories tiré de tes hashtags les plus fréquents (avec leurs voisins habituels)."""
    with db.connect() as conn:
        rows = [r[0] for r in conn.execute("SELECT hashtags FROM videos WHERE hashtags != ''")]
    posts = [[t for t in dict.fromkeys(_tags(h)) if t not in GENERIC_TAGS] for h in rows]
    freq = Counter(t for p in posts for t in p)
    top = [t for t, c in freq.most_common(80) if c >= 3]
    chosen: list[str] = []
    for t in top:  # évite deux catégories quasi identiques (recette / recettes)
        if not any(t.startswith(c) or c.startswith(t) for c in chosen):
            chosen.append(t)
        if len(chosen) == n:
            break
    if len(chosen) < 3:
        return None
    cats = []
    for t in chosen:
        co = Counter(x for p in posts if t in p for x in p if x != t and x not in chosen)
        cats.append({"name": t.replace("_", " ").capitalize(), "keywords": [t] + [x for x, _ in co.most_common(6)]})
    cats.append({"name": OTHER, "keywords": []})
    return {"id": "hashtags", "name": "Tes hashtags", "description": "Tes hashtags les plus fréquents.",
            "categories": cats}


def _tags(h: str) -> list[str]:
    return [t.lower() for t in re.findall(r"#([\wÀ-ɏ]+)", h or "")]


LLM_PROMPT = """Tu aides à ranger une bibliothèque de vidéos TikTok enregistrées en favoris.
Voici un échantillon de leurs légendes et hashtags (une vidéo par ligne).

Propose 3 jeux de catégories DIFFÉRENTS pour ranger TOUTE la bibliothèque, par exemple un par
grand thème, un par usage, un plus détaillé adapté aux sujets qui reviennent le plus.
Chaque jeu : 6 à 14 catégories courtes en français, qui ne se chevauchent pas, plus une
catégorie « Autre ». Pour chaque catégorie, 10 à 25 mots-clés en minuscules, en français ET en
anglais, sans accents ni #, qui apparaîtraient dans la légende, les hashtags ou ce qui est dit.

Réponds uniquement en JSON :
{"sets": [{"name": "...", "description": "une phrase", "categories": [{"name": "...", "keywords": ["...", "..."]}]}]}

Échantillon :
"""


def llm_sets(db: Database, settings, sample_size: int = 300) -> list[dict]:
    from . import analyze

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT description, hashtags FROM videos WHERE status = 'done' AND (description != '' OR hashtags != '') "
            "ORDER BY RANDOM() LIMIT ?", (sample_size,)).fetchall()
    if len(rows) < 10:
        return []
    lines = [re.sub(r"\s+", " ", f"{r['description'][:140]} {r['hashtags'][:80]}").strip() for r in rows]
    data = analyze.llm_json(settings, LLM_PROMPT + "\n".join(f"- {line}" for line in lines))
    out = []
    for i, s in enumerate(data.get("sets") or []):
        cleaned = normalize_set({**s, "id": f"ia-{i + 1}"}) if isinstance(s, dict) else None
        if cleaned and len(cleaned["categories"]) >= 3:
            cleaned["by_ai"] = True
            out.append(cleaned)
    return out


def propose(db: Database, settings) -> dict:
    """→ {"sets": [...], "llm_error": str | None}. Les jeux de l'IA d'abord, s'il y en a."""
    from . import analyze

    sets: list[dict] = []
    error = None
    if settings.llm_provider in analyze.PROVIDERS:
        try:
            sets += llm_sets(db, settings)
        except Exception as e:  # noqa: BLE001 - les jeux locaux restent proposés
            log.warning("propositions IA impossibles : %s", e)
            error = str(e)[:300]
    tags = hashtag_set(db)
    if tags:
        sets.append(tags)
    sets += [json.loads(json.dumps(p)) for p in PRESETS]
    return {"sets": sets, "llm_error": error}


# --- tri ------------------------------------------------------------------------

def normalize_set(s: dict) -> dict | None:
    """Nettoie un jeu (venant de l'IA ou de l'interface) ; ``None`` s'il est inutilisable."""
    cats, seen = [], set()
    for c in (s.get("categories") or [])[:40]:
        if not isinstance(c, dict):
            continue
        name = re.sub(r"\s+", " ", str(c.get("name") or "")).strip()[:60]
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        kws = c.get("keywords") or []
        if isinstance(kws, str):
            kws = kws.split(",")
        kws = list(dict.fromkeys(k for k in (re.sub(r"\s+", " ", str(x)).strip().lower().lstrip("#")[:40]
                                             for x in kws[:60]) if k and re.search(r"\w", k)))
        cats.append({"name": name, "keywords": kws})
    if not cats:
        return None
    return {"id": str(s.get("id") or "perso")[:40], "name": str(s.get("name") or "Mes catégories")[:60],
            "description": str(s.get("description") or "")[:200], "categories": cats}


def _match_query(keywords: list[str]) -> str:
    parts = []
    for k in keywords:
        term = re.sub(r'[\"*()^:{}\[\]#]', " ", k).strip()
        if not term:
            continue
        # « recette » trouve aussi « recettes » ; les mots courts (ia, ai, pc) restent exacts.
        parts.append(f'"{term}"*' if len(term) >= 4 and " " not in term else f'"{term}"')
    return " OR ".join(parts)


COLUMNS = [c for c in FTS_COLUMNS if c != "author"]


def _scores(db: Database, keywords: list[str]) -> dict[str, float]:
    match = _match_query(keywords)
    if not match:
        return {}
    weights = ", ".join(str(WEIGHTS[c] if c in COLUMNS else 0.0) for c in FTS_COLUMNS)
    sql = (f"SELECT v.id, bm25(videos_fts, 0, {weights}) AS s FROM videos_fts JOIN videos v ON v.rowid = videos_fts.rowid "
           f"WHERE videos_fts MATCH ? AND v.status = 'done'")
    try:
        with db.connect() as conn:
            return {r[0]: -r[1] for r in conn.execute(sql, ("{" + " ".join(COLUMNS) + "} : (" + match + ")",))}
    except sqlite3.OperationalError as e:
        log.warning("mots-clés rejetés (%s) : %s", match[:200], e)
        return {}


def assign(db: Database, cset: dict) -> tuple[dict[str, list[tuple[str, float]]], list[str]]:
    """→ ({catégorie: [(video_id, score)]}, vidéos sans correspondance). Une catégorie par vidéo :
    celle dont les mots-clés correspondent le mieux."""
    best: dict[str, tuple[str, float]] = {}
    for c in cset["categories"]:
        if c["name"] == OTHER or not c["keywords"]:
            continue
        for vid, score in _scores(db, c["keywords"]).items():
            if score > 0 and (vid not in best or score > best[vid][1]):
                best[vid] = (c["name"], score)
    groups: dict[str, list[tuple[str, float]]] = {c["name"]: [] for c in cset["categories"]}
    for vid, (name, score) in best.items():
        groups[name].append((vid, round(score, 3)))
    with db.connect() as conn:
        all_ids = [r[0] for r in conn.execute("SELECT id FROM videos WHERE status = 'done'")]
    unmatched = [v for v in all_ids if v not in best]
    return groups, unmatched


def preview(db: Database, cset: dict, examples: int = 3) -> dict:
    cset = normalize_set(cset) or {"categories": []}
    groups, unmatched = assign(db, cset)
    ids = [vid for g in groups.values() for vid, _ in sorted(g, key=lambda x: -x[1])[:examples]]
    captions = {}
    if ids:
        with db.connect() as conn:
            captions = {r[0]: (r[1] or r[2] or "")[:90] for r in conn.execute(
                f"SELECT id, description, hashtags FROM videos WHERE id IN ({','.join('?' * len(ids))})", ids)}
    cats = []
    for c in cset["categories"]:
        members = groups.get(c["name"], [])
        if c["name"] == OTHER:
            members = members + [(v, 0.0) for v in unmatched]
        top = [captions.get(v, "") for v, _ in sorted(members, key=lambda x: -x[1])[:examples]]
        cats.append({"name": c["name"], "count": len(members), "examples": [t for t in top if t]})
    if not any(c["name"] == OTHER for c in cset["categories"]) and unmatched:
        cats.append({"name": OTHER, "count": len(unmatched), "examples": []})
    return {"categories": cats, "total": sum(c["count"] for c in cats)}


def apply(db: Database, cset: dict) -> dict:
    """Remplace le tri automatique précédent par celui-ci (les collections TikTok ne sont pas touchées)."""
    cset = normalize_set(cset)
    if not cset:
        raise ValueError("aucune catégorie")
    groups, unmatched = assign(db, cset)
    names = [c["name"] for c in cset["categories"]]
    if OTHER not in names:
        cset["categories"].append({"name": OTHER, "keywords": []})
    groups.setdefault(OTHER, [])
    groups[OTHER] = groups[OTHER] + [(v, None) for v in unmatched]
    keep = set()
    for pos, c in enumerate(cset["categories"]):
        ext = "auto:" + c["name"].lower()
        keep.add(ext)
        cid = db.upsert_category(c["name"], "auto", ext, cset["name"], c["keywords"], pos)
        db.set_category_members(cid, groups.get(c["name"], []))
    db.delete_categories("auto", keep_external_ids=keep)
    db.kv_set(KV_SET, json.dumps(cset, ensure_ascii=False))
    return {"categories": {name: len(groups.get(name, [])) for name in [c["name"] for c in cset["categories"]]}}


def saved_set(db: Database) -> dict | None:
    raw = db.kv_get(KV_SET)
    try:
        return json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return None


def clear(db: Database) -> int:
    db.kv_delete(KV_SET)
    return db.delete_categories("auto")
