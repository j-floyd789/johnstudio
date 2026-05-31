"""Per-agent context-pack generator.

Builds a dedicated prompt file for each worker on a task. No giant CLAUDE.md.
Each context pack contains explicit sections that the worker is expected to
follow, including rule precedence and an output contract.
"""
from __future__ import annotations

from pathlib import Path

from . import config, memory, patterns, skill_router, utils
from .models import ContextPack, ProjectConfig, SkillRouteResult, WorkerConfig

RULE_PRECEDENCE = (
    "1. Explicit user instruction\n"
    "2. Safety policy\n"
    "3. Project-specific rules\n"
    "4. Current task instructions\n"
    "5. Loaded skill guidance\n"
    "6. General best practices\n"
)


def _read(p: Path, max_chars: int = 4000) -> str:
    if not p.exists():
        return ""
    text = p.read_text(encoding="utf-8")
    if len(text) > max_chars:
        return text[:max_chars] + "\n…(truncated)\n"
    return text


def _scope_block(worker_cfg: WorkerConfig, worktree_path: Path | None) -> str:
    lines = []
    if worker_cfg.can_edit:
        lines.append(f"- CAN EDIT files inside your worktree: `{worktree_path}`")
        lines.append("- DO NOT modify files outside the worktree.")
    else:
        lines.append("- READ-ONLY. Do not modify files.")
    if not worker_cfg.worktree:
        lines.append("- No worktree assigned; review only.")
    return "\n".join(lines)


def _selected_skills_block(skills: list[SkillRouteResult]) -> str:
    if not skills:
        return "_No skills selected for this agent._\n"
    blocks: list[str] = []
    reg = config.home_dir() / "skill-registry" / "skills"
    for s in skills:
        # Use distilled body by default.
        body = _read(reg / s.skill_id / "distilled.md", max_chars=8000)
        blocks.append(
            f"### Skill: {s.skill_id}\n_score={s.score:.0f}  tokens={s.tokens}  why={s.rationale}_\n\n{body}"
        )
    return "\n\n".join(blocks)


def _safety_block(safety_paths: list[str], dangerous: list[str]) -> str:
    lines = ["**Protected paths (never modify):**"]
    lines.extend(f"- `{p}`" for p in safety_paths)
    lines.append("\n**Dangerous commands (never run without approval):**")
    lines.extend(f"- `{c}`" for c in dangerous)
    return "\n".join(lines)


def _graph_links_for(repo_path: str | Path, project_name: str) -> list[str]:
    links = [
        f"[[Project - {project_name}]]",
        "[[Person - John]]",
    ]
    return links


def shared_artifacts_dir(repo_path: str | Path, task_id: int) -> Path:
    """Absolute path to a task's SHARED artifacts directory.

    `<repo>/.johnstudio/tasks/task-<NNNN>/shared_artifacts/` — the one
    location every worker on the task reads/writes structured outputs to,
    so siblings (on other branches/worktrees) and the synthesizer all see
    the SAME files. Must match the orchestrator-wide convention verbatim.
    """
    return (
        Path(repo_path)
        / ".johnstudio"
        / "tasks"
        / f"task-{task_id:04d}"
        / patterns.SHARED_ARTIFACTS_DIRNAME
    )


def _ensure_shared_artifacts(shared_dir: Path, worktree_path: Path | None) -> None:
    """Create the shared dir and link it into the worker's worktree.

    Per-worktree git isolation means a worker on branch B cannot see a
    file another worker committed on branch A. By writing artifacts into a
    repo-root-relative shared dir (outside any branch's tracked tree) and
    exposing it via a `<worktree>/shared_artifacts` symlink, every worker
    reaches the identical absolute path. Best-effort: never block the pack.
    """
    try:
        shared_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    if worktree_path is None:
        return
    link = Path(worktree_path) / patterns.SHARED_ARTIFACTS_DIRNAME
    try:
        # If a correct symlink already exists, leave it. Replace a stale one.
        if link.is_symlink():
            if Path(link.readlink()).resolve() == shared_dir.resolve():
                return
            link.unlink()
        elif link.exists():
            # A real dir/file is squatting the path — don't clobber it.
            return
        link.symlink_to(shared_dir, target_is_directory=True)
    except OSError:
        # Symlink unsupported / racing worker / perms — workers can still
        # use the absolute shared path printed in the context pack.
        pass


