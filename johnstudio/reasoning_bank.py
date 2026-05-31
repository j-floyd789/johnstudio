"""ReasoningBank — durable memory of past task trajectories.

Each row stores (project_id, task_number, goal, outcome, approach_summary,
tags) and is mirrored into the vector store under namespace
``reasoning_bank`` so a new task's planner can semantically retrieve
priors like "this approach tried 8 times, all null".

The bank lives in ``<JOHNSTUDIO_HOME>/reasoning_bank.sqlite``. It is its
own file (not the projects DB) so it can be wiped or rebuilt without
touching the project registry.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from . import config, vector_store

NAMESPACE = "reasoning_bank"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reasoning_bank (
    task_number INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    goal TEXT NOT NULL,
    outcome TEXT NOT NULL,
    approach_summary TEXT NOT NULL,
    tags_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_rb_project ON reasoning_bank(project_id);
"""


def default_db_path() -> Path:
    home = config.home_dir()
    home.mkdir(parents=True, exist_ok=True)
    return home / "reasoning_bank.sqlite"


@dataclass(frozen=True)
class Prior:
    """A retrieved prior task — what the planner sees."""

    task_number: int
    project_id: int
    goal: str
    outcome: str
    approach_summary: str
    tags: list[str]
    score: float


def _embed_text(goal: str, outcome: str, approach_summary: str) -> str:
    """The canonical text we embed for retrieval.

    We deliberately bias toward the goal (it's what new tasks query on)
    by listing it first, then the outcome label, then the approach.
    """
    return (
        f"Goal: {goal.strip()}\n"
        f"Outcome: {outcome.strip()}\n"
        f"Approach: {approach_summary.strip()}"
    )


