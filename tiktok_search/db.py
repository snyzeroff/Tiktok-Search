"""Stockage SQLite + index plein texte FTS5 (insensible aux accents et à la casse).

- ``videos`` / ``videos_fts`` : une ligne par vidéo ou carrousel (texte complet par champ)
- ``segments`` / ``segments_fts`` : morceaux horodatés (phrase de transcription, texte vu à
  l'écran à un instant t, texte d'une page de carrousel) → permet d'afficher les timecodes
  et numéros de page qui correspondent à une recherche
- ``analyses`` : résultats d'analyse IA mis en cache (on ne paie jamais deux fois)
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# Colonnes indexées, dans l'ordre de la table FTS (l'index sert à snippet()/bm25()).
FTS_COLUMNS = ["description", "hashtags", "transcript", "ocr_text", "author", "entities"]
TOKENIZER = "unicode61 remove_diacritics 2"

VIDEO_COLUMNS = {
    "id": "TEXT PRIMARY KEY",
    "url": "TEXT NOT NULL",
    "kind": "TEXT DEFAULT 'video'",        # video | photo (carrousel)
    "author": "TEXT DEFAULT ''",
    "description": "TEXT DEFAULT ''",
    "hashtags": "TEXT DEFAULT ''",
    "transcript": "TEXT DEFAULT ''",
    "ocr_text": "TEXT DEFAULT ''",
    "entities": "TEXT DEFAULT ''",         # noms extraits par l'analyse (outils, sites…)
    "thumbnail": "TEXT DEFAULT ''",        # URL locale (/media/...) ou distante
    "images": "TEXT DEFAULT '[]'",         # JSON : URLs locales des pages du carrousel
    "media_path": "TEXT DEFAULT ''",       # URL locale de la vidéo si conservée
    "duration": "REAL",
    "favorited_at": "TEXT DEFAULT ''",
    "source": "TEXT DEFAULT ''",
    "status": "TEXT DEFAULT 'pending'",    # pending | done | error
    "error": "TEXT DEFAULT ''",
    "updated_at": "REAL",
}

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS videos ({", ".join(f"{k} {v}" for k, v in VIDEO_COLUMNS.items())});

CREATE TABLE IF NOT EXISTS segments (
    id        INTEGER PRIMARY KEY,
    video_id  TEXT NOT NULL,
    kind      TEXT NOT NULL,      -- transcript | ocr
    start     REAL,               -- secondes (vidéo)
    end       REAL,
    page      INTEGER,            -- numéro de page, à partir de 1 (carrousel)
    text      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS segments_video ON segments(video_id);

CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5(text, tokenize = "{TOKENIZER}");

CREATE TABLE IF NOT EXISTS analyses (
    video_id    TEXT NOT NULL,
    mode        TEXT NOT NULL,
    input_hash  TEXT NOT NULL,
    provider    TEXT,
    model       TEXT,
    result      TEXT NOT NULL,
    created_at  REAL,
    PRIMARY KEY (video_id, mode)
);

CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""

FTS_SCHEMA = (
    f"CREATE VIRTUAL TABLE videos_fts USING fts5(id UNINDEXED, {', '.join(FTS_COLUMNS)}, "
    f'tokenize = "{TOKENIZER}")'
)


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(videos)")}
        for col, decl in VIDEO_COLUMNS.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE videos ADD COLUMN {col} {decl.replace('PRIMARY KEY', '')}")
        fts_cols = [r["name"] for r in conn.execute("PRAGMA table_info(videos_fts)")]
        if fts_cols != ["id", *FTS_COLUMNS]:
            conn.execute("DROP TABLE IF EXISTS videos_fts")
            conn.execute(FTS_SCHEMA)
            for row in conn.execute("SELECT * FROM videos").fetchall():
                self._index_row(conn, row)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _index_row(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
        conn.execute("DELETE FROM videos_fts WHERE id = ?", (row["id"],))
        conn.execute(
            f"INSERT INTO videos_fts (id, {', '.join(FTS_COLUMNS)}) VALUES (?{', ?' * len(FTS_COLUMNS)})",
            (row["id"], *(row[c] or "" for c in FTS_COLUMNS)),
        )

    # --- vidéos -----------------------------------------------------------

    def add_favorite(self, video_id: str, url: str, favorited_at: str = "", source: str = "",
                     kind: str = "video") -> bool:
        """Ajoute une vidéo à traiter. Retourne False si elle existait déjà."""
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO videos (id, url, kind, favorited_at, source, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (video_id, url, kind, favorited_at, source, time.time()),
            )
            return cur.rowcount > 0

    def save_video(self, video_id: str, segments: list[dict] | None = None, **fields) -> None:
        """Met à jour une vidéo et réindexe. Si ``segments`` est fourni, il remplace les anciens."""
        if "images" in fields and not isinstance(fields["images"], str):
            fields["images"] = json.dumps(fields["images"])
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.connect() as conn:
            conn.execute(f"UPDATE videos SET {cols} WHERE id = ?", (*fields.values(), video_id))
            row = conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
            if row is not None:
                self._index_row(conn, row)
            if segments is not None:
                old = [r[0] for r in conn.execute("SELECT id FROM segments WHERE video_id = ?", (video_id,))]
                if old:
                    conn.execute(f"DELETE FROM segments_fts WHERE rowid IN ({','.join('?' * len(old))})", old)
                    conn.execute("DELETE FROM segments WHERE video_id = ?", (video_id,))
                for s in segments:
                    if not (s.get("text") or "").strip():
                        continue
                    cur = conn.execute(
                        "INSERT INTO segments (video_id, kind, start, end, page, text) VALUES (?, ?, ?, ?, ?, ?)",
                        (video_id, s["kind"], s.get("start"), s.get("end"), s.get("page"), s["text"].strip()),
                    )
                    conn.execute("INSERT INTO segments_fts (rowid, text) VALUES (?, ?)",
                                 (cur.lastrowid, s["text"].strip()))

    def get_video(self, video_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
        return _video_dict(row) if row else None

    def get_segments(self, video_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM segments WHERE video_id = ? ORDER BY kind, COALESCE(page, 0), COALESCE(start, 0), id",
                (video_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_videos(self, limit: int = 50, offset: int = 0, kind: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM videos WHERE status = 'done'", []
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY favorited_at DESC, updated_at DESC LIMIT ? OFFSET ?"
        with self.connect() as conn:
            return [_video_dict(r) for r in conn.execute(sql, (*params, limit, offset))]

    def videos_to_process(self, retry_errors: bool = False, limit: int | None = None) -> list[dict]:
        statuses = ("pending", "error") if retry_errors else ("pending",)
        sql = f"SELECT * FROM videos WHERE status IN ({', '.join('?' * len(statuses))}) ORDER BY favorited_at DESC"
        params: list = list(statuses)
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connect() as conn:
            return [_video_dict(r) for r in conn.execute(sql, params)]

    def reset_all(self) -> int:
        """Remet toutes les vidéos en attente (pour tout réindexer après une mise à jour)."""
        with self.connect() as conn:
            return conn.execute("UPDATE videos SET status = 'pending', error = ''").rowcount

    def stats(self) -> dict:
        with self.connect() as conn:
            counts = {r["status"]: r["n"] for r in
                      conn.execute("SELECT status, COUNT(*) AS n FROM videos GROUP BY status")}
            kinds = {r["kind"]: r["n"] for r in
                     conn.execute("SELECT kind, COUNT(*) AS n FROM videos GROUP BY kind")}
            with_transcript = conn.execute("SELECT COUNT(*) FROM videos WHERE transcript != ''").fetchone()[0]
            analysed = conn.execute("SELECT COUNT(DISTINCT video_id) FROM analyses").fetchone()[0]
        return {
            "total": sum(counts.values()),
            "pending": counts.get("pending", 0),
            "done": counts.get("done", 0),
            "error": counts.get("error", 0),
            "videos": kinds.get("video", 0),
            "carousels": kinds.get("photo", 0),
            "with_transcript": with_transcript,
            "analysed": analysed,
        }

    # --- analyses -----------------------------------------------------------

    def get_analysis(self, video_id: str, mode: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM analyses WHERE video_id = ? AND mode = ?", (video_id, mode)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["result"] = json.loads(d["result"])
        return d

    def latest_analysis(self, video_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT mode FROM analyses WHERE video_id = ? ORDER BY created_at DESC LIMIT 1",
                               (video_id,)).fetchone()
        return self.get_analysis(video_id, row["mode"]) if row else None

    def save_analysis(self, video_id: str, mode: str, input_hash: str, provider: str, model: str,
                      result: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO analyses VALUES (?, ?, ?, ?, ?, ?, ?)",
                (video_id, mode, input_hash, provider, model, json.dumps(result, ensure_ascii=False), time.time()),
            )

    # --- clé/valeur (jetons OAuth, etc.) ------------------------------------

    def kv_get(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None

    def kv_set(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value))


def _video_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        d["images"] = json.loads(d.get("images") or "[]")
    except json.JSONDecodeError:
        d["images"] = []
    return d
