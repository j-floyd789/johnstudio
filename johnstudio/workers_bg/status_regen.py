"""StatusRegenWorker — auto-regenerate project status after lifecycle events.

Today every time an arc lands an iteration or a task merges, the
operator has to remember to run two scripts in the project repo:

    python3 scripts/regen_status.py
    python3 scripts/reconcile_task_state.py --write

This worker subscribes to `arc.iter_complete` and `task.merged` and
fires both, coalescing bursts (e.g. an arc landing 5 iterations in
quick succession) into a single run thanks to a 30s throttle.

Payload resolution:
    - If `project_repo` is in the payload, use it directly.
    - Else if `project_name` is in the payload, look it up via
      `project.get_project(name)`.
    - Else if `project_id` is in the payload, look it up via the
      api/_helpers helper.
    - Else: log + skip (we don't know which repo to operate on).

Each script invocation is isolated; one failing doesn't block the other.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from ..background_workers import BackgroundWorker

_log = logging.getLogger("johnstudio.workers_bg.status_regen")


def _resolve_repo(payload: dict) -> Path | None:
    """Resolve project_repo from a hook payload. Returns None if unknown."""
    repo = payload.get("project_repo")
    if repo:
        p = Path(str(repo)).expanduser()
        return p if p.exists() else None

    name = payload.get("project_name")
    if name:
        try:
            from .. import project as _project
            proj = _project.get_project(str(name))
        except Exception:
            _log.exception("project.get_project lookup failed for %s", name)
            return None
        if proj and proj.get("repo_path"):
            p = Path(str(proj["repo_path"])).expanduser()
            return p if p.exists() else None

    pid = payload.get("project_id")
    if pid is not None:
        try:
            from ..api._helpers import get_project_by_id
            proj = get_project_by_id(int(pid))
        except Exception:
            _log.exception("get_project_by_id lookup failed for %s", pid)
            return None
        if proj and proj.get("repo_path"):
            p = Path(str(proj["repo_path"])).expanduser()
            return p if p.exists() else None

    return None


class StatusRegenWorker(BackgroundWorker):
    name = "status-regen"
    events = ["arc.iter_complete", "task.merged"]
    throttle_seconds = 30

    # Scripts (relative to the project repo) to run on every fire.
    SCRIPTS: list[list[str]] = [
        ["scripts/regen_status.py"],
        ["scripts/reconcile_task_state.py", "--write"],
    ]

    def handle(self, event: str, payload: dict) -> None:
        repo = _resolve_repo(payload)
        if repo is None:
            _log.info(
                "status-regen: no repo resolvable from payload keys=%s; skipping",
                sorted(payload.keys()),
            )
            return

        errors: list[str] = []
        for argv in self.SCRIPTS:
            script_path = repo / argv[0]
            if not script_path.exists():
                # Project doesn't ship that script — perfectly fine, skip.
                _log.debug("status-regen: %s missing in %s; skipping", argv[0], repo)
                continue
            cmd = ["python3"] + argv
            try:
                cp = subprocess.run(
                    cmd, cwd=str(repo), capture_output=True, text=True, timeout=120,
                )
                if cp.returncode != 0:
                    errors.append(
                        f"{argv[0]} exit={cp.returncode} stderr={cp.stderr.strip()[:200]}"
                    )
            except subprocess.TimeoutExpired:
                errors.append(f"{argv[0]} timed out")
            except Exception as e:
                errors.append(f"{argv[0]} raised {type(e).__name__}: {e}")

        if errors:
            # Raise so the framework records this run as failed; another
            # event will retry. (Don't crash the bus — handle() exceptions
            # are caught by BackgroundWorker._run_loop.)
            raise RuntimeError("; ".join(errors))