class ReasoningBank:
    """A project-scoped store of past task trajectories with semantic recall.

    Args:
        project_id: Scope used as the default for ``find_priors``. Passing
            ``None`` is allowed for callers that want to write multiple
            projects' rows; in that case every write must specify
            ``project_id`` explicitly.
        db_path: Optional sqlite path override.
        store: Optional pre-built :class:`VectorStore` (useful in tests).
    """

    def __init__(
        self,
        project_id: int | None = None,
        *,
        db_path: Path | str | None = None,
        store: vector_store.VectorStore | None = None,
    ):
        self.project_id = project_id
        self.db_path = Path(db_path) if db_path else default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.db_path))
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self._owned_store = store is None
        self._store = store or vector_store.VectorStore()

    def close(self) -> None:
        try:
            self._db.close()
        except sqlite3.Error:
            pass
        if self._owned_store:
            self._store.close()

    def __enter__(self) -> "ReasoningBank":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ---- write -----------------------------------------------------------

    def record_task(
        self,
        *,
        task_number: int,
        goal: str,
        outcome: str,
        approach_summary: str,
        tags: Sequence[str] | None = None,
        project_id: int | None = None,
    ) -> int:
        """Persist a task row AND embed it into the vector store.

        Idempotent on ``task_number`` (UPSERT). Returns ``task_number``.
        Raises :class:`embed.OllamaUnavailable` when Ollama is unreachable —
        by policy, NO paid-API fallback.
        """
        pid = project_id if project_id is not None else self.project_id
        if pid is None:
            raise ValueError("project_id required (set on ctor or pass explicitly)")
        if not goal.strip():
            raise ValueError("goal must be non-empty")
        if not outcome.strip():
            raise ValueError("outcome must be non-empty")
        if not approach_summary.strip():
            raise ValueError("approach_summary must be non-empty")
        tag_list = list(tags or [])
        self._db.execute(
            "INSERT INTO reasoning_bank "
            "  (task_number, project_id, goal, outcome, approach_summary, tags_json) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(task_number) DO UPDATE SET "
            "  project_id = excluded.project_id, "
            "  goal = excluded.goal, "
            "  outcome = excluded.outcome, "
            "  approach_summary = excluded.approach_summary, "
            "  tags_json = excluded.tags_json, "
            "  created_at = CURRENT_TIMESTAMP",
            (task_number, pid, goal, outcome, approach_summary, json.dumps(tag_list)),
        )
        self._db.commit()
        self._store.upsert(
            NAMESPACE,
            "task",
            str(task_number),
            _embed_text(goal, outcome, approach_summary),
        )
        return task_number

    # ---- read ------------------------------------------------------------

    def get(self, task_number: int) -> Prior | None:
        row = self._db.execute(
            "SELECT task_number, project_id, goal, outcome, approach_summary, tags_json "
            "FROM reasoning_bank WHERE task_number = ?",
            (task_number,),
        ).fetchone()
        if row is None:
            return None
        return Prior(
            task_number=int(row[0]),
            project_id=int(row[1]),
            goal=row[2],
            outcome=row[3],
            approach_summary=row[4],
            tags=list(json.loads(row[5] or "[]")),
            score=1.0,
        )

    def list_all(self, project_id: int | None = None) -> list[Prior]:
        if project_id is None:
            rows = self._db.execute(
                "SELECT task_number, project_id, goal, outcome, approach_summary, tags_json "
                "FROM reasoning_bank ORDER BY task_number"
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT task_number, project_id, goal, outcome, approach_summary, tags_json "
                "FROM reasoning_bank WHERE project_id = ? ORDER BY task_number",
                (project_id,),
            ).fetchall()
        out: list[Prior] = []
        for r in rows:
            out.append(Prior(
                task_number=int(r[0]),
                project_id=int(r[1]),
                goal=r[2],
                outcome=r[3],
                approach_summary=r[4],
                tags=list(json.loads(r[5] or "[]")),
                score=1.0,
            ))
        return out

    def find_priors(
        self,
        goal: str,
        k: int = 5,
        *,
        project_id: int | None = None,
    ) -> list[Prior]:
        """Return up to ``k`` semantically nearest priors.

        Optionally filter by ``project_id`` (defaults to ``self.project_id``;
        when both are ``None``, all projects are searched).
        """
        if not goal or not goal.strip():
            return []
        pid = project_id if project_id is not None else self.project_id
        # Over-fetch then filter — the namespace is small (<10k rows).
        raw = self._store.search(NAMESPACE, goal, k=max(k * 4, k))
        out: list[Prior] = []
        for ref_id, score, _text in raw:
            try:
                tn = int(ref_id)
            except ValueError:
                continue
            row = self._db.execute(
                "SELECT task_number, project_id, goal, outcome, approach_summary, tags_json "
                "FROM reasoning_bank WHERE task_number = ?",
                (tn,),
            ).fetchone()
            if row is None:
                continue
            if pid is not None and int(row[1]) != pid:
                continue
            out.append(Prior(
                task_number=int(row[0]),
                project_id=int(row[1]),
                goal=row[2],
                outcome=row[3],
                approach_summary=row[4],
                tags=list(json.loads(row[5] or "[]")),
                score=float(score),
            ))
            if len(out) >= k:
                break
        return out


# ---------------------------------------------------------------------------
# Prompt rendering — used by the planner injection
# ---------------------------------------------------------------------------

_MAX_PRIOR_LINES = 50


def render_priors_section(priors: Iterable[Prior], *, max_lines: int = _MAX_PRIOR_LINES) -> str:
    """Render priors as the ``## Prior similar tasks`` block.

    Returns an empty string for an empty iterable so the caller can omit
    the section entirely. Caps the output at ``max_lines`` lines total
    (heading included) to keep planner-prompt budget bounded.
    """
    priors = list(priors)
    if not priors:
        return ""
    lines: list[str] = ["## Prior similar tasks", ""]
    for p in priors:
        if len(lines) >= max_lines:
            break
        tag_str = f" [{', '.join(p.tags)}]" if p.tags else ""
        lines.append(
            f"- **task-{p.task_number:04d}** (score {p.score:.2f}, outcome: "
            f"{p.outcome}){tag_str}: {p.goal.strip()}"
        )
        summary = p.approach_summary.strip().splitlines()[0] if p.approach_summary else ""
        if summary and len(lines) < max_lines:
            lines.append(f"  - approach: {summary[:240]}")
    # Trim to bound.
    return "\n".join(lines[:max_lines])
