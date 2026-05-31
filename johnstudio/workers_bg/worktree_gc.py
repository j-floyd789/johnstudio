"""WorktreeGCWorker — garbage-collect stale git worktrees after a merge.

Every approved task gets its own git worktree under
`<repo>/.johnstudio/worktrees/task-NNNN-<worker>` (see
`..git_worktree.worktree_path_for`). Once a task's branch is merged the
worktree is dead weight: it pins a branch, holds a checkout on disk, and
shows up forever in `git worktree list` until someone prunes it. Today an
operator has to remember to clean those up by hand.

This worker fires on `task.merged`. From the payload it reads
`project_repo` (resolved via the same helper the sibling workers use) and
`task_number`. It then:

  1. Removes every worktree directory matching `task-NNNN-*` for the
     merged task via `git worktree remove --force <dir>` (falling back to
     a direct rmtree if git can't, e.g. the directory is already
     detached from git's records).
  2. Always runs `git worktree prune` to clear any stale administrative
     records git is still tracking — even when no matching directory
     existed (idempotent: a re-delivered event, or a task whose worktree
     was already cleaned, still leaves the repo in a tidy state).

Like the other workers, a failure is raised so the framework records the
run as failed; `BackgroundWorker._invoke` catches it so the bus and
sibling workers are never affected.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from ..background_workers import BackgroundWorker
from .status_regen import _resolve_repo

_log = logging.getLogger("johnstudio.workers_bg.worktree_gc")


class WorktreeGCWorker(BackgroundWorker):
    name = "worktree-gc"
    events = ["task.merged"]
    throttle_seconds = 10

    def handle(self, event: str, payload: dict) -> None:
        repo = _resolve_repo(payload)
        if repo is None:
            _log.info(
                "worktree-gc: no repo resolvable from payload keys=%s; skipping",
                sorted(payload.keys()),
            )
            return

        errors: list[str] = []

        # 1. Remove worktree dirs belonging to the merged task.
        task_number = payload.get("task_number")
        if task_number is not None:
            try:
                prefix = f"task-{int(task_number):04d}-"
            except (TypeError, ValueError):
                prefix = None
                _log.info(
                    "worktree-gc: non-integer task_number=%r; skipping dir removal",
                    task_number,
                )
            if prefix:
                errors.extend(self._remove_task_worktrees(repo, prefix))
        else:
            _log.debug("worktree-gc: payload has no task_number; prune-only run")

        # 2. Always prune stale administrative records.
        try:
            cp = subprocess.run(
                ["git", "worktree", "prune"],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=60,
            )
            if cp.returncode != 0:
                errors.append(
                    f"git worktree prune exit={cp.returncode} "
                    f"stderr={(cp.stderr or '').strip()[:200]}"
                )
        except subprocess.TimeoutExpired:
            errors.append("git worktree prune timed out")
        except Exception as e:
            errors.append(f"git worktree prune raised {type(e).__name__}: {e}")

        if errors:
            raise RuntimeError("; ".join(errors))

    def _remove_task_worktrees(self, repo: Path, prefix: str) -> list[str]:
        """Remove every `<repo>/.johnstudio/worktrees/<prefix>*` directory.

        Returns a list of error strings (empty on full success).
        """
        errors: list[str] = []
        wt_root = repo / ".johnstudio" / "worktrees"
        if not wt_root.is_dir():
            # Nothing was ever spawned for this repo — perfectly fine.
            return errors

        for child in sorted(wt_root.iterdir()):
            if not child.is_dir():
                continue
            if not child.name.startswith(prefix):
                continue
            try:
                cp = subprocess.run(
                    ["git", "worktree", "remove", "--force", str(child)],
                    cwd=str(repo),
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            except subprocess.TimeoutExpired:
                errors.append(f"git worktree remove {child.name} timed out")
                continue
            except Exception as e:
                errors.append(
                    f"git worktree remove {child.name} raised "
                    f"{type(e).__name__}: {e}"
                )
                continue

            if cp.returncode == 0:
                continue

            # git couldn't remove it (commonly: the dir is no longer a
            # registered worktree, or the repo metadata is gone). Fall back
            # to a plain rmtree so disk space is reclaimed regardless. The
            # subsequent `git worktree prune` clears any dangling record.
            # RECONSTRUCTED: the rmtree fallback is inferred, not recovered;
            # the original may have surfaced the git error instead. Tests
            # only exercise the success path (git returns 0).
            try:
                shutil.rmtree(child, ignore_errors=True)
                _log.debug(
                    "worktree-gc: git remove failed for %s (%s); rmtree'd directly",
                    child.name,
                    (cp.stderr or "").strip()[:120],
                )
            except Exception as e:
                errors.append(
                    f"rmtree {child.name} after git-remove failure raised "
                    f"{type(e).__name__}: {e}"
                )
        return errors
