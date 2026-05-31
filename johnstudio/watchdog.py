"""Watchdog daemon — detects stuck specialists.

Two failure modes it catches:

1. PID-dead-DB-says-alive: a run is marked status='launched' but its
   PID no longer exists. Common cause: specialist's parent shell died
   without the orchestrator getting an exit signal. Mark stopped.

2. Idle specialist: a run has been status='launched'/'running' for
   more than `idle_minutes` minutes (default 10) without emitting ANY
   worker_event in that window. The specialist is wedged — kill its
   PID (if still alive), mark its run failed, log the event.

Runs as a separate daemon process. Re-checks every `poll_seconds`
(default 60). Designed to be cheap and safe — at most one SQL update
per stuck run, no cascading actions.
"""
from __future__ import annotations

import os
import signal
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(ts: str) -> datetime | None:
    """Parse a worker_events.ts string. Returns timezone-aware UTC."""
    if not ts:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(ts.replace("Z", ""), fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _terminate(pid) -> None:
    """SIGTERM the worker's whole process GROUP. Workers run as
    `sh -c 'cat | claude | tee'` in their own session (start_new_session), so
    SIGTERM to just the sh pid orphans the model child (it keeps running and
    burning provider quota). Killing the group reaches it. Guarded by liveness
    to avoid signalling a reused PID.
    """
    try:
        if not _pid_alive(int(pid)):
            return
        os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    except Exception:
        try:
            os.kill(int(pid), signal.SIGTERM)
        except Exception:
            pass


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except Exception:
        return False


def tick(*, idle_minutes: int = 10, max_runtime_minutes: int = 15) -> dict:
    """Run one watchdog pass. Returns a summary dict."""
    now = _utc_now()
    idle_threshold = now - timedelta(minutes=idle_minutes)
    runtime_threshold = now - timedelta(minutes=max_runtime_minutes)
    reaped_dead_pid = 0
    reaped_idle = 0
    reaped_runtime = 0

    conn = db.connect()
    try:
        cur = conn.execute(
            "SELECT id, pid, started_at, worktree_path FROM runs "
            "WHERE status IN ('launched','running')"
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    for r in rows:
        run_id = int(r["id"])
        pid = r["pid"]

        # 1. PID-dead reap.
        if pid and not _pid_alive(pid):
            conn = db.connect()
            try:
                conn.execute(
                    "UPDATE runs SET status='stopped' WHERE id=? AND status IN ('launched','running')",
                    (run_id,),
                )
                conn.commit()
                reaped_dead_pid += 1
            finally:
                conn.close()
            continue

        # 1.5 Max wall-clock — a worker still ACTIVE past the runtime budget is
        # force-concluded so the task converges instead of hanging on a slow
        # worker. We write a synthetic DONE.md into its worktree so its partial
        # (already real) work is collected, then terminate it; the exit-reaper
        # sees DONE.md and marks it completed.
        started = _parse_ts(r["started_at"]) if r["started_at"] else None
        if started is not None and started < runtime_threshold:
            wt = r["worktree_path"]
            if wt and Path(wt).exists() and not (Path(wt) / "DONE.md").exists():
                try:
                    (Path(wt) / "DONE.md").write_text(
                        "status: COMPLETE\n"
                        f"note: watchdog concluded at max runtime ({max_runtime_minutes}min); "
                        "partial work collected\n",
                        encoding="utf-8",
                    )
                except Exception:
                    pass
            if pid:
                try:
                    _terminate(pid)
                except Exception:
                    pass
            conn = db.connect()
            try:
                conn.execute(
                    "UPDATE runs SET status='stopped' WHERE id=? AND status IN ('launched','running')",
                    (run_id,),
                )
                conn.execute(
                    """INSERT INTO worker_events (run_id, task_id, phase_id, seq, ts, kind, summary, raw_json)
                       SELECT id, task_id, NULL, 999998, ?, 'error',
                              'watchdog: hit max runtime ' || ? || 'min, concluded', '{}'
                       FROM runs WHERE id=?""",
                    (now.strftime("%Y-%m-%dT%H:%M:%S"), max_runtime_minutes, run_id),
                )
                conn.commit()
                reaped_runtime += 1
            finally:
                conn.close()
            continue

        # 2. Idle check — last worker_event timestamp.
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT MAX(ts) AS last_ts FROM worker_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            last_ts = _parse_ts(row["last_ts"]) if row else None
        finally:
            conn.close()

        if last_ts is None:
            # No events yet — give it a runway before declaring idle.
            continue
        if last_ts < idle_threshold:
            # Wedged. Kill the PID if alive (defensive) and mark failed.
            if pid:
                try:
                    _terminate(pid)
                except Exception:
                    pass
            conn = db.connect()
            try:
                conn.execute(
                    "UPDATE runs SET status='killed' WHERE id=? AND status IN ('launched','running')",
                    (run_id,),
                )
                # Log a watchdog event so the timeline shows why.
                conn.execute(
                    """INSERT INTO worker_events (run_id, task_id, phase_id, seq, ts, kind, summary, raw_json)
                       SELECT id, task_id, NULL, 999999, ?, 'error',
                              'watchdog: idle for >=' || ? || 'min, killed', '{}'
                       FROM runs WHERE id=?""",
                    (
                        now.strftime("%Y-%m-%dT%H:%M:%S"),
                        idle_minutes,
                        run_id,
                    ),
                )
                conn.commit()
                reaped_idle += 1
            finally:
                conn.close()

    return {
        "checked": len(rows),
        "reaped_dead_pid": reaped_dead_pid,
        "reaped_idle": reaped_idle,
        "reaped_runtime": reaped_runtime,
        "ts": now.isoformat(),
    }


def run_forever(*, idle_minutes: int = 10, max_runtime_minutes: int = 15, poll_seconds: int = 10) -> None:
    """Daemon entry. Polls every `poll_seconds`.

    Default 10s so a worker that exits (PID gone) is reaped from
    status='launched' within ~10s instead of lingering minutes.
    """
    while True:
        try:
            summary = tick(idle_minutes=idle_minutes, max_runtime_minutes=max_runtime_minutes)
            if summary["reaped_dead_pid"] or summary["reaped_idle"] or summary["reaped_runtime"]:
                print(f"watchdog: {summary}", flush=True)
        except Exception as e:
            print(f"watchdog tick failed: {e}", flush=True)
        time.sleep(poll_seconds)


if __name__ == "__main__":
    import sys
    idle = 10
    poll = 10
    max_runtime = 15
    for arg in sys.argv[1:]:
        if arg.startswith("--idle="):
            idle = int(arg.split("=", 1)[1])
        elif arg.startswith("--poll="):
            poll = int(arg.split("=", 1)[1])
        elif arg.startswith("--max-runtime="):
            max_runtime = int(arg.split("=", 1)[1])
    print(f"watchdog start: idle_minutes={idle}, max_runtime_minutes={max_runtime}, poll_seconds={poll}", flush=True)
    run_forever(idle_minutes=idle, max_runtime_minutes=max_runtime, poll_seconds=poll)
