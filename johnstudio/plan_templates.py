"""Plan-template library — reusable plan shapes keyed by task type.

When a team task SUCCEEDS, the orchestrator can record the *shape* of the
plan that worked — the set of role↔VP (worker/provider) assignments — under
a coarse "task-type" label derived from the task text. A later planner can
then ask for the closest past successful shapes and skip re-deriving a team
from scratch.

Storage mirrors the sqlite-in-``<JOHNSTUDIO_HOME>`` pattern used by
:mod:`johnstudio.reasoning_bank`: its own file
(``<JOHNSTUDIO_HOME>/plan_templates.sqlite``) so it can be wiped/rebuilt
without touching the project registry. Retrieval is keyword/label based by
default and opportunistically upgraded with embedding similarity via
:mod:`johnstudio.embed` when the local Ollama daemon is up (never a hard
dependency — embedding failures degrade silently to the keyword path).

Typical caller flow::

    from johnstudio import plan_templates as pt

    # on a successful team task:
    pt.save_plan_template(
        task_type=task_title,                 # raw text is fine; we normalise
        plan_shape={"quant-researcher": "claude_vp",
                    "backtester": "gemini_vp"},
        outcome_score=0.92,
    )

    # when planning a new task:
    for tmpl in pt.suggest_plan_templates(new_task_title, k=3):
        print(tmpl.task_type, tmpl.plan_shape, tmpl.outcome_score)

This module is intentionally standalone — it exposes importable functions the
planner/orchestrator can call later; nothing here wires itself into the team
orchestrator.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from . import config

# Embedding is optional: if Ollama is down we still work via keyword match.
try:
    from . import embed as _embed  # type: ignore
except Exception:  # pragma: no cover - embed import should normally succeed
    _embed = None  # type: ignore

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plan_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL,
    plan_shape_json TEXT NOT NULL,
    outcome_score REAL,
    use_count INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(task_type, plan_shape_json)
);
CREATE INDEX IF NOT EXISTS idx_plan_templates_type ON plan_templates(task_type);
"""

# Words that carry no signal for a task-type label.
_STOPWORDS = frozenset(
    {
        "a", "an", "the", "and", "or", "of", "to", "for", "in", "on", "with",
        "is", "are", "be", "this", "that", "task", "run", "build", "make",
        "create", "add", "implement", "fix", "use", "using", "via", "from",
        "into", "by", "at", "it", "we", "our", "new",
    }
)

_WORD_RE = re.compile(r"[a-z0-9]+")


def default_db_path() -> Path:
    home = config.home_dir()
    home.mkdir(parents=True, exist_ok=True)
    return home / "plan_templates.sqlite"


@dataclass(frozen=True)
class PlanTemplate:
    """A retrieved plan shape — what a planner sees."""

    id: int
    task_type: str
    plan_shape: dict
    outcome_score: float | None
    use_count: int
    score: float = 0.0  # retrieval relevance (keyword overlap or cosine)


def task_type_key(text: str) -> str:
    """Normalise free task text/title into a coarse, stable task-type label.

    Lowercases, strips non-alphanumerics, drops stopwords, dedupes while
    preserving order, and keeps the first few salient tokens joined by ``-``
    (e.g. ``"Backtest the Kalshi NYC weather strategy"`` -> ``"backtest-kalshi-nyc-weather-strategy"``).
    An already-clean label (no spaces) is returned lightly normalised so
    callers may pass either raw text or a pre-computed key.
    """
    if not text or not text.strip():
        return ""
    toks = [t for t in _WORD_RE.findall(text.lower()) if t not in _STOPWORDS]
    seen: list[str] = []
    for t in toks:
        if t not in seen:
            seen.append(t)
    if not seen:
        # Everything was a stopword — fall back to a slug of the raw text.
        slug = "-".join(_WORD_RE.findall(text.lower()))
        return slug[:64]
    return "-".join(seen[:6])


def _normalise_shape(plan_shape: Mapping) -> dict:
    """Canonicalise a plan shape so equal shapes hash/compare equal.

    Keys (roles) are coerced to ``str`` and the mapping is sorted by key so
    the JSON form used for the UNIQUE constraint is order-independent.
    """
    if not isinstance(plan_shape, Mapping):
        raise TypeError(
            f"plan_shape must be a mapping role->vp, got {type(plan_shape).__name__}"
        )
    return {str(k): plan_shape[k] for k in sorted(plan_shape, key=str)}


def _connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    db = sqlite3.connect(str(db_path or default_db_path()))
    db.row_factory = sqlite3.Row
    db.executescript(_SCHEMA)
    db.commit()
    return db


