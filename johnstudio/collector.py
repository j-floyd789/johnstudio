"""Collector: gathers task results, diffs, tests, and safety flags."""
from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

from . import (
    config,
    db,
    git_worktree as gw,
    project as project_mod,
    safety,
    tmux_controller,
    utils,
)
from .hooks import EventTypes, bus


def collect(task_number: int, project_name: str) -> dict:
    """Run collection over every run of the task. Returns a summary dict."""
    proj = project_mod.get_project(project_name)
    if not proj:
        raise KeyError(project_name)
    pcfg = config.load_project_config(proj["repo_path"])
    global_cfg = config.load_global_config()

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

    repo_path = Path(proj["repo_path"])
    base_branch = pcfg.base_branch
    task_folder = repo_path / ".johnstudio" / "tasks" / f"task-{task_number:04d}"
    (task_folder / "results").mkdir(parents=True, exist_ok=True)
    (task_folder / "diffs").mkdir(parents=True, exist_ok=True)
    (task_folder / "test_results").mkdir(parents=True, exist_ok=True)
    (task_folder / "logs").mkdir(parents=True, exist_ok=True)

    summary: list[dict] = []
    for r in runs:
        worker_name = r["worker_name"]
        # Each run is isolated and committed independently: a failure collecting
        # one worker must not discard the diffs/test rows already gathered for
        # the others, nor leave the connection open.
        try:
            _collect_one_run(
                r, worker_name, conn, task, pcfg, global_cfg,
                base_branch, task_folder, summary,
            )
            conn.commit()
        except Exception as e:  # noqa: BLE001 — collection must be best-effort per run
            try:
                conn.rollback()
            except Exception:
                pass
            summary.append({"worker": worker_name, "error": f"collect failed: {e}"})

    try:
        conn.execute(
            "UPDATE tasks SET status = 'collected', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (task["id"],),
        )
        conn.commit()
    finally:
        conn.close()
    return {"task_number": task_number, "runs": summary}


def _collect_one_run(r, worker_name, conn, task, pcfg, global_cfg,
                     base_branch, task_folder, summary) -> None:
        wt = Path(r["worktree_path"]) if r["worktree_path"] else None

        # 1. RESULT.md → copy into task/results/
        result_text = ""
        if wt and (wt / "RESULT.md").exists():
            result_text = (wt / "RESULT.md").read_text(encoding="utf-8")
            (task_folder / "results" / f"{worker_name}_RESULT.md").write_text(result_text, encoding="utf-8")
        done_present = bool(wt) and (wt / "DONE.md").exists()

        # 2. tmux capture → logs/<worker>.log (append)
        if r["tmux_session"] and r["tmux_pane"] and tmux_controller.is_available():
            cap = tmux_controller.capture_pane(r["tmux_session"], r["tmux_pane"])
            with (task_folder / "logs" / f"{worker_name}.log").open("a", encoding="utf-8") as f:
                f.write("\n--- captured " + utils.run(["date", "-u", "+%FT%TZ"]).stdout.strip() + " ---\n")
                f.write(cap)

        # 3. git diff
        diff_text = ""
        changed_files: list[str] = []
        if wt and wt.exists():
            diff_text = gw.diff_against(wt, base=base_branch)
            (task_folder / "diffs" / f"{worker_name}.diff").write_text(diff_text, encoding="utf-8")
            changed_files = safety.extract_changed_files_from_diff(diff_text)
            # If diff was empty (only untracked files), fall back to git status.
            if not changed_files:
                status_out = gw.status(wt)
                names = set()
                for line in status_out.splitlines():
                    if not line.strip():
                        continue
                    name = line[3:].strip()
                    # Renames/copies are "R  old -> new"; record the destination
                    # so the protected-path safety scan sees the real file.
                    if " -> " in name:
                        name = name.split(" -> ", 1)[1].strip()
                    names.add(name)
                changed_files = sorted(names)
            stat = gw.diff_stat(wt, base=base_branch)
            conn.execute(
                """INSERT INTO diffs (task_id, worker_id, diff_path, files_changed_json, stats_json)
                   VALUES (?,?,?,?,?)""",
                (task["id"], r["worker_id"],
                 str(task_folder / "diffs" / f"{worker_name}.diff"),
                 json.dumps(changed_files), json.dumps({"stat": stat})),
            )
            # git.committed (item 20): emit when the worker actually committed
            # work in its worktree, so consumers can react to landed commits.
            n_commits, head_sha = gw.commits_ahead(wt, base=base_branch)
            if n_commits > 0:
                try:
                    bus.emit(EventTypes.GIT_COMMITTED, {
                        "task_id": task["id"], "worker": worker_name,
                        "commits_ahead": n_commits, "head_sha": head_sha,
                    })
                except Exception:
                    pass

        # 4. tests (only for edit-capable workers with a worktree)
        test_outputs: list[dict] = []
        if wt and wt.exists() and pcfg.test_commands:
            for tc in pcfg.test_commands:
                out_path = task_folder / "test_results" / f"{worker_name}_{utils.slugify(tc)}.txt"
                try:
                    cp = subprocess.run(
                        shlex.split(tc), cwd=str(wt), shell=False, text=True,
                        capture_output=True, timeout=120,
                    )
                    out_path.write_text(
                        f"$ {tc}\nexit={cp.returncode}\n--- stdout ---\n{cp.stdout}\n--- stderr ---\n{cp.stderr}\n",
                        encoding="utf-8",
                    )
                    conn.execute(
                        """INSERT INTO test_results (task_id, worker_id, command, exit_code, output_path)
                           VALUES (?,?,?,?,?)""",
                        (task["id"], r["worker_id"], tc, cp.returncode, str(out_path)),
                    )
                    test_outputs.append({"command": tc, "exit_code": cp.returncode})
                except subprocess.TimeoutExpired:
                    out_path.write_text(f"$ {tc}\ntimeout\n", encoding="utf-8")
                    test_outputs.append({"command": tc, "exit_code": -1, "timeout": True})

        # 5. safety scans
        protected = safety.scan_protected_paths_in_files(changed_files, global_cfg.safety.blocked_paths)
        dangerous = safety.scan_text_for_dangerous_commands(
            result_text + "\n" + diff_text, global_cfg.safety.dangerous_commands
        )
        approval_needed = safety.scan_text_for_approval_commands(
            result_text + "\n" + diff_text, global_cfg.safety.require_approval_commands
        )

        # 5b. Shared artifacts: workers write their structured candidate JSON
        # into the per-task shared dir (NOT their private branch), so siblings
        # + the synthesizer can read across worktrees. Surface anything found
        # there so it isn't missed — independent of this worker's worktree.
        shared_dir = task_folder / "shared_artifacts"
        shared_artifacts: list[str] = []
        if shared_dir.is_dir():
            shared_artifacts = sorted(
                str(p) for p in shared_dir.iterdir() if p.is_file()
            )

        # Mark run completed when DONE.md exists.
        if done_present:
            conn.execute(
                "UPDATE runs SET status = 'completed', finished_at = CURRENT_TIMESTAMP WHERE id = ?",
                (r["id"],),
            )

        summary.append({
            "worker": worker_name,
            "result_present": bool(result_text),
            "done_present": done_present,
            "files_changed": changed_files,
            "shared_artifacts": shared_artifacts,
            "tests": test_outputs,
            "protected_path_hits": protected,
            "dangerous_command_hits": dangerous,
            "approval_command_hits": approval_needed,
        })
