"""Artifact manifest store.

Agents (especially parallel workers in team mode) dump JSON outputs into
worktree ``artifacts/`` directories. Downstream agents that need to read
those outputs have, until now, polled the filesystem for expected
filenames — which causes duplicate JSONs, repeated re-reads, and a
hand-built verdict ledger per task.

This module gives agents a manifest layer: register an artifact with
``{kind, path, sha256, tags}`` and downstream consumers refer to it by
integer ID via :meth:`Manifests.get` / :meth:`Manifests.find`.

Schema lives in :mod:`johnstudio.db` (tables ``artifacts`` and
``artifact_tags``). ``UNIQUE(project_id, sha256)`` dedupes
re-registrations of the same bytes; :meth:`Manifests.register` returns
the existing id on a re-register so callers can be idempotent.

Stdlib only.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from . import db

_CHUNK = 65536


@dataclass
class Manifest:
    id: int
    project_id: int
    task_number: int | None
    kind: str
    path: str
    sha256: str
    size_bytes: int | None
    agent: str | None
    created_at: str
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "task_number": self.task_number,
            "kind": self.kind,
            "path": self.path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "agent": self.agent,
            "created_at": self.created_at,
            "tags": list(self.tags),
        }


def _sha256_file(path: Path) -> tuple[str, int]:
    """Stream the file through sha256. Returns (hex digest, size in bytes)."""
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


class Manifests:
    """Per-project artifact manifest store.

    Construct once per project (``Manifests(project_id=2)``) and call
    :meth:`register`, :meth:`get`, :meth:`find`. Connection is created
    on demand from :func:`johnstudio.db.connect`.
    """

    def __init__(self, project_id: int):
        self.project_id = int(project_id)

    def register(
        self,
        *,
        task_number: int | None = None,
        kind: str,
        path: str | Path,
        tags: Iterable[str] | None = None,
        agent: str | None = None,
    ) -> int:
        """Register an artifact. Returns its id.

        Computes sha256 of the file at ``path``. If an artifact with the
        same (project_id, sha256) already exists, returns its id
        unchanged — tags from this call are still unioned in so
        re-registers can enrich the tag set.
        """
        if not kind:
            raise ValueError("kind is required")
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"artifact path not a file: {path}")
        digest, size = _sha256_file(p)

        conn = db.connect()
        db.init_schema(conn)
        cur = conn.cursor()
        row = cur.execute(
            "SELECT id FROM artifacts WHERE project_id = ? AND sha256 = ?",
            (self.project_id, digest),
        ).fetchone()
        if row is not None:
            artifact_id = int(row["id"])
        else:
            cur.execute(
                """
                INSERT INTO artifacts
                    (project_id, task_number, kind, path, sha256, size_bytes, agent)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.project_id,
                    task_number,
                    kind,
                    str(p),
                    digest,
                    size,
                    agent,
                ),
            )
            artifact_id = int(cur.lastrowid)

        for tag in tags or []:
            cur.execute(
                "INSERT OR IGNORE INTO artifact_tags (artifact_id, tag) VALUES (?, ?)",
                (artifact_id, str(tag)),
            )
        conn.commit()
        return artifact_id

    def get(self, artifact_id: int) -> Manifest | None:
        """Fetch one manifest by id (must belong to this project)."""
        conn = db.connect()
        db.init_schema(conn)
        cur = conn.cursor()
        row = cur.execute(
            "SELECT * FROM artifacts WHERE id = ? AND project_id = ?",
            (int(artifact_id), self.project_id),
        ).fetchone()
        if row is None:
            return None
        tags = [
            r["tag"]
            for r in cur.execute(
                "SELECT tag FROM artifact_tags WHERE artifact_id = ? ORDER BY tag",
                (int(artifact_id),),
            )
        ]
        return _row_to_manifest(row, tags)

    def find(
        self,
        *,
        task_number: int | None = None,
        kind: str | None = None,
        tags: Iterable[str] | None = None,
        agent: str | None = None,
    ) -> list[Manifest]:
        """Return manifests matching the given filters.

        ``tags`` is an AND-match: an artifact must carry every requested
        tag to be returned. Filters are conjunctive. Results are ordered
        by ``id`` ascending (insertion order).
        """
        conn = db.connect()
        db.init_schema(conn)
        cur = conn.cursor()

        clauses = ["a.project_id = ?"]
        params: list = [self.project_id]
        if task_number is not None:
            clauses.append("a.task_number = ?")
            params.append(int(task_number))
        if kind is not None:
            clauses.append("a.kind = ?")
            params.append(kind)
        if agent is not None:
            clauses.append("a.agent = ?")
            params.append(agent)

        tag_list = [str(t) for t in tags] if tags else []
        if tag_list:
            placeholders = ", ".join("?" for _ in tag_list)
            clauses.append(
                """
                a.id IN (
                    SELECT artifact_id FROM artifact_tags
                    WHERE tag IN ("""
                + placeholders
                + """)
                    GROUP BY artifact_id
                    HAVING COUNT(DISTINCT tag) = ?
                )
                """
            )
            params.extend(tag_list)
            params.append(len(set(tag_list)))

        sql = "SELECT a.* FROM artifacts a WHERE " + " AND ".join(clauses) + " ORDER BY a.id ASC"
        rows = cur.execute(sql, params).fetchall()
        if not rows:
            return []

        ids = [int(r["id"]) for r in rows]
        tags_by_id: dict[int, list[str]] = {aid: [] for aid in ids}
        id_ph = ", ".join("?" for _ in ids)
        for tr in cur.execute(
            "SELECT artifact_id, tag FROM artifact_tags WHERE artifact_id IN ("
            + id_ph
            + ") ORDER BY tag",
            ids,
        ):
            tags_by_id.setdefault(int(tr["artifact_id"]), []).append(tr["tag"])

        return [_row_to_manifest(r, tags_by_id.get(int(r["id"]), [])) for r in rows]

    def list_all(self) -> list[Manifest]:
        """Return every artifact registered against this project."""
        return self.find()


def _row_to_manifest(row, tags: Iterable[str]) -> Manifest:
    return Manifest(
        id=int(row["id"]),
        project_id=int(row["project_id"]),
        task_number=row["task_number"],
        kind=row["kind"],
        path=row["path"],
        sha256=row["sha256"],
        size_bytes=row["size_bytes"],
        agent=row["agent"],
        created_at=row["created_at"],
        tags=list(tags),
    )


def _on_artifact_landed(event: str, payload: dict) -> None:
    """Hook subscriber: register an artifact when an ARTIFACT_LANDED event fires.

    Payload contract (best-effort; missing fields are skipped quietly):
        {
            "project_id": int,
            "task_number": int | None,
            "kind": str,
            "path": str,
            "tags": list[str] | None,
            "agent": str | None,
        }
    """
    try:
        project_id = int(payload["project_id"])
        kind = payload["kind"]
        path = payload["path"]
    except (KeyError, TypeError, ValueError):
        return
    try:
        Manifests(project_id).register(
            task_number=payload.get("task_number"),
            kind=kind,
            path=path,
            tags=payload.get("tags") or [],
            agent=payload.get("agent"),
        )
    except (FileNotFoundError, ValueError):
        return


try:
    from .hooks import bus, EventTypes

    bus.subscribe(EventTypes.ARTIFACT_LANDED, _on_artifact_landed)
except ImportError:
    pass
