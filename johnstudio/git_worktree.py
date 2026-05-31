"""Transparent git worktree wrappers via subprocess. No GitPython."""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import utils


# Repos with multiple concurrent worktrees (Claude Code + JohnStudio + user)
# can hit "mmap failed: Resource deadlock avoided" — POSIX fcntl deadlock
# detection when more than one git process touches shared .git state at
# once. Three layers of defense:
# 1. GIT_OPTIONAL_LOCKS=0 in env tells git to skip optional sub-locks
#    (refs snapshotting, fsmonitor) that are the actual deadlock source.
#    Documented in git-config(1) under `core.optionalLocks`.
# 2. A process-wide threading lock serializes OUR git writes so the
#    team-mode fan-out never races itself.
# 3. Per-command retry with backoff absorbs races against EXTERNAL git
#    processes (Claude Code's own worktrees) that we can't control.
_GIT_WRITE_LOCK = threading.Lock()
# Patterns that indicate a CONCURRENT-ACCESS failure rather than a real
# problem. We retry these with backoff. On macOS specifically, when another
# git process atomically rewrites the index file (rename-over), our mmap'd
# pages SIGBUS and the operation dies with "died of signal 10" — that's not
# permanent, the next attempt usually succeeds.
_TRANSIENT_ERRORS = (
    "Resource deadlock",
    "index.lock",
    "Unable to create",
    "died of signal 10",   # SIGBUS — concurrent rename-over of the mmap'd index
    "died of signal 7",    # SIGBUS on Linux (different signal number)
    "died of signal 11",   # SIGSEGV from mmap region invalidation
    "could not lock",
    "Another git process",
)


def _is_transient(stderr: str) -> bool:
    return any(t in stderr for t in _TRANSIENT_ERRORS)


def _git_env() -> dict:
    env = os.environ.copy()
    # Suppress fcntl deadlock against concurrent external worktrees.
    env["GIT_OPTIONAL_LOCKS"] = "0"
    # Disable git's internal fsmonitor daemon — its mmap'd state file is
    # another deadlock surface on macOS APFS when something else heavy
    # (pandas read_csv, npm install) is mmaping unrelated files at the
    # same time.
    env["GIT_FSMONITOR"] = "false"
    return env


def _git_low_io_args() -> list[str]:
    """Per-invocation `-c k=v` config to minimise concurrent mmap surface.
    Used on worktree-add / branch-D / prune; not on read-only `worktree list`."""
    return [
        "-c", "core.fsmonitor=false",
        "-c", "index.threads=1",   # serialise index reads, less concurrent mmap
        "-c", "pack.threads=1",
        "-c", "core.preloadIndex=false",
    ]


def _wait_for_quiet_disk(max_wait_s: int = 300, threshold_mbs: float = 25.0) -> float:
    """Block until macOS disk0 throughput drops below `threshold_mbs` MB/s
    sustained over two consecutive samples, or `max_wait_s` elapses.

    Returns the actual wait time in seconds. On any sampling error or non-
    macOS platform we return 0 immediately — callers must still rely on
    retry-with-backoff for the deadlock case.

    Rationale: macOS APFS fires `Resource deadlock avoided` when concurrent
    mmap+fcntl from unrelated processes overlap. Best-effort dodge: wait for
    the disk to be quiet first, then proceed."""
    import time as _t
    start = _t.time()
    try:
        prev_mbs = None
        while _t.time() - start < max_wait_s:
            # `iostat -d disk0 -c 1` prints a one-shot sample. The 3rd column is MB/s.
            cp = subprocess.run(
                ["iostat", "-d", "disk0", "-c", "1"],
                text=True, capture_output=True, timeout=3,
            )
            if cp.returncode != 0:
                return 0.0
            last = cp.stdout.strip().splitlines()[-1].split()
            mbs = float(last[2]) if len(last) >= 3 else 0.0
            if mbs < threshold_mbs and (prev_mbs is None or prev_mbs < threshold_mbs):
                return _t.time() - start
            prev_mbs = mbs
            _t.sleep(2)
        return _t.time() - start
    except Exception:
        return 0.0


