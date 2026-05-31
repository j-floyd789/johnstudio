"""Orchestrator: turns a (project, task) into a coordinated run of workers.

For each task it:
  1. Inserts a `tasks` row and gets the task id (auto-increment within project).
  2. Chooses the active team based on flags and worker availability.
  3. For each edit-capable worker: creates a git worktree + branch.
  4. Builds a per-worker context pack via `context_builder`.
  5. Launches each worker via tmux (or subprocess fallback).
  6. Records `runs` rows for status/resume/cleanup.

Workers cannot spawn other workers; they may write HANDOFF_REQUEST.md which the
orchestrator surfaces (collector reads it on the next `collect`).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from . import (
    config,
    context_builder,
    db,
    git_worktree as gw,
    project as project_mod,
    tmux_controller,
    utils,
    workers,
)
from .models import GlobalConfig, ProjectConfig, WorkerConfig


# ---------------------------------------------------------------------------
# Team selection
# ---------------------------------------------------------------------------

DEFAULT_TEAM = [
    "claude_backend",
    "claude_frontend",
    "codex_tests",
    "gemini_review",
    "security_review",
]

STUB_TEAM = ["terminal_stub"]


def choose_team(
    global_cfg: GlobalConfig,
    *,
    requested: list[str] | None = None,
    stub_only: bool = False,
    max_agents: int | None = None,
) -> list[str]:
    if stub_only:
        return STUB_TEAM
    if requested:
        candidates = [w for w in requested if w in global_cfg.workers]
    else:
        candidates = [w for w in DEFAULT_TEAM if w in global_cfg.workers]
    # Filter by availability (binary present), but keep `always_available` workers.
    available: list[str] = []
    for name in candidates:
        cfg = global_cfg.workers[name]
        worker = workers.make_worker(name, cfg)
        if worker.is_available():
            available.append(name)
    if not available:
        # Fall back to stub so the pipeline still runs.
        return STUB_TEAM
    cap = max_agents or global_cfg.runtime.max_active_agents
    return available[:cap]


# ---------------------------------------------------------------------------
# Task scaffolding
# ---------------------------------------------------------------------------

@dataclass
class TaskPaths:
    task_id_internal: int   # DB primary key
    task_number: int        # per-project counter (used in folder names)
    folder: Path
    prompts_dir: Path
    results_dir: Path
    diffs_dir: Path
    test_dir: Path
    logs_dir: Path


def _next_task_number(conn, project_id: int) -> int:
    cur = conn.execute(
        "SELECT COALESCE(MAX(task_number), 0) AS m FROM tasks WHERE project_id = ?", (project_id,)
    )
    return int(cur.fetchone()["m"]) + 1


def _task_paths(repo_path: Path, task_number: int) -> Path:
    return repo_path / ".johnstudio" / "tasks" / f"task-{task_number:04d}"


def _scaffold(repo_path: Path, task_number: int) -> TaskPaths:
    base = _task_paths(repo_path, task_number)
    for sub in ("prompts", "results", "diffs", "test_results", "logs"):
        (base / sub).mkdir(parents=True, exist_ok=True)
    return TaskPaths(
        task_id_internal=-1,
        task_number=task_number,
        folder=base,
        prompts_dir=base / "prompts",
        results_dir=base / "results",
        diffs_dir=base / "diffs",
        test_dir=base / "test_results",
        logs_dir=base / "logs",
    )


def _ensure_worker_row(conn, name: str, cfg: WorkerConfig) -> int:
    cur = conn.execute(
        """INSERT INTO workers (name, provider, role, command, can_edit, worktree_enabled)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(name) DO UPDATE SET
               provider = excluded.provider, role = excluded.role,
               command = excluded.command, can_edit = excluded.can_edit,
               worktree_enabled = excluded.worktree_enabled
           RETURNING id""",
        (name, cfg.provider, cfg.role, cfg.command,
         1 if cfg.can_edit else 0, 1 if cfg.worktree else 0),
    )
    return int(cur.fetchone()["id"])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run(
    project_name: str,
    task_text: str,
    *,
    dry_run: bool = False,
    stub_only: bool = False,
    requested_workers: list[str] | None = None,
    max_agents: int | None = None,
    relevant_files: list[str] | None = None,
) -> dict:
    """Create and launch a task. Returns task summary dict."""
    global_cfg = config.load_global_config()
    proj = project_mod.get_project(project_name)
    if not proj:
        raise KeyError(f"Project not registered: {project_name}")
    pcfg = config.load_project_config(proj["repo_path"])

    conn = db.connect()
    repo_path = Path(proj["repo_path"])
    created_worktrees: list[Path] = []
    task_db_id: int | None = None
    try:
        db.init_schema(conn)
        task_number = _next_task_number(conn, proj["id"])
        cur = conn.execute(
            """INSERT INTO tasks (project_id, task_number, title, description, status, base_branch)
               VALUES (?,?,?,?,?,?) RETURNING id""",
            (proj["id"], task_number, task_text[:80], task_text, "pending", pcfg.base_branch),
        )
        task_db_id = int(cur.fetchone()["id"])
        conn.commit()

        paths = _scaffold(repo_path, task_number)

        # TASK.md is the canonical task statement.
        utils.write_text(
            paths.folder / "TASK.md",
            f"# Task {task_number:04d}\n\n{task_text}\n",
            overwrite=True,
        )

        team = choose_team(global_cfg, requested=requested_workers, stub_only=stub_only, max_agents=max_agents)
        if dry_run:
            plan = _dry_run_plan(repo_path, pcfg, global_cfg, task_db_id, task_number, team, paths)
            utils.write_text(paths.folder / "DRY_RUN_PLAN.md", plan, overwrite=True)
            return {
                "task_db_id": task_db_id, "task_number": task_number,
                "task_folder": str(paths.folder), "team": team, "dry_run": True,
            }

        # Real run.
        session = f"johnstudio-task-{task_number:04d}"
        use_tmux = tmux_controller.is_available()
        if use_tmux and not tmux_controller.session_exists(session):
            tmux_controller.new_session(session, cwd=repo_path)

        launched: list[dict] = []
        from . import spawner
        for worker_index, name in enumerate(team, 1):
            wcfg = global_cfg.workers[name]

            worktree_path: Path | None = None
            branch_name: str | None = None
            if wcfg.worktree:
                worktree_path = gw.worktree_path_for(repo_path, task_number, name)
                branch_name = gw.branch_name_for(task_number, name)
                gw.add_worktree(repo_path, worktree_path, branch_name, base=pcfg.base_branch)
                created_worktrees.append(worktree_path)

            cwd = worktree_path if worktree_path else repo_path
            prompt_path = paths.prompts_dir / f"{name}.md"
            _, md = context_builder.build_context_pack(
                project_cfg=pcfg, project_name=project_name,
                worker_name=name, worker_cfg=wcfg,
                task_id=task_number, task_title=task_text[:80],
                task_description=task_text,
                worker_index=worker_index,
                worktree_path=worktree_path,
                relevant_files=relevant_files,
            )

            # All launch + run-row + tailer + stagger goes through the
            # shared seam. Parallel mode uses tmux when available so the
            # user can attach to live worker panes.
            result = spawner.spawn(spawner.SpawnRequest(
                worker_name=name, worker_cfg=wcfg,
                cwd=cwd, prompt_md=md, prompt_path=prompt_path,
                log_path=paths.logs_dir / f"{name}.log",
                task_db_id=task_db_id,
                worktree_path=worktree_path, branch_name=branch_name,
                result_path=(worktree_path or repo_path) / "RESULT.md",
                tmux_session=session if use_tmux else None,
            ))

            launched.append({
                "worker": name, "pid": result.pid, "pane": result.tmux_pane,
                "worktree": str(worktree_path) if worktree_path else None,
            })

        conn.execute(
            "UPDATE tasks SET status = 'running', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (task_db_id,),
        )
        conn.commit()

        return {
            "task_db_id": task_db_id, "task_number": task_number,
            "task_folder": str(paths.folder), "team": team,
            "session": session if use_tmux else None,
            "launched": launched, "dry_run": False,
        }
    except Exception:
        # Launch failed partway: mark the task failed and tear down any
        # worktrees we already created so they aren't orphaned.
        if task_db_id is not None:
            try:
                conn.execute(
                    "UPDATE tasks SET status = 'failed', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (task_db_id,),
                )
                conn.commit()
            except Exception:
                pass
        for wt in created_worktrees:
            try:
                gw.remove_worktree(repo_path, wt, force=True)
            except Exception:
                pass
        raise
    finally:
        conn.close()


def _dry_run_plan(repo_path, pcfg, global_cfg, task_db_id, task_number, team, paths) -> str:
    lines = [
        f"# Dry-Run Plan — task-{task_number:04d}",
        "",
        f"Project: `{pcfg.name}`  Base: `{pcfg.base_branch}`",
        f"Repo: `{repo_path}`",
        "",
        "## Team",
    ]
    for name in team:
        cfg = global_cfg.workers[name]
        lines.append(f"- **{name}** ({cfg.provider}) — role={cfg.role} can_edit={cfg.can_edit} worktree={cfg.worktree}")
        if cfg.worktree:
            wt = gw.worktree_path_for(repo_path, task_number, name)
            br = gw.branch_name_for(task_number, name)
            lines.append(f"   - would create worktree: `{wt}` on branch `{br}` from `{pcfg.base_branch}`")
    lines += [
        "",
        "## Tmux",
        f"- session: `johnstudio-task-{task_number:04d}`",
        "- one pane per worker (or subprocess fallback if tmux is absent)",
        "",
        "## No real launches performed (dry-run).",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# status / stop / cleanup / resume
# ---------------------------------------------------------------------------

def status(task_number: int, project_name: str) -> dict:
    proj = project_mod.get_project(project_name)
    if not proj:
        raise KeyError(project_name)
    conn = db.connect()
    db.init_schema(conn)
    task = conn.execute(
        "SELECT * FROM tasks WHERE project_id = ? AND task_number = ?",
        (proj["id"], task_number),
    ).fetchone()
    if not task:
        conn.close()
        raise KeyError(f"task-{task_number}")
    runs = conn.execute(
        """SELECT r.*, w.name AS worker_name FROM runs r
           JOIN workers w ON w.id = r.worker_id
           WHERE r.task_id = ? ORDER BY r.id""",
        (task["id"],),
    ).fetchall()
    conn.close()

    out_runs: list[dict] = []
    for r in runs:
        wt = r["worktree_path"]
        result_exists = bool(r["result_path"]) and Path(r["result_path"]).exists()
        done_exists = bool(wt) and (Path(wt) / "DONE.md").exists()
        out_runs.append({
            # `id` and `task_id` were added so the live-tree UI can re-seed
            # its run state when an event arrives for a run that snuck in
            # between the SSE snapshot and the last task_state emission
            # (the task_state event only fires on task.status change today —
            # 9 freshly-spawned specialists under a still-`running` task
            # would otherwise never appear in the topology).
            "id": int(r["id"]),
            "task_id": int(r["task_id"]),
            "worker": r["worker_name"], "status": r["status"],
            "branch": r["branch_name"], "worktree": wt,
            "tmux_pane": r["tmux_pane"],
            "result_md_exists": result_exists, "done_md_exists": done_exists,
        })
    return {
        "task_id": int(task["id"]),
        "task_number": task_number, "title": task["title"],
        "status": task["status"], "runs": out_runs,
    }


def stop(task_number: int, project_name: str) -> dict:
    """Kill the task's tmux session (if any) and mark active runs as stopped."""
    proj = project_mod.get_project(project_name)
    if not proj:
        raise KeyError(project_name)
    conn = db.connect()
    db.init_schema(conn)
    task = conn.execute(
        "SELECT * FROM tasks WHERE project_id = ? AND task_number = ?",
        (proj["id"], task_number),
    ).fetchone()
    if not task:
        conn.close()
        raise KeyError(f"task-{task_number}")
    session = f"johnstudio-task-{task_number:04d}"
    if tmux_controller.is_available() and tmux_controller.session_exists(session):
        tmux_controller.kill_session(session)
    conn.execute(
        "UPDATE runs SET status = 'stopped', finished_at = ? WHERE task_id = ? AND status IN ('launched','running')",
        (_now_iso(), task["id"]),
    )
    conn.execute(
        "UPDATE tasks SET status = 'stopped', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (task["id"],),
    )
    conn.commit()
    conn.close()
    return {"task_number": task_number, "session": session}