def _artifact_name(worker_name: str, worker_index: int | None) -> str:
    """Canonical artifact filename for this worker.

    Uses the 1-based ``worker_index`` when known; otherwise falls back to a
    slug of the worker name so the filename stays unique per worker.
    """
    n = worker_index if worker_index is not None else utils.slugify(worker_name)
    return patterns.artifact_filename(n)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_context_pack(
    *,
    project_cfg: ProjectConfig,
    project_name: str,
    worker_name: str,
    worker_cfg: WorkerConfig,
    task_id: int,
    task_title: str,
    task_description: str,
    worktree_path: Path | None,
    relevant_files: list[str] | None = None,
    worker_index: int | None = None,
) -> tuple[ContextPack, str]:
    """Return (ContextPack model, rendered markdown).

    Renders all required sections in order: Role / Scope / Task / Project Summary /
    Current State / Relevant Files / Selected Skills / Knowledge Graph Links /
    Safety Rules / Rule Precedence / Output Contract / Completion Signal.
    """
    global_cfg = config.load_global_config()
    relevant_files = relevant_files or []

    # Shared artifacts: a single per-task dir all workers read/write, plus a
    # per-worktree symlink so branch-isolated siblings + the synthesizer see
    # the SAME files. Best-effort — never blocks the pack (see helper).
    shared_dir = shared_artifacts_dir(project_cfg.repo_path, task_id)
    _ensure_shared_artifacts(shared_dir, worktree_path)
    artifact_name = _artifact_name(worker_name, worker_index)

    # Skills
    req = skill_router.RouteRequest(
        project=project_cfg,
        agent_role=worker_cfg.role,
        task_text=f"{task_title}\n{task_description}",
        relevant_files=relevant_files,
        memory_text=_read(memory.memory_root(project_cfg.repo_path) / "current_state.md"),
        feedback=skill_router.previous_feedback(),
    )
    selected = skill_router.route(req)

    # Memory excerpts
    mem = memory.memory_root(project_cfg.repo_path)
    project_summary = _read(mem / "project_brief.md", max_chars=2000)
    current_state = _read(mem / "current_state.md", max_chars=2000)

    pack = ContextPack(
        worker_name=worker_name,
        task_id=task_id,
        role=worker_cfg.role,
        scope=str(worktree_path) if worker_cfg.worktree else "review-only",
        task_title=task_title,
        task_description=task_description,
        project_summary=project_summary,
        current_state=current_state,
        relevant_files=relevant_files,
        selected_skills=selected,
        graph_links=_graph_links_for(project_cfg.repo_path, project_name),
        safety_notes=global_cfg.safety.blocked_paths,
    )

    md = _render_markdown(
        pack, project_cfg, worker_cfg, worktree_path, global_cfg,
        shared_dir=shared_dir, artifact_name=artifact_name,
    )
    return pack, md


def build_phase_context_pack(
    *,
    phase,
    round: int,
    project_cfg: ProjectConfig,
    project_name: str,
    worker_name: str,
    worker_cfg: WorkerConfig,
    task_id: int,
    task_title: str,
    task_description: str,
    task_folder: Path,
    worktree_path: Path | None,
    prior_artifacts: str | None = None,
) -> tuple[ContextPack, str]:
    """Chain-mode context pack: the standard pack plus phase/round framing
    and the prior phase's artifacts to build on.

    # RECONSTRUCTED: the original was lost (no file-history backup); rebuilt
    # from the chain.run_phase call site. Wraps build_context_pack and adds a
    # phase header + the prior phase's artifacts so each chain phase continues
    # from the last rather than restarting from the brief.
    """
    pack, base_md = build_context_pack(
        project_cfg=project_cfg, project_name=project_name,
        worker_name=worker_name, worker_cfg=worker_cfg,
        task_id=task_id, task_title=task_title,
        task_description=task_description, worktree_path=worktree_path,
    )
    phase_label = getattr(phase, "value", str(phase))
    header = (
        f"# Chain phase: {phase_label} (round {round})\n\n"
        f"You are executing the **{phase_label}** phase of a multi-phase chain "
        f"for task {task_id:04d}. Build on the prior phase's output below; do "
        f"not restart from the brief.\n"
    )
    parts = [header, base_md]
    if prior_artifacts:
        parts.append(
            "\n---\n\n## Prior phase artifacts\n\n"
            + str(prior_artifacts).rstrip() + "\n"
        )
    md = "\n".join(parts).rstrip() + "\n"
    return pack, md