def save_plan_template(
    task_type: str,
    plan_shape: Mapping,
    *,
    outcome_score: float | None = None,
    db_path: Path | str | None = None,
) -> int:
    """Record a successful plan shape under a task-type label.

    ``task_type`` may be raw task text/title or a pre-computed key — it is
    normalised via :func:`task_type_key`. ``plan_shape`` is the role↔VP
    assignment map (``{role: vp_or_worker}``). Idempotent on
    ``(task_type, plan_shape)``: re-saving the same shape bumps ``use_count``
    and keeps the better (higher) ``outcome_score``. Returns the row id.
    """
    key = task_type_key(task_type)
    if not key:
        raise ValueError("task_type produced an empty key (need some text)")
    shape = _normalise_shape(plan_shape)
    if not shape:
        raise ValueError("plan_shape must be a non-empty role->vp mapping")
    shape_json = json.dumps(shape, sort_keys=True)
    score = None if outcome_score is None else float(outcome_score)

    db = _connect(db_path)
    try:
        cur = db.execute(
            "INSERT INTO plan_templates (task_type, plan_shape_json, outcome_score) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(task_type, plan_shape_json) DO UPDATE SET "
            "  use_count = use_count + 1, "
            "  outcome_score = CASE "
            "    WHEN excluded.outcome_score IS NULL THEN outcome_score "
            "    WHEN outcome_score IS NULL THEN excluded.outcome_score "
            "    ELSE MAX(outcome_score, excluded.outcome_score) END, "
            "  updated_at = CURRENT_TIMESTAMP",
            (key, shape_json, score),
        )
        db.commit()
        if cur.lastrowid:
            row_id = int(cur.lastrowid)
        else:  # UPSERT path — lastrowid may be unreliable; look it up.
            row = db.execute(
                "SELECT id FROM plan_templates WHERE task_type = ? AND plan_shape_json = ?",
                (key, shape_json),
            ).fetchone()
            row_id = int(row["id"])
    finally:
        db.close()
    return row_id


def _row_to_template(row, score: float = 0.0) -> PlanTemplate:
    return PlanTemplate(
        id=int(row["id"]),
        task_type=row["task_type"],
        plan_shape=dict(json.loads(row["plan_shape_json"])),
        outcome_score=(
            None if row["outcome_score"] is None else float(row["outcome_score"])
        ),
        use_count=int(row["use_count"]),
        score=float(score),
    )


def _keyword_score(query_key: str, cand_key: str) -> float:
    """Jaccard-ish overlap of the two hyphen-token sets in ``[0, 1]``."""
    q = set(query_key.split("-")) - {""}
    c = set(cand_key.split("-")) - {""}
    if not q or not c:
        return 0.0
    inter = len(q & c)
    if inter == 0:
        return 0.0
    return inter / len(q | c)


def _embedding_scores(query_text: str, rows: list) -> dict[int, float] | None:
    """Cosine similarity of ``query_text`` to each row's task_type, or None.

    Returns ``None`` (signalling "fall back to keyword") if embedding is
    unavailable or any embed call fails — never raises.
    """
    if _embed is None:
        return None
    try:
        qv = _embed.embed(query_text)
    except Exception:
        return None
    qnorm = sum(x * x for x in qv) ** 0.5
    if qnorm == 0:
        return None
    out: dict[int, float] = {}
    for r in rows:
        try:
            cv = _embed.embed(r["task_type"].replace("-", " "))
        except Exception:
            return None
        cnorm = sum(x * x for x in cv) ** 0.5
        if cnorm == 0:
            out[int(r["id"])] = 0.0
            continue
        dot = sum(a * b for a, b in zip(qv, cv))
        out[int(r["id"])] = dot / (qnorm * cnorm)
    return out


def suggest_plan_templates(
    task_type_or_text: str,
    *,
    k: int = 3,
    db_path: Path | str | None = None,
    use_embeddings: bool = True,
) -> list[PlanTemplate]:
    """Return up to ``k`` past successful plan shapes closest to the input.

    ``task_type_or_text`` may be a raw task title or a pre-computed key.
    Ranking: an exact task-type match always wins; otherwise we score by
    embedding cosine similarity (when ``use_embeddings`` and Ollama is up)
    and fall back to keyword (hyphen-token) overlap. Ties break toward a
    higher ``outcome_score`` then a higher ``use_count``.
    """
    k = max(1, int(k))
    key = task_type_key(task_type_or_text)
    if not key:
        return []

    db = _connect(db_path)
    try:
        rows = db.execute(
            "SELECT id, task_type, plan_shape_json, outcome_score, use_count "
            "FROM plan_templates"
        ).fetchall()
    finally:
        db.close()
    if not rows:
        return []

    emb_scores = (
        _embedding_scores(task_type_or_text, rows) if use_embeddings else None
    )

    scored: list[PlanTemplate] = []
    for r in rows:
        rid = int(r["id"])
        if r["task_type"] == key:
            rel = 1.0  # exact label match — top relevance
        elif emb_scores is not None:
            rel = emb_scores.get(rid, 0.0)
        else:
            rel = _keyword_score(key, r["task_type"])
        if rel <= 0.0 and r["task_type"] != key:
            continue
        scored.append(_row_to_template(r, score=rel))

    scored.sort(
        key=lambda t: (
            t.score,
            t.outcome_score if t.outcome_score is not None else -1.0,
            t.use_count,
        ),
        reverse=True,
    )
    return scored[:k]
