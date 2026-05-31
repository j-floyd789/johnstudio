"""Minimal sqlite-backed vector store with brute-force cosine search.

No new pip dependencies: vectors are persisted as JSON arrays in a TEXT
column and scored in pure Python. This is intentional — for the size we
care about (under ~10k entries per namespace), a linear scan in Python
is well under one second and avoids dragging in numpy/faiss/sqlite-vss.

The default store lives in ``<JOHNSTUDIO_HOME>/vectors.sqlite``, the same
home directory as the embed cache so a single env override
(``JOHNSTUDIO_HOME``) relocates everything for tests.

Embeddings are obtained from :mod:`johnstudio.embed`, which is a thin
client over the local Ollama daemon. The vector store deliberately does
NOT know about any paid provider.
"""
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import Sequence

from . import config, embed

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vectors (
    id INTEGER PRIMARY KEY,
    namespace TEXT NOT NULL,
    ref_kind TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    text TEXT NOT NULL,
    vector_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_vec_ns ON vectors(namespace);
CREATE UNIQUE INDEX IF NOT EXISTS uq_vec_ns_ref ON vectors(namespace, ref_kind, ref_id);
"""


def default_db_path() -> Path:
    home = config.home_dir()
    home.mkdir(parents=True, exist_ok=True)
    return home / "vectors.sqlite"


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0 or nb == 0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


class VectorStore:
    """Sqlite + JSON vector store with cosine retrieval per namespace.

    Args:
        db_path: Optional override of the on-disk file. Defaults to
            ``<JOHNSTUDIO_HOME>/vectors.sqlite``.
    """

    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path else default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.db_path))
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        try:
            self._db.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "VectorStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def upsert(self, namespace: str, ref_kind: str, ref_id: str, text: str) -> int:
        """Embed ``text`` and store/update the row for (namespace, ref_kind, ref_id).

        Returns the row id. Raises :class:`embed.OllamaUnavailable` if the
        local daemon is not running.
        """
        if not (namespace and ref_kind and ref_id):
            raise ValueError("namespace, ref_kind and ref_id are required")
        vec = embed.embed(text)
        vec_json = json.dumps(vec)
        self._db.execute(
            "INSERT INTO vectors (namespace, ref_kind, ref_id, text, vector_json) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(namespace, ref_kind, ref_id) DO UPDATE SET "
            "  text = excluded.text, "
            "  vector_json = excluded.vector_json, "
            "  created_at = CURRENT_TIMESTAMP",
            (namespace, ref_kind, ref_id, text, vec_json),
        )
        self._db.commit()
        row = self._db.execute(
            "SELECT id FROM vectors WHERE namespace = ? AND ref_kind = ? AND ref_id = ?",
            (namespace, ref_kind, ref_id),
        ).fetchone()
        return int(row[0])

    def count(self, namespace: str | None = None) -> int:
        if namespace is None:
            row = self._db.execute("SELECT COUNT(*) FROM vectors").fetchone()
        else:
            row = self._db.execute(
                "SELECT COUNT(*) FROM vectors WHERE namespace = ?",
                (namespace,),
            ).fetchone()
        return int(row[0])

    def search(
        self, namespace: str, query: str, k: int = 5
    ) -> list[tuple[str, float, str]]:
        """Return the top-k cosine matches in ``namespace`` for ``query``.

        Each result is ``(ref_id, score, text)``. An empty namespace
        returns an empty list rather than raising — callers that compose
        retrieval into a prompt prefer that to a hard failure.
        """
        if k <= 0:
            return []
        rows = self._db.execute(
            "SELECT ref_id, text, vector_json FROM vectors WHERE namespace = ?",
            (namespace,),
        ).fetchall()
        if not rows:
            return []
        q_vec = embed.embed(query)
        scored: list[tuple[str, float, str]] = []
        for ref_id, text, vec_json in rows:
            vec = json.loads(vec_json)
            score = _cosine(q_vec, vec)
            scored.append((ref_id, score, text))
        scored.sort(key=lambda t: t[1], reverse=True)
        return scored[:k]