# Default wall-clock ceiling for any single git invocation. A corrupted
# `.git/worktrees/` entry can make plumbing like `git worktree list` hang
# forever on a stale lock; without a timeout the whole server wedges.
_GIT_TIMEOUT = 30
# `git worktree list` is read-only and quick; give it a short leash so a
# corrupt worktree record fails fast instead of stalling startup.
_GIT_LIST_TIMEOUT = 10


def _run_git(cmd: list[str], *, timeout: int = _GIT_TIMEOUT) -> subprocess.CompletedProcess:
    """Run a git command with GIT_OPTIONAL_LOCKS=0 to suppress the
    fcntl deadlock against concurrent external worktrees.

    Always bounded by `timeout`; on expiry we raise a clear error rather than
    hang (a corrupt `.git/worktrees/` entry can otherwise block forever)."""
    try:
        return subprocess.run(
            cmd, env=_git_env(), text=True, capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"git command timed out after {timeout}s: {' '.join(cmd)} "
            "(possible .git/worktrees corruption — try prune_worktrees)"
        ) from e


def _run_with_retry(
    cmd: list[str], *, max_retries: int = 6, base_sleep: float = 0.5,
    timeout: int = _GIT_TIMEOUT,
):
    """Run `cmd`, retry exponentially on transient git errors. Returns the
    final CompletedProcess. Each attempt is bounded by `timeout`.

    Special-cases macOS APFS "Resource deadlock avoided": that's a kernel-level
    fcntl deadlock that needs SECONDS to clear (not milliseconds). Standard
    exponential backoff (0.5*2^n) caps too low. For that error specifically we
    bump to a baseline of 8s with linear growth (8, 16, 24, 32, 40, 48, 60s)
    so 7 attempts span ~3.5 minutes — enough for sibling git/file ops to drain.
    """
    last = None
    for attempt in range(max_retries + 1):
        cp = _run_git(cmd, timeout=timeout)
        if cp.returncode == 0:
            return cp
        if not _is_transient(cp.stderr or ""):
            return cp
        last = cp
        stderr = cp.stderr or ""
        if "Resource deadlock" in stderr:
            # APFS deadlock: longer linear waits, capped at 60s.
            sleep_s = min(60.0, 8.0 * (attempt + 1))
        else:
            sleep_s = base_sleep * (2 ** attempt)
        time.sleep(sleep_s)
    return last


