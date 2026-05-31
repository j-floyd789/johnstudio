"""Deterministic review and merge planning. No LLM in MVP."""
from __future__ import annotations

import json
from pathlib import Path

from . import collector, config, db, project as project_mod, utils
from .models import ReviewScore


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _score_run(run_summary: dict, files_changed: list[str], stat_lines: int) -> ReviewScore:
    breakdown: dict[str, int] = {}
    flags: list[str] = []

    test_results = run_summary.get("tests") or []
    if test_results:
        if all(t["exit_code"] == 0 for t in test_results):
            breakdown["tests_pass"] = 30
        else:
            breakdown["tests_fail"] = -30
            flags.append("tests-failed")

    if 0 < len(files_changed) <= 10:
        breakdown["small_diff"] = 10
    elif len(files_changed) > 40:
        breakdown["huge_diff"] = -25
        flags.append("huge-diff")

    if run_summary.get("protected_path_hits"):
        breakdown["protected_path"] = -50
        flags.append("protected-path-touched")

    if run_summary.get("dangerous_command_hits"):
        breakdown["dangerous_command"] = -50
        flags.append("dangerous-command")

    if run_summary.get("approval_command_hits"):
        breakdown["approval_command"] = -10
        flags.append("approval-needed")

    if run_summary.get("done_present"):
        breakdown["completed"] = 15

    if not run_summary.get("result_present"):
        breakdown["no_result_md"] = -15
        flags.append("missing-result-md")

    score = sum(breakdown.values())
    return ReviewScore(
        worker_name=run_summary["worker"],
        score=score,
        breakdown=breakdown,
        flags=flags,
    )


# ---------------------------------------------------------------------------
# Build review packet
# ---------------------------------------------------------------------------

def review(task_number: int, project_name: str) -> dict:
    """Run review against a previously-collected task. Returns scores + paths."""
    summary = collector.collect(task_number, project_name)  # re-collect for freshness
    proj = project_mod.get_project(project_name)
    pcfg = config.load_project_config(proj["repo_path"])
    base_branch = pcfg.base_branch
    repo_path = Path(proj["repo_path"])
    task_folder = repo_path / ".johnstudio" / "tasks" / f"task-{task_number:04d}"

    conn = db.connect()
    db.init_schema(conn)
    task = conn.execute(
        "SELECT id, title, description FROM tasks WHERE project_id = ? AND task_number = ?",
        (proj["id"], task_number),
    ).fetchone()

    scores: list[ReviewScore] = []
    per_worker_diff_size: dict[str, int] = {}
    for r in summary["runs"]:
        diff_path = task_folder / "diffs" / f"{r['worker']}.diff"
        stat_lines = 0
        if diff_path.exists():
            stat_lines = sum(1 for _ in diff_path.read_text().splitlines())
        scores.append(_score_run(r, r.get("files_changed", []), stat_lines))
        per_worker_diff_size[r["worker"]] = stat_lines

    scores.sort(key=lambda s: s.score, reverse=True)
    best = scores[0] if scores else None

    final_review = _render_final_review(task_number, task, summary, scores, base_branch)
    merge_plan = _render_merge_plan(task_number, task, summary, scores, best, base_branch, pcfg.test_commands)

    fr_path = task_folder / "FINAL_REVIEW.md"
    mp_path = task_folder / "MERGE_PLAN.md"
    utils.write_text(fr_path, final_review, overwrite=True)
    utils.write_text(mp_path, merge_plan, overwrite=True)

    for s in scores:
        conn.execute(
            """INSERT INTO reviews (task_id, reviewer_worker_id, review_path, recommendation, score_json)
               VALUES (?, NULL, ?, ?, ?)""",
            (task["id"], str(fr_path),
             "merge" if (best and s.worker_name == best.worker_name and s.score >= 30 and not s.flags) else "skip",
             json.dumps(s.model_dump())),
        )
    conn.execute(
        "UPDATE tasks SET status = 'reviewed', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (task["id"],),
    )
    conn.commit()
    conn.close()

    return {
        "task_number": task_number,
        "scores": [s.model_dump() for s in scores],
        "final_review_path": str(fr_path),
        "merge_plan_path": str(mp_path),
        "recommended": best.worker_name if best else None,
    }


def _render_final_review(task_number, task, summary, scores, base_branch) -> str:
    lines = [
        f"# Final Review — task-{task_number:04d}",
        "",
        f"**Task:** {task['title']}",
        f"**Base branch:** `{base_branch}`",
        "",
        "## Scores",
        "| worker | score | flags |",
        "|---|---|---|",
    ]
    for s in scores:
        flags = ", ".join(s.flags) or "—"
        lines.append(f"| `{s.worker_name}` | {s.score} | {flags} |")

    lines.append("\n## Per-worker details\n")
    for r in summary["runs"]:
        lines.append(f"### `{r['worker']}`")
        lines.append(f"- result.md: {'yes' if r['result_present'] else 'no'}")
        lines.append(f"- done.md: {'yes' if r['done_present'] else 'no'}")
        lines.append(f"- files changed ({len(r['files_changed'])}): " + (", ".join(r['files_changed']) or "—"))
        if r.get("tests"):
            for t in r["tests"]:
                lines.append(f"- test `{t['command']}` exit={t['exit_code']}")
        if r.get("protected_path_hits"):
            lines.append(f"- **PROTECTED PATH HITS:** {r['protected_path_hits']}")
        if r.get("dangerous_command_hits"):
            lines.append(f"- **DANGEROUS COMMAND HITS:** {r['dangerous_command_hits']}")
        if r.get("approval_command_hits"):
            lines.append(f"- approval required for: {r['approval_command_hits']}")
        lines.append("")

    lines += [
        "## Recommendation",
        f"`{scores[0].worker_name}` scored highest at {scores[0].score}."
        if scores else "_(no runs)_",
        "",
        "Human must confirm before merge. Use `johnstudio merge <task_number> <worker_name>`.",
    ]
    return "\n".join(lines) + "\n"


def _render_merge_plan(task_number, task, summary, scores, best, base_branch, test_commands) -> str:
    if not best:
        return f"# Merge Plan — task-{task_number:04d}\n\n_No runs to merge._\n"
    chosen = next((r for r in summary["runs"] if r["worker"] == best.worker_name), None)
    files = chosen.get("files_changed", []) if chosen else []
    return "\n".join([
        f"# Merge Plan — task-{task_number:04d}",
        "",
        f"**Selected worker:** `{best.worker_name}` (score {best.score})",
        f"**Branch:** `ai/task-{task_number:04d}/{best.worker_name.replace('_','-')}`",
        f"**Base:** `{base_branch}`",
        "",
        "## Files to merge",
        "\n".join(f"- `{f}`" for f in files) or "_(none — empty diff)_",
        "",
        "## Tests to run after merge",
        "\n".join(f"- `{c}`" for c in test_commands) or "_(no test commands configured)_",
        "",
        "## Rollback plan",
        f"`git -C <repo> reset --hard <previous_HEAD>`  (use the commit hash printed pre-merge).",
        "",
        "## Human confirmation checklist",
        "- [ ] Read FINAL_REVIEW.md",
        "- [ ] Eyeball diff",
        "- [ ] Tests passed in the worktree",
        "- [ ] No protected path touched",
        "- [ ] No dangerous commands queued",
        "",
    ]) + "\n"