def _render_markdown(
    pack: ContextPack,
    project_cfg: ProjectConfig,
    worker_cfg: WorkerConfig,
    worktree_path: Path | None,
    global_cfg,
    *,
    shared_dir: Path,
    artifact_name: str,
) -> str:
    sections = [
        f"# {pack.worker_name} — Task {pack.task_id:04d}",
        "",
        "## Role",
        f"You are **{pack.worker_name}** acting as **{pack.role}** on project `{project_cfg.name}`.",
        "",
        "## Scope",
        _scope_block(worker_cfg, worktree_path),
        "",
        "## Task",
        f"**{pack.task_title}**\n\n{pack.task_description}",
        "",
        "## Project Summary",
        pack.project_summary or "_(no project brief on file)_",
        "",
        "## Current State",
        pack.current_state or "_(no current state on file)_",
        "",
        "## Relevant Files",
        "\n".join(f"- `{f}`" for f in pack.relevant_files) or "_(none identified)_",
        "",
        "## Selected Skills",
        _selected_skills_block(pack.selected_skills),
        "",
        "## Knowledge Graph Links",
        "\n".join(f"- {l}" for l in pack.graph_links),
        "",
        "## Safety Rules",
        _safety_block(global_cfg.safety.blocked_paths, global_cfg.safety.dangerous_commands),
        "",
        "## Rule Precedence",
        RULE_PRECEDENCE,
        "",
        "## Shared Artifacts — READ THIS",
        (
            "Your structured output MUST go into the **shared artifacts** "
            "directory, NOT into `results/` on your private branch. Per-worktree "
            "git isolation means files you commit to your own branch are "
            "INVISIBLE to sibling workers and to the synthesizer — only the "
            "shared dir is seen by everyone.\n\n"
            f"- **Write your candidate to:** `shared_artifacts/{artifact_name}`\n"
            f"  (relative to your worktree root — a symlink points it at the "
            f"shared dir). Absolute path if the symlink is missing:\n"
            f"  `{shared_dir / artifact_name}`\n"
            f"- Use **exactly** this filename — `{artifact_name}`. Do NOT invent "
            "names like `angle_N.json` / `result_N.json`; the inspector and "
            "synthesizer only look for the canonical name.\n"
            "- Other workers' candidates land in the SAME `shared_artifacts/` "
            "dir — read them there if you need to compare.\n"
            "- JSON top-level shape (documented contract): "
            f"`{', '.join(patterns.ARTIFACT_TOP_LEVEL_KEYS)}`.\n"
        ),
        "",
        "## Output Contract",
        (
            "When done (or blocked):\n\n"
            f"1. **Write your candidate JSON** to `shared_artifacts/{artifact_name}` "
            "(see the Shared Artifacts section above) so siblings + the "
            "synthesizer can read it across branches.\n\n"
            "2. **Commit your work** to the current branch before writing RESULT.md:\n"
            "   `git add -A && git commit -m \"<short message>\"`\n"
            "   The orchestrator needs commits on this branch in order to merge.\n"
            "   (Note: `shared_artifacts/` is outside your branch's tracked "
            "tree — the candidate JSON above lives there by design, not in "
            "your commit.)\n\n"
            "3. Write `RESULT.md` in your worktree root with these sections:\n\n"
            "- **Summary** — one paragraph of what you did.\n"
            "- **Files changed** — full paths.\n"
            "- **Tests run** — command, exit code, brief result.\n"
            "- **Risks** — anything reviewer should look at twice.\n"
            "- **Blockers** — what stopped you, if anything.\n"
            "- **Handoff requests** — optional `HANDOFF_REQUEST.md` if you need another agent.\n"
            "- **Skill feedback** — which loaded skills were useful / not.\n"
            "- **New memory facts** — bullets the orchestrator should write into memory.\n"
            "- **Suggested tags/entities** — knowledge-graph updates.\n"
            "- **Next recommended action** — concrete next step.\n"
        ),
        "",
        "## Tool resilience (IMPORTANT)",
        "If a tool or shell command returns empty output, **retry it** — transient "
        "empty results and rate-limit blips happen. Do NOT conclude your tools or the "
        "harness are broken and give up; verify with a trivial probe (e.g. `echo ok`) "
        "and continue. Only write a blocked/failed RESULT.md if you genuinely cannot "
        "proceed after retrying. Never finish with zero changes when the task is "
        "implementable.\n",
        "",
        "## Completion Signal",
        "When fully complete, also write `DONE.md` with a single line: `status: COMPLETE`.\n",
    ]
    return "\n".join(sections).rstrip() + "\n"