def add_worktree(repo_path: str | Path, worktree_path: str | Path, branch: str, base: str = "main") -> None:
    """Create a worktree at `worktree_path` checked out to `branch`.

    Robust against three real failure modes:
    1. **Branch already exists** from a prior partial Approve. Two retries:
       (a) reuse the existing branch via `worktree add <path> <branch>`;
       (b) if that also fails, delete the branch and try `-b` again. The
       latter handles the case where a previous Approve created the branch
       but the worktree-add died mid-way.
    2. **mmap / index.lock contention** from a concurrent git process
       elsewhere on the same repo (very common when Claude Code has its
       own worktrees in `.claude/worktrees/`). Each individual `git`
       invocation is retried up to 4 times with exponential backoff.
    3. **Concurrent JohnStudio spawns** within this process. All write
       operations take a thread-level lock so the team-mode fan-out
       never races itself.
    """
    repo = str(repo_path)
    wt = str(worktree_path)
    Path(worktree_path).parent.mkdir(parents=True, exist_ok=True)

    with _GIT_WRITE_LOCK:
        # Best-effort: wait for the disk to be quiet before attempting the
        # worktree-add. Dodges most APFS deadlocks vs concurrent unrelated
        # mmap-heavy processes (pandas read_csv, npm install). Caps at 5 min
        # — if disk stays busy longer we go ahead and rely on retry.
        _wait_for_quiet_disk(max_wait_s=300, threshold_mbs=25.0)
        lowio = _git_low_io_args()
        # Attempt 1: fresh branch with -b (fails fast if branch already exists,
        # which is fine — most spawns hit this happy path with no contention).
        cp = _run_with_retry(["git", "-C", repo, *lowio, "worktree", "add", wt, "-b", branch, base])
        if cp.returncode == 0:
            _link_node_modules(repo, wt)
            return
        first_err = (cp.stderr or cp.stdout or "").strip()

        # Attempt 2: -B (capital) creates OR force-resets the branch to `base`.
        # This is the right tool when the branch exists from a prior partial
        # spawn but no worktree currently references it. Safe because git
        # refuses -B against a branch a worktree owns — so if some other
        # worktree is using it we get a clean error instead of silent damage.
        cp2 = _run_with_retry(["git", "-C", repo, *lowio, "worktree", "add", "-B", branch, wt, base])
        if cp2.returncode == 0:
            _link_node_modules(repo, wt)
            return
        second_err = (cp2.stderr or cp2.stdout or "").strip()

        # Attempt 3: explicit prune + retried branch delete + fresh -b. The
        # prune clears any stale worktree records git is still tracking; the
        # branch -D goes through _run_with_retry so we don't lose it to a
        # transient mmap deadlock the way a one-shot call would.
        _run_with_retry(["git", "-C", repo, "worktree", "prune"])
        del_cp = _run_with_retry(["git", "-C", repo, "branch", "-D", branch])
        # If the delete also failed (still mmap-deadlocked after retries),
        # don't waste a third attempt on -b — it will just say "already
        # exists" again. Surface the real error instead.
        if del_cp.returncode != 0 and "not found" not in (del_cp.stderr or ""):
            raise RuntimeError(
                "git worktree add failed (branch delete blocked):\n"
                f"  1) {first_err}\n"
                f"  2) {second_err}\n"
                f"  branch -D {branch}: {(del_cp.stderr or del_cp.stdout or '').strip()}"
            )
        cp3 = _run_with_retry(["git", "-C", repo, *lowio, "worktree", "add", wt, "-b", branch, base])
        if cp3.returncode == 0:
            _link_node_modules(repo, wt)
            return
        third_err = (cp3.stderr or cp3.stdout or "").strip()

        # All three attempts failed — a leaked `.git/worktrees/<name>` record
        # is a common culprit. Prune so the next spawn starts clean.
        try:
            prune_worktrees(repo)
        except Exception:
            pass
        raise RuntimeError(
            "git worktree add failed after three attempts:\n"
            f"  1) {first_err}\n"
            f"  2) {second_err}\n"
            f"  3) {third_err}"
        )


def _link_node_modules(repo: str, wt: str) -> None:
    """Symlink <worktree>/node_modules -> <repo>/node_modules so each worktree
    reuses the base repo's installed deps instead of duplicating gigabytes on
    disk (and so `npm install` in the worktree is a near-noop). Best-effort:
    never fails worktree creation. Only links when the repo actually has a
    node_modules and the worktree doesn't already have one."""
    try:
        repo_nm = Path(repo) / "node_modules"
        wt_nm = Path(wt) / "node_modules"
        if not repo_nm.is_dir():
            return
        if wt_nm.exists() or wt_nm.is_symlink():
            return
        wt_nm.symlink_to(repo_nm, target_is_directory=True)
    except OSError:
        # Symlinks may be unsupported / racing another spawn — ignore.
        pass


