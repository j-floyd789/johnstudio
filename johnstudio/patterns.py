"""Patterns — durable, confidence-scored lessons from arc iterations.

Today, lessons across arc iters live only in hand-maintained
``PRIOR_ITERATIONS.md`` files inside specific tasks. This module gives us
a generic store: when an arc iter ends, we summarize what worked /
didn't into a :class:`Pattern` row with a confidence score. New arc
planners auto-retrieve top-K relevant patterns via vector similarity.

Stack:
    * sqlite rows in the projects DB (``patterns`` table, see
      :mod:`johnstudio.db`).
    * each row mirrored into :class:`johnstudio.vector_store.VectorStore`
      under namespace ``patterns`` for cosine retrieval.
    * a hook-bus subscriber (``arc.iter_complete``) drives the
      :func:`summarize_arc_iter` flow automatically; the subscribe is
      wrapped in ``try/except ImportError`` so the import is safe even
      when the hooks module is absent.

All embedding goes through :mod:`johnstudio.embed`, which is local
Ollama — no paid APIs.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from . import db

# Import guarded so the DB-backed half of the store (record/get/list_all/
# prune/boost/demote) keeps working even when the vector layer or its
# Ollama backend is unavailable; find_similar then degrades to a DB scan.
try:
    from . import vector_store  # type: ignore
except Exception:  # pragma: no cover - vector_store may be absent
    vector_store = None  # type: ignore

NAMESPACE = "patterns"
REF_KIND = "pattern"

# ---------------------------------------------------------------------------
# Canonical worker-artifact convention (shared across the orchestrator).
#
# Every worker on a task writes its structured output into the task's
# SHARED artifacts directory under a single, predictable filename so that
# sibling workers, the inspector and the synthesizer can all find it
# regardless of which git branch/worktree produced it. Agents historically
# named these arbitrarily (angle_N.json / candidate_N.json / result_N.json);
# this constant is the one true pattern everything must agree on.
#
#   shared dir:  <repo>/.johnstudio/tasks/task-<NNNN>/shared_artifacts/
#   filename:    candidate_<n>.json   (n = 1-based worker index)
#
# `n` is the worker's 1-based index within the task fan-out. When an index
# is unavailable, callers fall back to a slug of the worker name so the
# filename stays unique per worker.
ARTIFACT_FILENAME_TEMPLATE = "candidate_{n}.json"
SHARED_ARTIFACTS_DIRNAME = "shared_artifacts"

# Documented top-level shape every artifact should carry. Not strictly
# enforced (no JSON schema is wired in yet), but readers can rely on these
# keys being present / optional as noted.
ARTIFACT_TOP_LEVEL_KEYS = (
    "worker",     # str  — name of the worker that produced this artifact
    "summary",    # str  — one-line summary of the candidate / result
    "result",     # any  — the structured payload (role-specific)
)


def artifact_filename(n: int | str) -> str:
    """Return the canonical artifact filename for worker index/slug ``n``.

    ``candidate_<n>.json`` — see :data:`ARTIFACT_FILENAME_TEMPLATE`.
    """
    return ARTIFACT_FILENAME_TEMPLATE.format(n=n)

CONFIDENCE_MIN = 0.0
CONFIDENCE_MAX = 0.99
DEFAULT_CONFIDENCE = 0.7
_MAX_PATTERN_LINES = 1  # per-pattern body lines kept in render (head only)
DEFAULT_BOOST = 0.1
DEFAULT_DEMOTE = 0.2


@dataclass
class Pattern:
    id: int
    project_id: int
    kind: str
    text: str
    confidence: float = DEFAULT_CONFIDENCE
    tags: list[str] = field(default_factory=list)
    evidence_artifact_ids: list[int] = field(default_factory=list)
    source_task_number: int | None = None
    created_at: str = ""
    updated_at: str = ""
    score: float = 0.0  # cosine similarity, populated by find_similar

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "kind": self.kind,
            "text": self.text,
            "confidence": self.confidence,
            "tags": list(self.tags),
            "evidence_artifact_ids": list(self.evidence_artifact_ids),
            "source_task_number": self.source_task_number,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "score": self.score,
        }


def _clamp_confidence(v: float) -> float:
    if v < CONFIDENCE_MIN:
        return CONFIDENCE_MIN
    if v > CONFIDENCE_MAX:
        return CONFIDENCE_MAX
    return float(v)


def _row_to_pattern(row, score: float = 0.0) -> Pattern:
    return Pattern(
        id=int(row["id"]),
        project_id=int(row["project_id"]),
        kind=row["kind"],
        text=row["text"],
        confidence=float(row["confidence"]),
        tags=list(json.loads(row["tags_json"] or "[]")),
        evidence_artifact_ids=list(json.loads(row["evidence_artifact_ids_json"] or "[]")),
        source_task_number=row["source_task_number"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        score=float(score),
    )


class Patterns:
    """Per-project pattern store backed by sqlite + an optional VectorStore."""

    def __init__(self, project_id: int, *, store=None):
        self.project_id = int(project_id)
        self._owned_store = False
        if store is not None:
            self._store = store
        elif vector_store is not None:
            # VectorStore defaults its db_path to <JOHNSTUDIO_HOME>/vectors.sqlite;
            # the patterns namespace partitions per-project rows logically, so
            # the store itself is shared (one file), not per-project.
            try:
                self._store = vector_store.VectorStore()
                self._owned_store = True
            except Exception:
                self._store = None
        else:
            self._store = None

    # -- context manager (closes an owned store) --------------------------

    def __enter__(self) -> "Patterns":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._owned_store and self._store is not None:
            try:
                self._store.close()
            except Exception:
                pass

    # -- write ------------------------------------------------------------

    def record(
        self,
        *,
        kind: str,
        text: str,
        confidence: float = DEFAULT_CONFIDENCE,
        tags: Iterable[str] | None = None,
        evidence_artifact_ids: Iterable[int] | None = None,
        source_task_number: int | None = None,
    ) -> int:
        """Insert a pattern, mirror it into the vector store, return its id."""
        if not text or not text.strip():
            raise ValueError("pattern text is required")
        conf = _clamp_confidence(float(confidence))
        tag_list = list(tags or [])
        ev_ids = list(evidence_artifact_ids or [])

        conn = db.connect()
        try:
            db.init_schema(conn)
            cur = conn.execute(
                """
                INSERT INTO patterns
                    (project_id, kind, text, confidence, tags_json,
                     evidence_artifact_ids_json, source_task_number)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.project_id,
                    kind,
                    text,
                    conf,
                    json.dumps(tag_list),
                    json.dumps(ev_ids),
                    source_task_number,
                ),
            )
            pattern_id = int(cur.lastrowid)
            conn.commit()
        finally:
            conn.close()

        if self._store is not None:
            try:
                self._store.upsert(NAMESPACE, REF_KIND, str(pattern_id), text)
            except Exception:
                # embed.OllamaUnavailable or any store error must not block
                # the durable DB write — vector mirroring is best-effort.
                pass
        return pattern_id

    # -- read -------------------------------------------------------------

    def get(self, pattern_id: int) -> Pattern | None:
        conn = db.connect()
        try:
            db.init_schema(conn)
            row = conn.execute(
                "SELECT * FROM patterns WHERE id = ? AND project_id = ?",
                (int(pattern_id), self.project_id),
            ).fetchone()
        finally:
            conn.close()
        return _row_to_pattern(row) if row is not None else None

    def list_all(self) -> list[Pattern]:
        conn = db.connect()
        try:
            db.init_schema(conn)
            rows = conn.execute(
                "SELECT * FROM patterns WHERE project_id = ? ORDER BY id ASC",
                (self.project_id,),
            ).fetchall()
        finally:
            conn.close()
        return [_row_to_pattern(r) for r in rows]

    def find_similar(
        self, query: str, top_k: int = 5, *, min_confidence: float = 0.0
    ) -> list[Pattern]:
        """Return up to ``top_k`` patterns most similar to ``query``.

        Uses the vector store when available; otherwise falls back to a
        confidence-ordered DB scan (no semantic ranking).
        """
        if not query or not query.strip():
            return []
        top_k = max(1, int(top_k))

        if self._store is not None:
            try:
                # vector_store.search returns (ref_id, score, text) triples.
                hits = self._store.search(NAMESPACE, query, k=top_k)
            except Exception:
                hits = []
            id_scores: list[tuple[int, float]] = []
            for hit in hits:
                ref_id, score = hit[0], hit[1]
                try:
                    id_scores.append((int(ref_id), float(score)))
                except (TypeError, ValueError):
                    continue
            if id_scores:
                ids = [i for i, _ in id_scores]
                placeholders = ", ".join("?" for _ in ids)
                conn = db.connect()
                try:
                    db.init_schema(conn)
                    rows = conn.execute(
                        "SELECT * FROM patterns WHERE project_id = ? AND id IN ("
                        + placeholders
                        + ")",
                        [self.project_id, *ids],
                    ).fetchall()
                finally:
                    conn.close()
                by_id = {int(r["id"]): r for r in rows}
                score_by_id = dict(id_scores)
                out = [
                    _row_to_pattern(by_id[i], score_by_id.get(i, 0.0))
                    for i in ids
                    if i in by_id
                ]
                out = [p for p in out if p.confidence >= min_confidence]
                out.sort(key=lambda p: p.score, reverse=True)
                return out[:top_k]

        # Fallback: confidence-ordered scan.
        results = [p for p in self.list_all() if p.confidence >= min_confidence]
        results.sort(key=lambda p: p.confidence, reverse=True)
        return results[:top_k]

    # -- confidence adjustment -------------------------------------------

    def _adjust(self, pattern_id: int, signed_delta: float) -> float:
        conn = db.connect()
        try:
            db.init_schema(conn)
            row = conn.execute(
                "SELECT confidence FROM patterns WHERE id = ? AND project_id = ?",
                (int(pattern_id), self.project_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"pattern not found: {pattern_id}")
            new_conf = _clamp_confidence(float(row["confidence"]) + signed_delta)
            conn.execute(
                "UPDATE patterns SET confidence = ?,   updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND project_id = ?",
                (new_conf, int(pattern_id), self.project_id),
            )
            conn.commit()
        finally:
            conn.close()
        return new_conf

    def boost(self, pattern_id: int, delta: float = DEFAULT_BOOST) -> float:
        """Raise confidence by ``delta``, clamped to ``CONFIDENCE_MAX``.

        Returns the new confidence value. Raises ``KeyError`` if missing.
        """
        return self._adjust(pattern_id, float(delta))

    def demote(self, pattern_id: int, delta: float = DEFAULT_DEMOTE) -> float:
        """Lower confidence by ``delta``, clamped to ``CONFIDENCE_MIN``.

        Returns the new confidence value. Raises ``KeyError`` if missing.
        """
        return self._adjust(pattern_id, -float(delta))

    def prune(self, min_confidence: float) -> int:
        """Delete patterns whose confidence dropped below ``min_confidence``.

        Removes their vector-store mirrors too. Returns the count removed.
        """
        threshold = float(min_confidence)
        conn = db.connect()
        try:
            db.init_schema(conn)
            rows = conn.execute(
                "SELECT id FROM patterns WHERE project_id = ? AND confidence < ?",
                (self.project_id, threshold),
            ).fetchall()
            ids = [int(r["id"]) for r in rows]
            if ids:
                placeholders = ", ".join("?" for _ in ids)
                conn.execute(
                    "DELETE FROM patterns WHERE id IN (" + placeholders + ")",
                    ids,
                )
                conn.commit()
        finally:
            conn.close()

        if self._store is not None and ids:
            # VectorStore has no public delete; remove the mirrored rows via
            # its connection (the disassembly used exactly this statement).
            for pid in ids:
                try:
                    self._store._db.execute(
                        "DELETE FROM vectors WHERE namespace = ? AND ref_kind = ? AND ref_id = ?",
                        (NAMESPACE, REF_KIND, str(pid)),
                    )
                except Exception:
                    pass
            try:
                self._store._db.commit()
            except Exception:
                pass
        return len(ids)


# ---------------------------------------------------------------------------
# Arc-iteration summarization
# ---------------------------------------------------------------------------


def _read_text_safe(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _find_task_dir(task_number: int) -> Path | None:
    """Locate `.johnstudio/tasks/task-NNNN` walking up from cwd.

    RECONSTRUCTED: the exact search roots are inferred (cwd + parents);
    returns the first existing match or None.
    """
    name = f"task-{task_number:04d}"
    cwd = Path.cwd()
    candidates: list[Path] = []
    for base in [cwd, *cwd.parents]:
        candidates.append(base / ".johnstudio" / "tasks" / name)
    for c in candidates:
        if c.is_dir():
            return c
    return None


def _load_iter_artifacts(project_id: int, task_number: int) -> list[dict]:
    """Load JSON payloads of artifacts registered for this iteration.

    Best-effort: missing/invalid artifacts are skipped.
    """
    out: list[dict] = []
    try:
        from . import artifacts as artifacts_mod

        manifests = artifacts_mod.Manifests(project_id).find(task_number=task_number)
    except Exception:
        return out
    for m in manifests:
        try:
            raw = _read_text_safe(Path(m.path))
            payload = json.loads(raw)
            if isinstance(payload, dict):
                out.append(payload)
        except (OSError, json.JSONDecodeError):
            continue
    return out


def summarize_arc_iter(project_id: int, task_number: int) -> int | None:
    """Summarize one completed arc iteration into a recorded pattern.

    Reads the task's DONE.md/result artifacts, derives a one-line lesson
    plus a confidence, and records it. Returns the new pattern id, or
    None if there was nothing worth recording.

    RECONSTRUCTED: the precise heuristic mapping artifacts → lesson text /
    confidence could not be fully recovered from bytecode. This preserves
    the recovered shape (gather DONE.md + artifact payloads, record a
    pattern) with a conservative summary; tune the lesson extraction to
    match prior behavior if a reference output is found.
    """
    task_dir = _find_task_dir(task_number)
    done_text = ""
    evidence_ids: list[int] = []
    if task_dir is not None:
        done_text = _read_text_safe(task_dir / "DONE.md")

    try:
        from . import artifacts as artifacts_mod

        for m in artifacts_mod.Manifests(project_id).find(task_number=task_number):
            try:
                evidence_ids.append(int(m.id))
            except (TypeError, ValueError):
                continue
    except Exception:
        pass

    payloads = _load_iter_artifacts(project_id, task_number)

    summary_source = done_text.strip()
    if not summary_source:
        for p in payloads:
            s = p.get("summary") or p.get("result") or ""
            if isinstance(s, str) and s.strip():
                summary_source = s.strip()
                break
    if not summary_source:
        return None

    head = next((ln for ln in summary_source.splitlines() if ln.strip()), "").strip()
    if not head:
        return None

    # Confidence proxy: more corroborating evidence -> slightly higher.
    confidence = _clamp_confidence(DEFAULT_CONFIDENCE + 0.05 * min(len(evidence_ids), 4))

    return Patterns(project_id).record(
        kind="arc_iter",
        text=head,
        confidence=confidence,
        tags=["arc"],
        evidence_artifact_ids=evidence_ids,
        source_task_number=task_number,
    )


def render_patterns_section(patterns: Sequence[Pattern], *, max_lines: int = 40) -> str:
    """Render patterns as the ``## Learned patterns`` block.

    Returns an empty string for an empty iterable so the caller can omit
    the section entirely. Caps output at ``max_lines`` lines.
    """
    patterns = list(patterns)
    if not patterns:
        return ""
    lines = ["## Learned patterns"]
    for p in patterns:
        if len(lines) >= max_lines:
            break
        tag_str = f" [{', '.join(p.tags)}]" if p.tags else ""
        head = next((ln for ln in p.text.strip().splitlines() if ln.strip()), "").strip()
        src = f" (task {p.source_task_number})" if p.source_task_number else ""
        lines.append(
            f"- **{p.kind}** (conf {p.confidence:.2f}){tag_str}{src}: {head}"
        )
    return "\n".join(lines[:max_lines])


def _on_arc_iter_complete(event: str, payload: dict) -> None:
    """Hook subscriber: summarize an arc iter when it completes.

    Payload contract (best-effort; missing fields skipped quietly):
        {"project_id": int, "task_number": int}
    """
    try:
        project_id = int(payload["project_id"])
        task_number = int(payload["task_number"])
    except (KeyError, TypeError, ValueError):
        return
    try:
        summarize_arc_iter(project_id, task_number)
    except Exception:
        return


try:
    from .hooks import bus, EventTypes

    bus.subscribe(EventTypes.ARC_ITER_COMPLETE, _on_arc_iter_complete)
except ImportError:
    pass