def cleanup(task_number: int, project_name: str, *, prune_worktrees: bool = False) -> dict:
    proj = project_mod.get_project(project_name)
    if not proj:
        raise KeyError(project_name)
    conn = db.connect()
    db.init_schema(conn)
    task = conn.execute(
        "SELECT * FROM tasks WHERE project_id = ? AND task_number = ?",
        (proj["id"], task_number),
    ).fetchone()
    if not task:
        conn.close()
        raise KeyError(f"task-{task_number}")
    session = f"johnstudio-task-{task_number:04d}"
    if tmux_controller.is_available() and tmux_controller.session_exists(session):
        tmux_controller.kill_session(session)
    removed: list[str] = []
    if prune_worktrees:
        runs = conn.execute(
            "SELECT worktree_path FROM runs WHERE task_id = ? AND worktree_path IS NOT NULL",
            (task["id"],),
        ).fetchall()
        for r in runs:
            wt = r["worktree_path"]
            if wt and Path(wt).exists():
                try:
                    gw.remove_worktree(proj["repo_path"], wt, force=True)
                    removed.append(wt)
                except Exception:
                    pass
    conn.close()
    return {"task_number": task_number, "removed_worktrees": removed}


def resume(task_number: int, project_name: str, worker_name: str) -> dict:
    """Create a fresh context pack from current state and re-nudge the worker.

    For terminal workers, simply rebuild the prompt; for tmux-attached workers,
    send the read-prompt instruction again.
    """
    proj = project_mod.get_project(project_name)
    if not proj:
        raise KeyError(project_name)
    pcfg = config.load_project_config(proj["repo_path"])
    global_cfg = config.load_global_config()
    if worker_name not in global_cfg.workers:
        raise KeyError(worker_name)
    wcfg = global_cfg.workers[worker_name]

    conn = db.connect()
    db.init_schema(conn)
    task = conn.execute(
        "SELECT * FROM tasks WHERE project_id = ? AND task_number = ?",
        (proj["id"], task_number),
    ).fetchone()
    if not task:
        conn.close()
        raise KeyError(f"task-{task_number}")
    run_row = conn.execute(
        """SELECT r.* FROM runs r JOIN workers w ON w.id = r.worker_id
           WHERE r.task_id = ? AND w.name = ?""",
        (task["id"], worker_name),
    ).fetchone()
    conn.close()
    if not run_row:
        raise KeyError(f"no run for {worker_name}")

    paths = _scaffold(Path(proj["repo_path"]), task_number)
    worktree_path = Path(run_row["worktree_path"]) if run_row["worktree_path"] else None
    _, md = context_builder.build_context_pack(
        project_cfg=pcfg, project_name=project_name,
        worker_name=worker_name, worker_cfg=wcfg,
        task_id=task_number, task_title=task["title"],
        task_description=task["description"],
        worktree_path=worktree_path,
    )
    prompt_path = paths.prompts_dir / f"{worker_name}.md"
    utils.write_text(prompt_path, md, overwrite=True)

    session = run_row["tmux_session"]
    pane = run_row["tmux_pane"]
    if session and pane and tmux_controller.is_available() and tmux_controller.session_exists(session):
        tmux_controller.send_keys(
            session, pane,
            f"Re-read the prompt at {prompt_path}. Continue from current state.",
        )
        return {"resumed": True, "session": session, "pane": pane, "prompt": str(prompt_path)}
    return {"resumed": False, "prompt": str(prompt_path)}


# ---------------------------------------------------------------------------

def _now_iso() -> str:
    from datetime import datetime
    return datetime.utcnow().isoformat(timespec="seconds")


def wait_for_done(repo_path: Path, task_number: int, *, timeout: float = 10.0) -> bool:
    """Helper for tests: block until every worktree under this task has a DONE.md."""
    deadline = time.time() + timeout
    base = _task_paths(repo_path, task_number)
    while time.time() < deadline:
        # Look at every run's worktree.
        # For simplicity, scan ./worktrees/task-NNNN-*.
        wts = list((repo_path / ".johnstudio" / "worktrees").glob(f"task-{task_number:04d}-*"))
        if wts and all((wt / "DONE.md").exists() for wt in wts):
            return True
        time.sleep(0.1)
    return False