def remove_worktree(repo_path: str | Path, worktree_path: str | Path, *, force: bool = False) -> None:
    args = ["git", "-C", str(repo_path), "worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(worktree_path))
    cp = utils.run(args, timeout=_GIT_TIMEOUT)
    if cp.returncode != 0:
        # The worktree record may be leaked/corrupt — prune so a stale entry
        # auto-cleans, then surface the original error.
        try:
            prune_worktrees(repo_path)
        except Exception:
            pass
        raise RuntimeError(f"git worktree remove failed: {cp.stderr or cp.stdout}")


def list_worktrees(repo_path: str | Path) -> list[dict]:
    # Short timeout: a corrupt `.git/worktrees/` entry can hang this forever.
    cp = utils.run(
        ["git", "-C", str(repo_path), "worktree", "list", "--porcelain"],
        timeout=_GIT_LIST_TIMEOUT,
    )
    if cp.returncode != 0:
        return []
    out: list[dict] = []
    cur: dict = {}
    for line in cp.stdout.splitlines():
        if not line.strip():
            if cur:
                out.append(cur)
                cur = {}
            continue
        if " " in line:
            k, v = line.split(" ", 1)
            cur[k] = v
        else:
            cur[line] = True
    if cur:
        out.append(cur)
    return out


def prune_worktrees(repo_path: str | Path) -> dict:
    """Run `git worktree prune` and reap leaked `.git/worktrees/<name>` records
    whose working directory no longer exists.

    `git worktree prune` already removes administrative entries for missing
    working trees, but a record can be left wedged if its `gitdir` file points
    at a path that's gone while git still considers it 'locked' or partially
    written. We belt-and-suspenders it by reading each entry's `gitdir` and
    removing the record dir when the target is absent.

    Returns a summary dict: {"pruned_ok": bool, "removed": [names]}.
    """
    repo = str(repo_path)
    removed: list[str] = []
    cp = _run_with_retry(
        ["git", "-C", repo, "worktree", "prune"], timeout=_GIT_TIMEOUT,
    )
    pruned_ok = bool(cp and cp.returncode == 0)

    # Resolve the real .git dir (worktree records live under <gitdir>/worktrees).
    wt_root = _git_worktrees_dir(repo)
    if wt_root and wt_root.is_dir():
        for entry in wt_root.iterdir():
            if not entry.is_dir():
                continue
            gitdir_file = entry / "gitdir"
            try:
                target = utils.read_text_retry(gitdir_file).strip()
            except OSError:
                continue
            if not target:
                continue
            # `gitdir` points at the worktree's `.git` file; its parent is the
            # actual working dir. If that's gone, the record is leaked.
            work_dir = Path(target).parent
            if not work_dir.exists():
                try:
                    shutil.rmtree(entry)
                    removed.append(entry.name)
                except OSError:
                    pass

    return {"pruned_ok": pruned_ok, "removed": removed}


def _git_worktrees_dir(repo_path: str | Path) -> Path | None:
    """Return `<git-common-dir>/worktrees` for `repo_path`, or None on error."""
    cp = utils.run(
        ["git", "-C", str(repo_path), "rev-parse", "--git-common-dir"],
        timeout=_GIT_LIST_TIMEOUT,
    )
    if cp.returncode != 0:
        return None
    common = cp.stdout.strip()
    if not common:
        return None
    common_path = Path(common)
    if not common_path.is_absolute():
        common_path = (Path(repo_path) / common_path).resolve()
    return common_path / "worktrees"


def worktree_health_check(repo_path: str | Path) -> dict:
    """Verify `git worktree list` responds under a short timeout; if it hangs
    or errors, self-heal by pruning + removing stale records.

    Intended to be called at server startup so a corrupt `.git/worktrees/`
    left by a crashed prior run doesn't wedge the first worktree operation.

    Returns {"healthy": bool, "recovered": bool, "removed": [names], "error": str|None}.
    """
    try:
        cp = utils.run(
            ["git", "-C", str(repo_path), "worktree", "list", "--porcelain"],
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        cp = None
    except Exception as e:  # pragma: no cover - defensive
        cp = None
        _ = e

    if cp is not None and cp.returncode == 0:
        return {"healthy": True, "recovered": False, "removed": [], "error": None}

    err = None if cp is None else (cp.stderr or cp.stdout or "").strip()
    try:
        summary = prune_worktrees(repo_path)
        return {
            "healthy": False,
            "recovered": True,
            "removed": summary.get("removed", []),
            "error": err or "git worktree list timed out",
        }
    except Exception as e:
        return {
            "healthy": False,
            "recovered": False,
            "removed": [],
            "error": f"{err or 'timeout'}; self-heal failed: {e}",
        }


def status(repo_path: str | Path) -> str:
    cp = utils.run(["git", "-C", str(repo_path), "status", "--short"], timeout=_GIT_TIMEOUT)
    return cp.stdout


def diff_against(repo_path: str | Path, base: str = "main") -> str:
    cp = utils.run(["git", "-C", str(repo_path), "diff", f"{base}...HEAD"], timeout=_GIT_TIMEOUT)
    return cp.stdout


def diff_stat(repo_path: str | Path, base: str = "main") -> str:
    cp = utils.run(["git", "-C", str(repo_path), "diff", "--stat", f"{base}...HEAD"], timeout=_GIT_TIMEOUT)
    return cp.stdout


def current_branch(repo_path: str | Path) -> str:
    cp = utils.run(["git", "-C", str(repo_path), "rev-parse", "--abbrev-ref", "HEAD"], timeout=_GIT_TIMEOUT)
    return cp.stdout.strip()


def commits_ahead(repo_path: str | Path, base: str = "main") -> tuple[int, str]:
    """Return (count, head_sha) of commits on HEAD not in `base`.

    Detects that a worker actually committed work in its worktree (powers
    the git.committed event). Best-effort: returns (0, "") if the range
    can't be resolved (e.g. base missing).
    """
    try:
        cnt = utils.run(
            ["git", "-C", str(repo_path), "rev-list", "--count", f"{base}..HEAD"],
            timeout=_GIT_TIMEOUT,
        ).stdout.strip()
        sha = utils.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"], timeout=_GIT_TIMEOUT
        ).stdout.strip()
        return int(cnt or "0"), sha
    except (ValueError, RuntimeError, OSError):
        return 0, ""


def is_clean(repo_path: str | Path, *, include_untracked: bool = False) -> bool:
    """Return True iff the working tree has no modifications.

    By default ignores untracked files — they cannot conflict with a merge,
    and JohnStudio's own `.johnstudio/` scaffolding is intentionally untracked.
    """
    args = ["git", "-C", str(repo_path), "status", "--porcelain"]
    if not include_untracked:
        args.append("-uno")
    cp = utils.run(args, timeout=_GIT_TIMEOUT)
    return cp.stdout.strip() == ""


def merge_branch(
    repo_path: str | Path, branch: str, *, no_ff: bool = True, dry_run: bool = False
) -> tuple[int, str]:
    """Merge `branch` into the current branch. Returns (exit_code, output)."""
    args = ["git", "-C", str(repo_path), "merge"]
    if no_ff:
        args.append("--no-ff")
    if dry_run:
        # Git's --no-commit is closer to a dry run; pair with --no-ff for visibility.
        args.append("--no-commit")
    args.append(branch)
    cp = utils.run(args, timeout=_GIT_TIMEOUT)
    out = (cp.stdout or "") + (cp.stderr or "")
    if dry_run and cp.returncode == 0:
        # Abort the partial merge cleanly so working tree stays untouched.
        utils.run(["git", "-C", str(repo_path), "merge", "--abort"], timeout=_GIT_TIMEOUT)
    return cp.returncode, out


def checkout(repo_path: str | Path, branch: str) -> None:
    cp = utils.run(["git", "-C", str(repo_path), "checkout", branch], timeout=_GIT_TIMEOUT)
    if cp.returncode != 0:
        raise RuntimeError(f"git checkout {branch} failed: {cp.stderr or cp.stdout}")


def branch_name_for(task_id: int, worker_name: str) -> str:
    return f"ai/task-{task_id:04d}/{worker_name.replace('_', '-')}"


def worktree_path_for(repo_path: str | Path, task_id: int, worker_name: str) -> Path:
    return (
        Path(repo_path) / ".johnstudio" / "worktrees" /
        f"task-{task_id:04d}-{worker_name.replace('_', '-')}"
    )
