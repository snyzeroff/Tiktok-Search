"""Stockage SQLite + index plein texte FTS5 (insensible aux accents et à la casse)."""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# Colonnes indexées, dans l'ordre de la table FTS (l'index sert à snippet()).
FTS_COLUMNS = ["description", "hashtags", "transcript", "ocr_text", "author"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id            TEXT PRIMARY KEY,
    url           TEXT NOT NULL,
    author        TEXT DEFAULT '',
    description   TEXT DEFAULT '',
    hashtags      TEXT DEFAULT '',
    transcript    TEXT DEFAULT '',
    ocr_text      TEXT DEFAULT '',
    thumbnail     TEXT DEFAULT '',
    duration      REAL,
    favorited_at  TEXT DEFAULT '',
    source        TEXT DEFAULT '',
    status        TEXT DEFAULT 'pending',   -- pending | done | error
    error         TEXT DEFAULT '',
    updated_at    REAL
);

CREATE VIRTUAL TABLE IF NOT EXISTS videos_fts USING fts5(
    id UNINDEXED,
    description, hashtags, transcript, ocr_text, author,
    tokenize = "unicode61 remove_diacritics 2"
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- vidéos -----------------------------------------------------------

    def add_favorite(self, video_id: str, url: str, favorited_at: str = "", source: str = "") -> bool:
        """Ajoute une vidéo à traiter. Retourne False si elle existait déjà."""
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO videos (id, url, favorited_at, source, updated_at) VALUES (?, ?, ?, ?, ?)",
                (video_id, url, favorited_at, source, time.time()),
            )
            return cur.rowcount > 0

    def save_video(self, video_id: str, **fields) -> None:
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.connect() as conn:
            conn.execute(f"UPDATE videos SET {cols} WHERE id = ?", (*fields.values(), video_id))
            row = conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
            conn.execute("DELETE FROM videos_fts WHERE id = ?", (video_id,))
            if row is not None:
                conn.execute(
                    f"INSERT INTO videos_fts (id, {', '.join(FTS_COLUMNS)}) VALUES (?{', ?' * len(FTS_COLUMNS)})",
                    (video_id, *(row[c] or "" for c in FTS_COLUMNS)),
                )

    def get_video(self, video_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
            return dict(row) if row else None

    def videos_to_process(self, retry_errors: bool = False, limit: int | None = None) -> list[dict]:
        statuses = ("pending", "error") if retry_errors else ("pending",)
        sql = f"SELECT * FROM videos WHERE status IN ({', '.join('?' * len(statuses))}) ORDER BY favorited_at DESC"
        params: list = list(statuses)
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, params)]

    def stats(self) -> dict:
        with self.connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM videos GROUP BY status").fetchall()
            counts = {r["status"]: r["n"] for r in rows}
            with_transcript = conn.execute(
                "SELECT COUNT(*) FROM videos WHERE transcript != ''"
            ).fetchone()[0]
        return {
            "total": sum(counts.values()),
            "pending": counts.get("pending", 0),
            "done": counts.get("done", 0),
            "error": counts.get("error", 0),
            "with_transcript": with_transcript,
        }

    # --- clé/valeur (jetons OAuth, etc.) ------------------------------------

    def kv_get(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None

    def kv_set(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value))
