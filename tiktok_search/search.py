"""Recherche plein texte (FTS5 / BM25) avec extraits surlignés."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field

from .db import FTS_COLUMNS, Database

log = logging.getLogger(__name__)

FIELD_LABELS = {
    "description": "légende",
    "hashtags": "hashtags",
    "transcript": "transcription",
    "ocr_text": "texte à l'écran",
    "author": "auteur",
    "entities": "outils & sites (IA)",
}
# Poids BM25 : une correspondance dans la légende/les hashtags compte plus qu'au fil d'une transcription.
WEIGHTS = {"description": 4.0, "hashtags": 4.0, "transcript": 1.0, "ocr_text": 2.0, "author": 3.0, "entities": 3.0}
# Champ de recherche → type de segment horodaté correspondant
SEGMENT_KIND = {"transcript": "transcript", "ocr_text": "ocr"}

MARK_OPEN, MARK_CLOSE = "\x02", "\x03"  # remplacés côté affichage, évite toute injection HTML


@dataclass
class SearchResult:
    video: dict
    score: float
    matches: dict[str, str]  # colonne -> extrait surligné
    hits: list[dict] = field(default_factory=list)  # segments horodatés / pages qui correspondent


def build_match_query(query: str, any_term: bool = False) -> str:
    """Transforme une requête utilisateur en requête FTS5 sûre.

    - ``"expression exacte"`` entre guillemets → phrase
    - ``#hashtag`` → mot (le # est retiré)
    - ``-mot`` → exclusion
    - ``mot*`` → préfixe ; par défaut le dernier mot est aussi cherché en préfixe
    - tous les termes sont requis (ET), sauf si ``any_term`` (OU)
    """
    tokens = re.findall(r'(-?)"([^"]+)"|(-?)(\S+)', query[:500])[:32]
    positives: list[str] = []
    negatives: list[str] = []
    for i, (neg_q, phrase, neg_w, word) in enumerate(tokens):
        if phrase:
            term, neg = phrase, neg_q
        else:
            term, neg = word, neg_w
        prefix = term.endswith("*")
        term = re.sub(r'[\"#*()^:{}\[\]]', " ", term).strip()
        if not term or not re.search(r"\w", term):  # « - », « & », émojis seuls : rien à chercher
            continue
        fts = '"' + term.replace('"', "") + '"'
        if not phrase and (prefix or (not neg and i == len(tokens) - 1 and len(term) >= 3)):
            fts += "*"
        (negatives if neg else positives).append(fts)
    if not positives:
        return ""
    q = (" OR " if any_term else " AND ").join(positives)
    if negatives:
        q = f"({q}) NOT ({' OR '.join(negatives)})"
    return q


def _segment_matches(db: Database, ids: list[str], query: str, kinds: list[str],
                     per_video: int | None) -> dict[str, list[dict]]:
    """Segments correspondants pour plusieurs vidéos en UNE requête par passe.

    Les mots d'une requête peuvent être répartis sur plusieurs phrases : pour les vidéos où
    aucun segment ne contient *tous* les mots, une seconde passe cherche au moins un des mots.
    """
    found: dict[str, list[dict]] = {i: [] for i in ids}
    if not ids or not kinds or not query.strip():
        return found
    for any_term in (False, True):
        remaining = [i for i in ids if not found[i]]
        match = build_match_query(query, any_term=any_term)
        if not remaining or not match:
            break
        sql = f"""
            SELECT s.id, s.video_id, s.kind, s.start, s.end, s.page,
                   highlight(segments_fts, 0, '{MARK_OPEN}', '{MARK_CLOSE}') AS hl
            FROM segments_fts JOIN segments s ON s.id = segments_fts.rowid
            WHERE segments_fts MATCH ?
              AND s.video_id IN ({",".join("?" * len(remaining))})
              AND s.kind IN ({",".join("?" * len(kinds))})
            ORDER BY s.video_id, COALESCE(s.page, 0), COALESCE(s.start, 0), s.id
        """
        try:
            with db.connect() as conn:
                rows = conn.execute(sql, (match, *remaining, *kinds)).fetchall()
        except sqlite3.OperationalError as e:
            if "locked" in str(e):
                raise
            log.warning("requête FTS segments rejetée : %s", e)
            break
        for r in rows:
            lst = found[r["video_id"]]
            if per_video is None or len(lst) < per_video:
                lst.append(dict(r))
    return found


def _public_hit(r: dict, match: bool = True) -> dict:
    return {"kind": r["kind"], "start": r["start"], "end": r["end"], "page": r["page"],
            "text": r.get("hl", r.get("text", "")), "match": match}


def segment_hits_many(db: Database, ids: list[str], query: str, kinds: list[str] | None = None,
                      per_video: int = 30) -> dict[str, list[dict]]:
    found = _segment_matches(db, ids, query, kinds or ["transcript", "ocr"], per_video)
    return {vid: [_public_hit(r) for r in rows] for vid, rows in found.items()}


def segment_hits(
    db: Database, video_id: str, query: str, kinds: list[str] | None = None, limit: int = 30,
    all_segments: bool = False,
) -> list[dict]:
    """Segments d'une vidéo qui correspondent à la requête, surlignés, triés par moment/page.
    Avec ``all_segments``, renvoie tous les segments (les correspondants étant surlignés)."""
    kinds = kinds or ["transcript", "ocr"]
    if not all_segments:
        return segment_hits_many(db, [video_id], query, kinds, limit)[video_id]
    hits = {r["id"]: r for r in _segment_matches(db, [video_id], query, kinds, None)[video_id]}
    out = []
    for seg in db.get_segments(video_id):
        if seg["kind"] in kinds:
            hit = hits.get(seg["id"])
            out.append(_public_hit({**seg, "hl": hit["hl"] if hit else seg["text"]}, bool(hit)))
    return out


def search(
    db: Database,
    query: str,
    fields: list[str] | None = None,
    any_term: bool = False,
    limit: int = 50,
    offset: int = 0,
    kind: str | None = None,
    sort: str = "relevance",
) -> list[SearchResult]:
    match = build_match_query(query, any_term=any_term)
    if not match:
        return []
    cols = [c for c in (fields or FTS_COLUMNS) if c in FTS_COLUMNS]
    if not cols:
        return []
    if set(cols) != set(FTS_COLUMNS):
        match = "{" + " ".join(cols) + "} : (" + match + ")"

    weights = ", ".join(str(WEIGHTS[c]) for c in FTS_COLUMNS)
    snippets = ", ".join(
        f"snippet(videos_fts, {i + 1}, '{MARK_OPEN}', '{MARK_CLOSE}', '…', 14) AS s_{c}"
        for i, c in enumerate(FTS_COLUMNS)
    )
    where, params = "videos_fts MATCH ?", [match]
    if kind in ("video", "photo"):
        where += " AND v.kind = ?"
        params.append(kind)
    order = "v.favorited_at DESC" if sort == "date" else "score"
    sql = f"""
        SELECT v.*, bm25(videos_fts, 0, {weights}) AS score, {snippets}
        FROM videos_fts JOIN videos v ON v.rowid = videos_fts.rowid
        WHERE {where}
        ORDER BY {order}
        LIMIT ? OFFSET ?
    """
    results = []
    try:
        with db.connect() as conn:
            rows = [dict(r) for r in conn.execute(sql, (*params, limit, offset))]
    except sqlite3.OperationalError as e:
        if "locked" in str(e) or "no such table" in str(e):
            raise
        log.warning("requête FTS rejetée (%r → %r) : %s", query, match, e)
        return []
    seg_kinds = [SEGMENT_KIND[c] for c in cols if c in SEGMENT_KIND]
    all_hits = segment_hits_many(db, [r["id"] for r in rows], query, seg_kinds) if seg_kinds else {}
    for row in rows:
        matches = {c: row.pop(f"s_{c}") for c in FTS_COLUMNS}
        matches = {c: s for c, s in matches.items() if s and MARK_OPEN in s and c in cols}
        score = row.pop("score")
        try:
            row["images"] = json.loads(row.get("images") or "[]")
        except json.JSONDecodeError:
            row["images"] = []
        hits = all_hits.get(row["id"], [])
        results.append(SearchResult(video=row, score=-score, matches=matches, hits=hits))
    return results
