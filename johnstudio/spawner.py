"""One launch seam for every mode (parallel / chain / team).

Previously each mode had its own ~30 lines of "write prompt → build
worker → launch → insert run row → start tailer → maybe stagger,"
with subtle drift between them (parallel staggered, team didn't; chain
cleared stale DONE.md, the others didn't; cross-reviewers in team mode
forgot to pass PID until that was added on a one-off basis).

This module is the single launch path. orchestrator.run, chain.run_phase,
and team_orchestrator.spawn_and_track all funnel through `spawn()`. New
modes should too.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import db, workers
from . import worker_events
from .hooks import EventTypes, bus
from .models import WorkerConfig


INTER_LAUNCH_DELAY = 0.5  # seconds; let Claude's local IPC register before the next spawn


@dataclass
class SpawnRequest:
    worker_name: str
    worker_cfg: WorkerConfig
    cwd: Path
    prompt_md: str
    prompt_path: Path
    log_path: Path
    task_db_id: int
    worktree_path: Path | None = None
    branch_name: str | None = None
    result_path: Path | None = None
    tmux_session: str | None = None
    phase_id: int | None = None
    stagger: bool = True
    # When True, ensures a workers-table row exists for `worker_name`.
    # Parallel mode prefers to manage its own row outside this seam; team
    # mode and chain mode rely on us to upsert.
    upsert_worker: bool = True


@dataclass
class SpawnResult:
    run_id: int
    pid: int | None
    tmux_session: str | None
    tmux_pane: str | None
    log_path: Path
    jsonl_path: Path


def spawn(req: SpawnRequest, *, retry_count: int = 0) -> SpawnResult:
    """Launch a worker. Steps:

    1. Write the prompt file (with parent mkdir).
    2. Make the worker via the provider factory.
    3. Call worker.launch (which honors tmux when session is set).
    4. Upsert workers row + insert runs row carrying the PID.
    5. Start the stream-json event tailer if the provider supports it.
    6. Stagger so concurrent spawns don't hit Claude's IPC simultaneously.

    `retry_count` is the number of prior transient-failure auto-retries
    that produced this spawn; it is threaded to the exit reaper so it can
    enforce MAX_WORKER_RETRIES. Item-17 callers never set it directly —
    the reaper re-invokes `spawn` with an incremented value.
    """
    req.prompt_path.parent.mkdir(parents=True, exist_ok=True)
    req.prompt_path.write_text(req.prompt_md, encoding="utf-8")

    worker = workers.make_worker(req.worker_name, req.worker_cfg)
    handle = worker.launch(
        cwd=req.cwd, prompt_path=req.prompt_path, log_path=req.log_path,
        session=req.tmux_session,
    )

    if req.upsert_worker:
        worker_id = _upsert_worker_row(req.worker_name, req.worker_cfg)
    else:
        worker_id = _lookup_worker_id(req.worker_name)

    run_id = _insert_run(
        task_db_id=req.task_db_id, worker_id=worker_id,
        worktree_path=req.worktree_path, branch=req.branch_name,
        prompt_path=req.prompt_path, result_path=req.result_path,
        tmux_session=handle.tmux_session, tmux_pane=handle.tmux_pane,
        pid=handle.pid,
    )

    jsonl_path = worker_events.jsonl_path_for_log(req.log_path)
    if req.worker_cfg.provider in ("claude", "codex", "gemini"):
        worker_events.start_tailer(
            jsonl_path=jsonl_path,
            run_id=run_id, task_id=req.task_db_id, phase_id=req.phase_id,
            provider=req.worker_cfg.provider,
        )

    # Mark the run terminal the moment the subprocess exits, rather than
    # waiting on the watchdog's poll. Only the detached-subprocess path gives
    # us a waitable PID (tmux panes don't), so this is best-effort and a no-op
    # when we launched into tmux — the watchdog still covers that case.
    if handle.pid:
        _start_exit_reaper(
            run_id=run_id, pid=handle.pid, req=req,
            log_path=req.log_path, retry_count=retry_count,
        )

    if req.stagger:
        time.sleep(INTER_LAUNCH_DELAY)

    return SpawnResult(
        run_id=run_id, pid=handle.pid,
        tmux_session=handle.tmux_session, tmux_pane=handle.tmux_pane,
        log_path=req.log_path, jsonl_path=jsonl_path,
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but not ours — still alive.
        return True
    except Exception:
        return False


# --- Item 17: transient-failure auto-retry ---------------------------------

MAX_WORKER_RETRIES = 2          # auto-retries per worker on a transient cause
RETRY_BACKOFF_SECONDS = 3.0     # short pause before re-spawning (oom / no_progress)
RATE_LIMIT_BACKOFF_SECONDS = 60.0  # rate limits need a real wait — retrying after
                                   # 3s just re-hits the provider's throttle window

# Substrings (lowercased) marking a HARD quota exhaustion — the provider budget
# is spent and won't recover for hours (e.g. gemini "Your quota will reset after
# 16h"). Retrying is futile and just thrashes, so these are NOT retried.
_QUOTA_EXHAUSTED_MARKERS = (
    "quota_exhausted",
    "exhausted your capacity",
    "terminalquotaerror",
    "you have exhausted",
    "quota will reset",
    "insufficient_quota",
)
# Substrings (lowercased) that mark a *transient* provider failure we DO retry:
# short rate-limit / overload throttles (recover within seconds-minutes).
_RATE_LIMIT_MARKERS = (
    "rate limit",
    "rate_limit",
    "ratelimit",
    "429",
    "too many requests",
    "overloaded",
    "resource_exhausted",
    "resource exhausted",
)
_OOM_MARKERS = (
    "out of memory",
    "oom",
    "cannot allocate memory",
    "memoryerror",
    "killed",            # bare "Killed" from the OOM killer
)


def _worktree_unchanged(worktree_path, base: str = "main") -> bool:
    """True iff an editor worktree has NO uncommitted changes AND NO diff vs
    base — i.e. the worker produced nothing. Used to retry a specialist that
    exited without DONE.md having done no work (e.g. confabulated being blocked).
    Best-effort: returns False (don't retry) if git can't be queried.
    """
    wt = str(worktree_path)
    try:
        st = subprocess.run(
            ["git", "-C", wt, "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if st:
            return False  # has uncommitted changes → did something
        # Committed work counts as progress too. Count commits ahead of the base
        # so a worker that COMMITTED (clean tree, no diff-vs-main) isn't falsely
        # flagged as idle. Try the given base, then the repo's real default — if
        # none resolve, return False (don't retry; safer than retrying good work).
        for b in (base, "origin/HEAD", "main", "master"):
            cp = subprocess.run(
                ["git", "-C", wt, "rev-list", "--count", f"{b}..HEAD"],
                capture_output=True, text=True, timeout=10,
            )
            if cp.returncode == 0:
                return cp.stdout.strip() in ("", "0")  # 0 commits ahead = nothing
        return False
    except Exception:
        return False


def classify_exit(*, tail: str | None, exit_code: int | None) -> str:
    """Classify a worker exit as 'quota_exhausted', 'rate_limit', 'oom', or 'permanent'.

    Pure + side-effect-free so it is trivially unit-testable. Decision order:

    - HARD quota exhaustion FIRST (a 429 saying "quota will reset in 16h" is not
      retryable — checked before generic rate-limit so it isn't mistaken for a
      transient throttle).
    - exit code 137 (128+SIGKILL) is the canonical OOM-killer signature.
    - transient rate-limit / overload markers (retried with backoff).
    - OOM markers.
    - everything else is permanent (clean exit, code bug, assertion, exit 0)
      and must NOT be retried.
    """
    # Only scan the END of the log: the real exit error (429 body, quota notice,
    # OOM kill) is always at the tail. Scanning the whole 8KB false-matches the
    # model's own chain-of-thought ("I hit a rate limit earlier...") or a file it
    # printed that happens to contain "429"/"killed" — causing bogus retries.
    text = (tail or "").lower()[-2000:]
    for m in _QUOTA_EXHAUSTED_MARKERS:
        if m in text:
            return "quota_exhausted"
    if exit_code == 137:
        return "oom"
    for m in _RATE_LIMIT_MARKERS:
        if m in text:
            return "rate_limit"
    for m in _OOM_MARKERS:
        if m in text:
            return "oom"
    return "permanent"


def _read_tail(log_path: Path | None, *, max_bytes: int = 8192) -> str | None:
    """Read the last `max_bytes` of a worker log. Best-effort."""
    if not log_path:
        return None
    try:
        p = Path(log_path)
        if not p.exists():
            return None
        size = p.stat().st_size
        with p.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            return f.read().decode("utf-8", errors="replace")
    except Exception:
        return None


def _start_exit_reaper(
    *, run_id: int, pid: int, req: SpawnRequest, log_path: Path | None,
    retry_count: int,
) -> None:
    """Background thread: poll the worker PID and, on exit, write a terminal
    status to the runs row directly (so it doesn't depend solely on the
    watchdog). 'completed' when a DONE.md marker is present in the worktree,
    otherwise 'stopped'. Best-effort and daemonized — never blocks shutdown.

    Also (Item 20) emits WORKER_EXITED on every observed exit, and (Item 17)
    auto-retries the worker when the exit looks transient (rate-limit / OOM)
    up to MAX_WORKER_RETRIES, with a short backoff."""
    worktree_path = req.worktree_path

    def _reap() -> None:
        # Poll the PID until it's gone. Cheap kill(pid, 0) probe every second.
        while _pid_alive(pid):
            time.sleep(1.0)
        done = bool(worktree_path) and (Path(worktree_path) / "DONE.md").exists()
        tail = _read_tail(log_path)
        cause = "permanent" if done else classify_exit(tail=tail, exit_code=None)
        # An editor that exited without DONE.md AND made no changes did nothing
        # useful (e.g. confabulated being blocked). Retry it — this is the
        # safety net for the self-build's biggest failure mode.
        if (not done) and cause == "permanent" and worktree_path and _worktree_unchanged(worktree_path):
            cause = "no_progress"
        transient = (not done) and cause in ("rate_limit", "oom", "no_progress")
        can_retry = transient and retry_count < MAX_WORKER_RETRIES

        status = "completed" if done else ("retrying" if can_retry else "stopped")
        claimed = False
        try:
            conn = db.connect()
            try:
                cur = conn.execute(
                    "UPDATE runs SET status=? WHERE id=? AND status IN ('launched','running')",
                    (status, run_id),
                )
                conn.commit()
                claimed = cur.rowcount == 1
            finally:
                conn.close()
        except Exception:
            # Watchdog remains the backstop if the direct update fails.
            pass

        # If we didn't claim the run (rowcount 0), another actor already gave it a
        # terminal status — the tailer 'killed' it for a forbidden tool, or the
        # watchdog 'stopped' it. Do NOT retry, or we loop re-spawning a worker
        # that hits the same wall (the forbidden-tool kill→retry loop).
        can_retry = can_retry and claimed

        # Item 20: best-effort liveness event. Never let an emit failure
        # break the reaper (or, by extension, the spawn that started it).
        try:
            bus.emit(EventTypes.WORKER_EXITED, {
                "run_id": run_id,
                "task_id": req.task_db_id,
                "exit_code": None,
                "worker": req.worker_name,
                "cause": cause,
                "retry_count": retry_count,
                "will_retry": bool(can_retry),
            })
        except Exception:
            pass

        # Item 17: auto-retry on a transient cause.
        if can_retry:
            _retry_worker(req=req, prior_run_id=run_id, retry_count=retry_count, cause=cause)
        elif transient:
            # Exhausted retries on a transient cause — surface as a death.
            try:
                bus.emit(EventTypes.WORKER_DIED, {
                    "run_id": run_id, "task_id": req.task_db_id,
                    "worker": req.worker_name, "cause": cause,
                    "retries_exhausted": True,
                })
            except Exception:
                pass

    t = threading.Thread(target=_reap, name=f"exit-reaper(run={run_id})", daemon=True)
    t.start()


def _retry_worker(*, req: SpawnRequest, prior_run_id: int, retry_count: int, cause: str) -> None:
    """Re-spawn a worker after a transient failure, with a short backoff.

    Re-uses the SAME SpawnRequest (same prompt, worktree, branch, result
    path) so the retried worker resumes the identical assignment. The new
    spawn carries retry_count+1, so a second/third transient failure
    eventually stops retrying. Best-effort + daemonized; logged."""
    def _go() -> None:
        # Rate limits need a real wait; a 3s retry just re-hits the throttle.
        backoff = RATE_LIMIT_BACKOFF_SECONDS if cause == "rate_limit" else RETRY_BACKOFF_SECONDS
        time.sleep(backoff)
        next_count = retry_count + 1
        try:
            res = spawn(req, retry_count=next_count)
            print(
                f"spawner: auto-retried worker {req.worker_name!r} "
                f"(cause={cause}, attempt {next_count}/{MAX_WORKER_RETRIES}, "
                f"prior_run={prior_run_id} → run={res.run_id})",
                flush=True,
            )
        except Exception as e:
            print(
                f"spawner: auto-retry of {req.worker_name!r} failed: {e}",
                flush=True,
            )

    t = threading.Thread(
        target=_go, name=f"retry({req.worker_name})", daemon=True,
    )
    t.start()


def _upsert_worker_row(name: str, cfg: WorkerConfig) -> int:
    conn = db.connect()
    db.init_schema(conn)
    try:
        cur = conn.execute(
            """INSERT INTO workers (name, provider, role, command, can_edit, worktree_enabled)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET
                   provider = excluded.provider, role = excluded.role,
                   command = excluded.command, can_edit = excluded.can_edit,
                   worktree_enabled = excluded.worktree_enabled
               RETURNING id""",
            (name, cfg.provider, cfg.role or name, cfg.command,
             1 if cfg.can_edit else 0, 1 if cfg.worktree else 0),
        )
        wid = int(cur.fetchone()["id"])
        conn.commit()
        return wid
    finally:
        conn.close()


def _lookup_worker_id(name: str) -> int:
    conn = db.connect()
    try:
        row = conn.execute("SELECT id FROM workers WHERE name = ?", (name,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise RuntimeError(f"workers row for {name!r} not found (upsert_worker=False was used without a prior insert)")
    return int(row["id"])


def _insert_run(
    *, task_db_id: int, worker_id: int,
    worktree_path: Path | None, branch: str | None,
    prompt_path: Path, result_path: Path | None,
    tmux_session: str | None, tmux_pane: str | None,
    pid: int | None,
) -> int:
    from datetime import datetime
    conn = db.connect()
    db.init_schema(conn)
    try:
        cur = conn.execute(
            """INSERT INTO runs (task_id, worker_id, status, tmux_session, tmux_pane,
                worktree_path, branch_name, prompt_path, result_path, started_at, pid)
               VALUES (?,?,?,?,?,?,?,?,?,?,?) RETURNING id""",
            (
                task_db_id, worker_id, "launched",
                tmux_session, tmux_pane,
                str(worktree_path) if worktree_path else None,
                branch,
                str(prompt_path),
                str(result_path) if result_path else None,
                datetime.utcnow().isoformat(timespec="seconds"),
                pid,
            ),
        )
        rid = int(cur.fetchone()["id"])
        conn.commit()
        return rid
    finally:
        conn.close()
