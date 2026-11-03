"""Recherche plein texte (FTS5 / BM25) avec extraits surlignés."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .db import FTS_COLUMNS, Database

FIELD_LABELS = {
    "description": "légende",
    "hashtags": "hashtags",
    "transcript": "transcription",
    "ocr_text": "texte à l'écran",
    "author": "auteur",
}
# Poids BM25 : une correspondance dans la légende/les hashtags compte plus qu'au fil d'une transcription.
WEIGHTS = {"description": 4.0, "hashtags": 4.0, "transcript": 1.0, "ocr_text": 2.0, "author": 3.0}

MARK_OPEN, MARK_CLOSE = "\x02", "\x03"  # remplacés côté affichage, évite toute injection HTML


@dataclass
class SearchResult:
    video: dict
    score: float
    matches: dict[str, str]  # colonne -> extrait surligné


def build_match_query(query: str, any_term: bool = False) -> str:
    """Transforme une requête utilisateur en requête FTS5 sûre.

    - ``"expression exacte"`` entre guillemets → phrase
    - ``#hashtag`` → mot (le # est retiré)
    - ``-mot`` → exclusion
    - ``mot*`` → préfixe ; par défaut le dernier mot est aussi cherché en préfixe
    - tous les termes sont requis (ET), sauf si ``any_term`` (OU)
    """
    tokens = re.findall(r'(-?)"([^"]+)"|(-?)(\S+)', query)
    positives: list[str] = []
    negatives: list[str] = []
    for i, (neg_q, phrase, neg_w, word) in enumerate(tokens):
        if phrase:
            term, neg = phrase, neg_q
        else:
            term, neg = word, neg_w
        prefix = term.endswith("*")
        term = re.sub(r'[\"#*()^:{}\[\]]', " ", term).strip()
        if not term:
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


def search(
    db: Database,
    query: str,
    fields: list[str] | None = None,
    any_term: bool = False,
    limit: int = 50,
    offset: int = 0,
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
    sql = f"""
        SELECT v.*, bm25(videos_fts, 0, {weights}) AS score, {snippets}
        FROM videos_fts JOIN videos v ON v.id = videos_fts.id
        WHERE videos_fts MATCH ?
        ORDER BY score
        LIMIT ? OFFSET ?
    """
    results = []
    with db.connect() as conn:
        for row in conn.execute(sql, (match, limit, offset)):
            row = dict(row)
            matches = {c: row.pop(f"s_{c}") for c in FTS_COLUMNS}
            matches = {c: s for c, s in matches.items() if s and MARK_OPEN in s and c in cols}
            score = row.pop("score")
            results.append(SearchResult(video=row, score=-score, matches=matches))
    return results
