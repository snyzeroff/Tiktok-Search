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
import threading
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
    "status": "TEXT DEFAULT 'pending'",
    "media_pending": "INTEGER DEFAULT 0",  # 1 = indexée, transcription/OCR encore à faire    # pending | done | error | unavailable (supprimée/privée)
    "attempts": "INTEGER DEFAULT 0",
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

-- Données d'une publication lues dans la liste des favoris, en attendant l'indexation
-- (évite d'ouvrir la page de chaque vidéo ; les liens qu'elles contiennent expirent en quelques heures).
CREATE TABLE IF NOT EXISTS item_cache (video_id TEXT PRIMARY KEY, data TEXT NOT NULL, fetched_at REAL);

-- Catégories : collections TikTok (source « tiktok ») ou tri automatique (source « auto »).
CREATE TABLE IF NOT EXISTS categories (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    source      TEXT NOT NULL,
    external_id TEXT NOT NULL DEFAULT '',
    set_name    TEXT DEFAULT '',
    keywords    TEXT DEFAULT '[]',
    position    INTEGER DEFAULT 0,
    UNIQUE (source, external_id)
);
CREATE TABLE IF NOT EXISTS video_categories (
    video_id    TEXT NOT NULL,
    category_id INTEGER NOT NULL,
    score       REAL,
    PRIMARY KEY (video_id, category_id)
);
CREATE INDEX IF NOT EXISTS idx_video_categories_cat ON video_categories (category_id);
"""

FTS_VERSION = "2"

FTS_SCHEMA = (
    f"CREATE VIRTUAL TABLE videos_fts USING fts5(id UNINDEXED, {', '.join(FTS_COLUMNS)}, "
    f'tokenize = "{TOKENIZER}")'
)


class Database:
    """Une connexion SQLite par thread (réutilisée), en mode WAL : l'interface peut lire et
    chercher pendant que l'indexation écrit en arrière-plan."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)
            conn.execute("CREATE INDEX IF NOT EXISTS videos_status ON videos(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS videos_fav ON videos(favorited_at)")

    def _migrate(self, conn: sqlite3.Connection) -> None:
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(videos)")}
        for col, decl in VIDEO_COLUMNS.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE videos ADD COLUMN {col} {decl.replace('PRIMARY KEY', '')}")
        fts_cols = [r["name"] for r in conn.execute("PRAGMA table_info(videos_fts)")]
        version = conn.execute("SELECT value FROM kv WHERE key = 'fts_version'").fetchone()
        if fts_cols != ["id", *FTS_COLUMNS] or not version or version[0] != FTS_VERSION:
            conn.execute("DROP TABLE IF EXISTS videos_fts")
            conn.execute(FTS_SCHEMA)
            for row in conn.execute("SELECT rowid AS _rowid, * FROM videos").fetchall():
                self._index_row(conn, row)
            conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES ('fts_version', ?)", (FTS_VERSION,))

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=60, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=60000")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Transaction : validée à la sortie, annulée en cas d'exception."""
        conn = self._conn()
        depth = getattr(self._local, "depth", 0)
        self._local.depth = depth + 1
        try:
            yield conn
            if depth == 0:
                conn.commit()
        except BaseException:
            if depth == 0:
                conn.rollback()
            raise
        finally:
            self._local.depth = depth

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    @staticmethod
    def _index_row(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
        # rowid FTS = rowid de la vidéo : suppression/jointure directes, sans parcourir l'index.
        conn.execute("DELETE FROM videos_fts WHERE rowid = ?", (row["_rowid"],))
        conn.execute(
            f"INSERT INTO videos_fts (rowid, id, {', '.join(FTS_COLUMNS)}) VALUES (?, ?{', ?' * len(FTS_COLUMNS)})",
            (row["_rowid"], row["id"], *(row[c] or "" for c in FTS_COLUMNS)),
        )

    # --- vidéos -----------------------------------------------------------

    def add_favorite(self, video_id: str, url: str, favorited_at: str = "", source: str = "",
                     kind: str = "video") -> bool:
        """Ajoute une vidéo à traiter. Retourne False si elle existait déjà."""
        with self._write_lock, self.connect() as conn:
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
        unknown = set(fields) - set(VIDEO_COLUMNS)
        if unknown:
            raise ValueError(f"colonnes inconnues : {unknown}")
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._write_lock, self.connect() as conn:
            conn.execute(f"UPDATE videos SET {cols} WHERE id = ?", (*fields.values(), video_id))
            row = conn.execute("SELECT rowid AS _rowid, * FROM videos WHERE id = ?", (video_id,)).fetchone()
            if row is not None:
                self._index_row(conn, row)
            if segments is not None:
                old = [r[0] for r in conn.execute("SELECT id FROM segments WHERE video_id = ?", (video_id,))]
                if old:
                    conn.execute(f"DELETE FROM segments_fts WHERE rowid IN ({','.join('?' * len(old))})", old)
                    conn.execute("DELETE FROM segments WHERE video_id = ?", (video_id,))
                rows = [
                    (video_id, s["kind"], s.get("start"), s.get("end"), s.get("page"), s["text"].strip())
                    for s in segments if (s.get("text") or "").strip()
                ]
                if rows:
                    conn.executemany(
                        "INSERT INTO segments (video_id, kind, start, end, page, text) VALUES (?, ?, ?, ?, ?, ?)", rows)
                    conn.execute(
                        "INSERT INTO segments_fts (rowid, text) SELECT id, text FROM segments WHERE video_id = ?",
                        (video_id,))

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

    def list_videos(self, limit: int = 50, offset: int = 0, kind: str | None = None,
                    category: int | None = None) -> list[dict]:
        sql, params = "SELECT * FROM videos WHERE status = 'done'", []
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if category:
            sql += " AND id IN (SELECT video_id FROM video_categories WHERE category_id = ?)"
            params.append(category)
        sql += " ORDER BY favorited_at DESC, updated_at DESC LIMIT ? OFFSET ?"
        with self.connect() as conn:
            return [_video_dict(r) for r in conn.execute(sql, (*params, limit, offset))]

    def videos_to_process(self, retry_errors: bool = False, limit: int | None = None) -> list[dict]:
        statuses = ("pending", "error", "unavailable") if retry_errors else ("pending",)
        sql = f"SELECT * FROM videos WHERE status IN ({', '.join('?' * len(statuses))}) ORDER BY favorited_at DESC"
        params: list = list(statuses)
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connect() as conn:
            return [_video_dict(r) for r in conn.execute(sql, params)]

    def reset_all(self) -> int:
        """Remet toutes les vidéos en attente (pour tout réindexer après une mise à jour)."""
        with self._write_lock, self.connect() as conn:
            return conn.execute(
                "UPDATE videos SET status = 'pending', error = '', attempts = 0, media_pending = 0").rowcount

    def videos_media_pending(self, limit: int | None = None) -> list[dict]:
        sql = "SELECT * FROM videos WHERE status = 'done' AND media_pending = 1 ORDER BY favorited_at DESC"
        with self.connect() as conn:
            return [_video_dict(r) for r in conn.execute(sql + (" LIMIT ?" if limit else ""),
                                                         (limit,) if limit else ())]

    def mark_failure(self, video_id: str, error: str, permanent: bool = False) -> None:
        with self._write_lock, self.connect() as conn:
            conn.execute(
                "UPDATE videos SET status = ?, error = ?, attempts = attempts + 1, updated_at = ? WHERE id = ?",
                ("unavailable" if permanent else "error", error[:500], time.time(), video_id),
            )

    def stats(self) -> dict:
        with self.connect() as conn:
            counts = {r["status"]: r["n"] for r in
                      conn.execute("SELECT status, COUNT(*) AS n FROM videos GROUP BY status")}
            kinds = {r["kind"]: r["n"] for r in
                     conn.execute("SELECT kind, COUNT(*) AS n FROM videos GROUP BY kind")}
            with_transcript = conn.execute("SELECT COUNT(*) FROM videos WHERE transcript != ''").fetchone()[0]
            analysed = conn.execute("SELECT COUNT(DISTINCT video_id) FROM analyses").fetchone()[0]
            media_pending = conn.execute(
                "SELECT COUNT(*) FROM videos WHERE status = 'done' AND media_pending = 1").fetchone()[0]
        return {
            "total": sum(counts.values()),
            "pending": counts.get("pending", 0),
            "done": counts.get("done", 0),
            "error": counts.get("error", 0),
            "unavailable": counts.get("unavailable", 0),
            "videos": kinds.get("video", 0),
            "carousels": kinds.get("photo", 0),
            "with_transcript": with_transcript,
            "analysed": analysed,
            "media_pending": media_pending,
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
        with self._write_lock, self.connect() as conn:
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

    def kv_delete(self, *keys: str) -> None:
        with self.connect() as conn:
            conn.executemany("DELETE FROM kv WHERE key = ?", [(k,) for k in keys])

    def all_ids(self) -> set[str]:
        with self.connect() as conn:
            return {r[0] for r in conn.execute("SELECT id FROM videos")}

    def ids_with_status(self, ids: list[str], status: str) -> set[str]:
        if not ids:
            return set()
        with self.connect() as conn:
            return {r[0] for r in conn.execute(
                f"SELECT id FROM videos WHERE status = ? AND id IN ({','.join('?' * len(ids))})", (status, *ids))}

    # --- catégories -------------------------------------------------------------

    def upsert_category(self, name: str, source: str, external_id: str, set_name: str = "",
                        keywords: list[str] | None = None, position: int = 0) -> int:
        with self._write_lock, self.connect() as conn:
            conn.execute(
                "INSERT INTO categories (name, source, external_id, set_name, keywords, position) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (source, external_id) DO UPDATE SET name = excluded.name, set_name = excluded.set_name, "
                "keywords = excluded.keywords, position = excluded.position",
                (name, source, external_id, set_name, json.dumps(keywords or [], ensure_ascii=False), position))
            return conn.execute("SELECT id FROM categories WHERE source = ? AND external_id = ?",
                                (source, external_id)).fetchone()[0]

    def set_category_members(self, category_id: int, members: list[tuple[str, float | None]]) -> None:
        """Remplace les vidéos d'une catégorie par ``[(video_id, score)]``."""
        with self._write_lock, self.connect() as conn:
            conn.execute("DELETE FROM video_categories WHERE category_id = ?", (category_id,))
            conn.executemany("INSERT OR REPLACE INTO video_categories (video_id, category_id, score) VALUES (?, ?, ?)",
                             [(vid, category_id, score) for vid, score in members])

    def delete_categories(self, source: str, keep_external_ids: set[str] | None = None) -> int:
        """Supprime les catégories d'une source (sauf ``keep_external_ids``) et leurs liens."""
        with self._write_lock, self.connect() as conn:
            rows = [r[0] for r in conn.execute("SELECT id, external_id FROM categories WHERE source = ?", (source,))
                    if keep_external_ids is None or r[1] not in keep_external_ids]
            for cid in rows:
                conn.execute("DELETE FROM video_categories WHERE category_id = ?", (cid,))
                conn.execute("DELETE FROM categories WHERE id = ?", (cid,))
            return len(rows)

    def list_categories(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT c.*, (SELECT COUNT(*) FROM video_categories vc JOIN videos v ON v.id = vc.video_id "
                "             WHERE vc.category_id = c.id AND v.status = 'done') AS count "
                "FROM categories c ORDER BY c.source DESC, c.position, c.name COLLATE NOCASE").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["keywords"] = json.loads(d.get("keywords") or "[]")
            out.append(d)
        return out

    def categories_for(self, ids: list[str]) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {i: [] for i in ids}
        if not ids:
            return out
        with self.connect() as conn:
            for r in conn.execute(
                    f"SELECT vc.video_id, c.id, c.name, c.source FROM video_categories vc "
                    f"JOIN categories c ON c.id = vc.category_id WHERE vc.video_id IN ({','.join('?' * len(ids))}) "
                    f"ORDER BY c.source DESC, c.position", ids):
                out[r["video_id"]].append({"id": r["id"], "name": r["name"], "source": r["source"]})
        return out

    # --- cache des données de la liste des favoris ----------------------------

    def cache_put(self, rows: list[tuple[str, str]], fetched_at: float) -> None:
        with self._write_lock, self.connect() as conn:
            conn.executemany("INSERT OR REPLACE INTO item_cache (video_id, data, fetched_at) VALUES (?, ?, ?)",
                             [(vid, data, fetched_at) for vid, data in rows])

    def cache_take(self, video_id: str, max_age: float) -> dict | None:
        """Renvoie (et supprime) les données en cache si elles sont assez récentes."""
        with self._write_lock, self.connect() as conn:
            row = conn.execute("SELECT data, fetched_at FROM item_cache WHERE video_id = ?", (video_id,)).fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM item_cache WHERE video_id = ?", (video_id,))
        if time.time() - (row["fetched_at"] or 0) > max_age:
            return None
        try:
            data = json.loads(row["data"])
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def cache_count(self) -> int:
        with self.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM item_cache").fetchone()[0]


def _video_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d.pop("_rowid", None)
    try:
        d["images"] = json.loads(d.get("images") or "[]")
    except json.JSONDecodeError:
        d["images"] = []
    return d
