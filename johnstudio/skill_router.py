"""Deterministic skill router.

Given a project, task, agent role, repo metadata, relevant files, pinned skills,
and previous feedback, returns ranked skills selected within a token budget.

Scoring (per spec):
    +10 pinned + category matches task
    +8  task keywords match skill tags
    +7  repo dependencies match skill frameworks
    +6  relevant files match file_patterns
    +5  agent role matches agent_roles
    +4  project memory mentions skill/category
    +3  prior tasks found this skill useful
    -5  prior tasks found this skill not useful
    -8  skill conflicts with project stack
   -10  trust_level=unreviewed AND safety-sensitive task
  -100  skill enabled=false (unless preview)
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import config, db, utils
from .models import GlobalConfig, ProjectConfig, SkillMetadata, SkillRouteResult


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

@dataclass
class RouteRequest:
    project: ProjectConfig
    agent_role: str
    task_text: str
    relevant_files: list[str] = field(default_factory=list)
    memory_text: str = ""
    safety_sensitive: bool | None = None
    preview: bool = False
    feedback: dict[str, int] = field(default_factory=dict)  # skill_id -> usefulness signal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAFETY_KEYWORDS = {
    "auth", "authentication", "authorize", "permission", "password", "secret",
    "credential", "billing", "payment", "stripe", "pii", "encrypt", "key",
    "token", "session", "csrf",
}


def _safety_sensitive(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in SAFETY_KEYWORDS)


def _keyword_overlap(text: str, terms: list[str]) -> int:
    if not terms:
        return 0
    low = text.lower()
    return sum(1 for t in terms if t and re.search(rf"\b{re.escape(t.lower())}\b", low))


def _files_match_patterns(files: list[str], patterns: list[str]) -> bool:
    for p in patterns:
        for f in files:
            if fnmatch.fnmatch(f, p):
                return True
    return False


# ---------------------------------------------------------------------------
# Loading skills from registry
# ---------------------------------------------------------------------------

def _registry_dir() -> Path:
    return config.home_dir() / "skill-registry" / "skills"


def _load_all_meta() -> list[SkillMetadata]:
    base = _registry_dir()
    if not base.exists():
        return []
    out: list[SkillMetadata] = []
    for sub in sorted(base.iterdir()):
        f = sub / "metadata.yaml"
        if not f.exists():
            continue
        try:
            out.append(SkillMetadata.model_validate(yaml.safe_load(f.read_text())))
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _score_one(
    meta: SkillMetadata,
    req: RouteRequest,
    safety_sensitive: bool,
    *,
    global_cfg: GlobalConfig,
) -> tuple[float, list[str]]:
    score = 0.0
    why: list[str] = []

    pinned = meta.id in req.project.pinned_skills
    if pinned and meta.category and meta.category in (req.project.stack.frameworks + req.project.stack.languages + [req.agent_role]):
        score += 10
        why.append("+10 pinned & category-matches")
    elif pinned:
        score += 5
        why.append("+5 pinned (category not specifically matched)")

    if meta.tags:
        overlap = _keyword_overlap(req.task_text, meta.tags)
        if overlap:
            score += min(8, overlap * 2 + 2)
            why.append(f"+{min(8, overlap*2+2)} task keywords overlap tags x{overlap}")

    deps = set(req.project.stack.frameworks) | set(req.project.stack.languages)
    if deps & set(meta.frameworks) or deps & set(meta.languages):
        score += 7
        why.append("+7 repo dependencies match skill")

    if req.relevant_files and meta.file_patterns and _files_match_patterns(req.relevant_files, meta.file_patterns):
        score += 6
        why.append("+6 relevant files match patterns")

    if req.agent_role in meta.agent_roles:
        score += 5
        why.append("+5 agent role matches")

    if req.memory_text and meta.id.lower() in req.memory_text.lower():
        score += 4
        why.append("+4 project memory mentions skill")
    elif req.memory_text and meta.category and meta.category.lower() in req.memory_text.lower():
        score += 2
        why.append("+2 project memory mentions category")

    fb = req.feedback.get(meta.id, 0)
    if fb > 0:
        score += 3
        why.append("+3 prior tasks found skill useful")
    elif fb < 0:
        score -= 5
        why.append("-5 prior tasks marked skill not useful")

    # Stack-conflict heuristic: a frontend-tagged skill on a pure-backend repo (and vice versa)
    if "frontend" in meta.tags and not (req.project.stack.frameworks and any(
        f in ("react", "nextjs", "vue", "svelte", "tailwind") for f in req.project.stack.frameworks
    )):
        score -= 4
        why.append("-4 frontend skill on non-frontend stack")

    if meta.trust_level == "unreviewed" and safety_sensitive:
        score -= 10
        why.append("-10 unreviewed skill on safety-sensitive task")

    if not meta.enabled and not req.preview:
        score -= 100
        why.append("-100 skill disabled (use --preview to override)")

    return score, why


def route(req: RouteRequest) -> list[SkillRouteResult]:
    """Score all skills, then select within token budget. Returns selected skills only."""
    global_cfg = config.load_global_config()
    metas = _load_all_meta()
    safety_sensitive = req.safety_sensitive if req.safety_sensitive is not None else _safety_sensitive(req.task_text)

    scored: list[tuple[SkillMetadata, float, list[str]]] = []
    for m in metas:
        s, why = _score_one(m, req, safety_sensitive, global_cfg=global_cfg)
        if s <= -90:  # disabled skills are out of contention by default
            continue
        scored.append((m, s, why))

    scored.sort(key=lambda t: t[1], reverse=True)

    max_skills = global_cfg.skill_registry.max_skills_per_agent
    max_tokens_total = global_cfg.skill_registry.max_skill_tokens_per_agent
    max_tokens_single = global_cfg.skill_registry.max_single_skill_tokens

    selected: list[SkillRouteResult] = []
    used_tokens = 0
    for meta, score, why in scored:
        if len(selected) >= max_skills:
            break
        if score <= 0:
            # No positive signal: stop adding to avoid burning budget on weak matches.
            break
        body_path = _registry_dir() / meta.id / (
            "distilled.md" if global_cfg.skill_registry.use_distilled_skills else "original.md"
        )
        if not body_path.exists():
            continue
        tokens = utils.approx_token_count(body_path.read_text(encoding="utf-8"))
        if tokens > max_tokens_single:
            # Fall back to summary if available.
            summary_path = _registry_dir() / meta.id / "summary.md"
            if summary_path.exists():
                tokens = utils.approx_token_count(summary_path.read_text(encoding="utf-8"))
            else:
                continue
        if used_tokens + tokens > max_tokens_total:
            continue
        used_tokens += tokens
        selected.append(SkillRouteResult(
            skill_id=meta.id,
            score=score,
            rationale="; ".join(why),
            tokens=tokens,
        ))
    return selected


# ---------------------------------------------------------------------------
# Feedback retrieval
# ---------------------------------------------------------------------------

def previous_feedback() -> dict[str, int]:
    """Return mapping skill_id -> summed usefulness signal across history."""
    conn = db.connect()
    db.init_schema(conn)
    cur = conn.execute(
        "SELECT skill_id, SUM(usefulness) AS s FROM skill_feedback GROUP BY skill_id"
    )
    out = {row["skill_id"]: int(row["s"] or 0) for row in cur.fetchall() if row["skill_id"]}
    conn.close()
    return out
