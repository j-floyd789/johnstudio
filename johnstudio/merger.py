"""Merger: gated by human confirmation. Updates memory and graph after success."""
from __future__ import annotations

import shlex
import subprocess
from datetime import datetime
from pathlib import Path

from . import (
    config,
    db,
    git_worktree as gw,
    knowledge_graph as kg,
    memory,
    project as project_mod,
    utils,
)


class MergeAborted(RuntimeError):
    pass


def merge(
    task_number: int,
    project_name: str,
    worker_name: str,
    *,
    dry_run: bool = False,
    assume_yes: bool = False,
    confirm: bool | None = None,
) -> dict:
    """Merge the worker's branch into base. Requires explicit confirmation.

    `confirm=True` is the programmatic equivalent of typing 'y' at the prompt.
    `assume_yes=True` is for tests only.
    """
    proj = project_mod.get_project(project_name)
    if not proj:
        raise KeyError(project_name)
    pcfg = config.load_project_config(proj["repo_path"])
    repo = Path(proj["repo_path"])
    branch = gw.branch_name_for(task_number, worker_name)

    # Fetch the task row UP FRONT. If the DB is out of sync with the repo we
    # must fail before mutating anything — not after the branch has merged.
    _c = db.connect()
    try:
        db.init_schema(_c)
        task = _c.execute(
            "SELECT id, title FROM tasks WHERE project_id = ? AND task_number = ?",
            (proj["id"], task_number),
        ).fetchone()
    finally:
        _c.close()
    if not task:
        raise KeyError(f"task {task_number} not found in project {project_name!r}")

    if not gw.is_clean(repo):
        raise MergeAborted(f"Working tree at {repo} is not clean; refusing to merge.")

    if dry_run:
        code, out = gw.merge_branch(repo, branch, dry_run=True)
        return {"dry_run": True, "exit_code": code, "output": out, "branch": branch}

    if not (assume_yes or confirm):
        raise MergeAborted("Merge requires explicit confirmation (confirm=True or --yes).")

    # Checkout base and merge.
    gw.checkout(repo, pcfg.base_branch)
    code, out = gw.merge_branch(repo, branch, dry_run=False)
    if code != 0:
        return {"merged": False, "exit_code": code, "output": out, "branch": branch}

    # Run tests if configured.
    test_results = []
    for tc in pcfg.test_commands:
        try:
            cp = subprocess.run(shlex.split(tc), cwd=str(repo), shell=False, capture_output=True, text=True, timeout=180)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            # The merge already landed; a test command that times out or can't
            # be launched must be reported as a test failure, not crash merge().
            return {
                "merged": True, "tests_passed": False,
                "branch": branch, "test_results": test_results,
                "note": f"Merge succeeded but test command {tc!r} could not run: {e}; consider revert.",
            }
        test_results.append({"command": tc, "exit_code": cp.returncode})
        if cp.returncode != 0:
            return {
                "merged": True, "tests_passed": False,
                "branch": branch, "test_results": test_results,
                "note": "Merge succeeded but tests failed; consider revert.",
            }

    # Memory + graph update. (task was fetched up front, above.)
    decision_slug = utils.slugify(task["title"])[:40] or f"task-{task_number}"
    decision_path = memory.write_decision(
        repo,
        decision_slug,
        _decision_md(task_number, task["title"], worker_name, branch, pcfg.base_branch),
    )

    # Knowledge graph.
    task_entity = kg.create_entity(
        project_id=proj["id"], repo_path=repo,
        entity_type="task", name=f"{task_number:04d} - {task['title'][:60]}",
        tags=["task", "merged"],
        metadata={"branch": branch, "merged_at": datetime.utcnow().isoformat(timespec="seconds")},
        body=f"# Task {task_number:04d}\n\n{task['title']}\n\nMerged from `{branch}` into `{pcfg.base_branch}`.\n",
    )
    kg.link_entities(
        proj["id"],
        ("task", f"{task_number:04d} - {task['title'][:60]}"),
        ("project", project_name),
        "belongs_to",
        source_note_path=str(task_entity),
    )
    decision_name = f"{datetime.utcnow().date()} - {decision_slug}"
    decision_entity = kg.create_entity(
        project_id=proj["id"], repo_path=repo,
        entity_type="decision", name=decision_name,
        tags=["decision", "merged"],
        metadata={"task_number": task_number, "branch": branch},
        body=decision_path.read_text(encoding="utf-8"),
    )
    kg.link_entities(
        proj["id"],
        ("decision", decision_name),
        ("task", f"{task_number:04d} - {task['title'][:60]}"),
        "decided_for",
        source_note_path=str(decision_entity),
    )

    conn = db.connect()
    try:
        db.init_schema(conn)
        conn.execute(
            "UPDATE tasks SET status = 'merged', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (task["id"],),
        )
        conn.execute(
            """INSERT INTO decisions (project_id, task_id, title, content_path) VALUES (?,?,?,?)""",
            (proj["id"], task["id"], task["title"], str(decision_path)),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "merged": True, "tests_passed": True,
        "branch": branch, "decision_path": str(decision_path),
        "test_results": test_results,
    }


def _decision_md(task_number, title, worker_name, branch, base) -> str:
    return (
        f"# Decision: merge task-{task_number:04d}\n\n"
        f"- Task: {title}\n"
        f"- Worker: `{worker_name}`\n"
        f"- Branch: `{branch}` → `{base}`\n"
        f"- Merged: {datetime.utcnow().isoformat(timespec='seconds')}\n\n"
        f"## Rationale\n_(human-authored, paste here)_\n\n"
        f"## Links\n- [[Project - X]]\n"
    )
