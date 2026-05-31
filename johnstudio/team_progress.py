"""Team-mode progress, stuck detection, and activity feed.

Three small read-only helpers backing the UI cluster E improvements:

- `compute_progress(task_db_id)` — derives a 0..100 score from team-state
  phase + per-assignment DONE.md presence, plus a stuck flag computed from
  `worker_events.ts` recency.
- `detect_stuck_runs(task_db_id, idle_seconds=180)` — returns the list of
  active runs that haven't emitted a worker_event in the configured window.
  Uses a shorter threshold than `watchdog.py` so the UI can show "warming
  up / getting stuck / dead" gradations before the watchdog reaps.
- `recent_activity(task_db_id, limit=30)` — joins worker_events ↔ runs ↔
  workers for an activity feed (run/worker name + event kind + summary +
  ts).

All read-only; no spawning, no state mutation. Safe to poll from the UI.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import db, team_orchestrator

STUCK_WARN_SECONDS = 90
STUCK_ALERT_SECONDS = 180
ACTIVITY_DEFAULT_LIMIT = 30

_PHASE_FLOOR = {
    "planning": 0,
    "running": 20,
    "revising": 35,
    "reviewing": 70,
    "pending_merge": 90,
    "merged": 100,
    "rejected": 100,
}
_PHASE_CEIL = {
    "planning": 20,
    "running": 70,
    "revising": 70,
    "reviewing": 90,
    "pending_merge": 100,
    "merged": 100,
    "rejected": 100,
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(ts: str | None) -> datetime | None:
    """Parse a stored timestamp into an aware UTC datetime, or None."""
    if not ts:
        return None
    ts = ts.rstrip("Z")
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _last_event_ts(task_db_id: int) -> datetime | None:
    """Most recent worker_event timestamp for the task, or None."""
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT MAX(ts) AS last_ts FROM worker_events WHERE task_id = ?",
            (task_db_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return _parse_ts(row["last_ts"])


def compute_progress(task_db_id: int) -> dict[str, Any]:
    """Return {score, phase, total, done, in_flight, stuck_count, ...}.

    Scoring model:
    - `planning`: 0 if no plan, 10 if plan exists but unapproved, 20 ceil.
    - `running`: 20 + 50 * (specialists_done / total).
    - `reviewing`: 70 + 20 * (reviewers_done / total).
    - `pending_merge`: 90.
    - `merged`/`rejected`: 100.
    """
    state = team_orchestrator.get_team_state(task_db_id)
    status = state.get("status") or "planning"
    phase = status if status in _PHASE_FLOOR else "unknown"

    # Resolve the task worktree-root so we can probe per-assignment DONE.md.
    # RECONSTRUCTED: the exact worktree-vs-output path used for DONE.md
    # probing is inferred from team_orchestrator._task_folder + the
    # assignment "worktree"/"output" keys; the disassembly confirmed a
    # DONE.md / Path / exists probe per assignment but not the exact join.
    tf: Path | None = None
    try:
        proj_info = team_orchestrator._project_for_task(task_db_id)
        if proj_info:
            _proj_name, repo, task_number = proj_info
            tf = team_orchestrator._task_folder(repo, task_number)
    except Exception:
        tf = None

    plan = state.get("plan") or {}
    assignments: list[dict] = list(plan.get("assignments") or [])
    reviewers: list[dict] = list(plan.get("cross_review") or plan.get("reviewers") or [])

    total = len(assignments)
    done = 0
    for a in assignments:
        if _assignment_done(tf, a):
            done += 1
    in_flight = max(total - done, 0)

    rev_total = len(reviewers)
    rev_done = 0
    for r in reviewers:
        if _assignment_done(tf, r):
            rev_done += 1

    floor = _PHASE_FLOOR.get(phase, 0)
    ceil_v = _PHASE_CEIL.get(phase, 100)

    if phase == "planning":
        score = 10 if state.get("plan_exists") else 0
    elif phase in ("running", "revising"):
        frac = (done / total) if total else 0.0
        score = floor + (ceil_v - floor) * frac
    elif phase == "reviewing":
        frac = (rev_done / rev_total) if rev_total else 0.0
        score = floor + (ceil_v - floor) * frac
    elif phase == "pending_merge":
        score = 90
    elif phase in ("merged", "rejected"):
        score = 100
    else:
        score = floor

    score = int(max(0, min(100, score)))

    last_event_ts = _last_event_ts(task_db_id)
    stuck_runs = detect_stuck_runs(task_db_id, idle_seconds=STUCK_ALERT_SECONDS)
    stuck = bool(stuck_runs) and phase in ("running", "revising", "reviewing")

    return {
        "task_db_id": task_db_id,
        "phase": phase,
        "status": status,
        "score": score,
        "total": total,
        "done": done,
        "in_flight": in_flight,
        "rev_total": rev_total,
        "rev_done": rev_done,
        "stuck": stuck,
        "stuck_count": len(stuck_runs),
        "last_event_ts": last_event_ts.isoformat() if last_event_ts else None,
        "plan_exists": bool(state.get("plan_exists")),
    }


def _assignment_done(tf: Path | None, assignment: dict) -> bool:
    """Best-effort: does this assignment's worktree contain a DONE.md?

    RECONSTRUCTED: probes `<worktree>/DONE.md` when the assignment carries
    a `worktree` path, else `<task_folder>/<output>` heuristics. Returns
    False on any uncertainty rather than over-counting progress.
    """
    if not isinstance(assignment, dict):
        return False
    wt = assignment.get("worktree")
    if wt:
        try:
            if (Path(str(wt)) / "DONE.md").exists():
                return True
        except OSError:
            return False
    output = assignment.get("output")
    if tf is not None and output:
        try:
            return (tf / str(output)).exists()
        except OSError:
            return False
    return False


def detect_stuck_runs(
    task_db_id: int, *, idle_seconds: int = STUCK_ALERT_SECONDS
) -> list[dict]:
    """Return active runs whose latest worker_event is older than the
    threshold OR which have no events at all and have been launched for
    >idle_seconds.

    Output shape matches what the activity feed renders so the UI can
    decorate the relevant rows without a second join.
    """
    now = _utc_now()
    threshold = timedelta(seconds=idle_seconds)
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT r.id AS run_id, r.status, r.started_at, w.name AS worker_name,\n"
            "                      w.role AS role,\n"
            "                      (SELECT MAX(ts) FROM worker_events WHERE run_id = r.id) AS last_ts,\n"
            "                      (SELECT COUNT(*) FROM worker_events WHERE run_id = r.id) AS event_count\n"
            "               FROM runs r JOIN workers w ON w.id = r.worker_id\n"
            "               WHERE r.task_id = ?\n"
            "                 AND r.status IN ('launched','running')",
            (task_db_id,),
        ).fetchall()
    finally:
        conn.close()

    output: list[dict] = []
    for row in rows:
        last_ts = _parse_ts(row["last_ts"])
        # If the run has emitted events, measure idle from the last one.
        # Otherwise fall back to how long it's been since it was launched.
        ref_ts = last_ts or _parse_ts(row["started_at"])
        if ref_ts is None:
            continue
        idle_sec = (now - ref_ts).total_seconds()
        if idle_sec < threshold.total_seconds():
            continue
        output.append(
            {
                "run_id": row["run_id"],
                "worker_name": row["worker_name"],
                "role": row["role"],
                "status": row["status"],
                "last_event_ts": last_ts.isoformat() if last_ts else None,
                "idle_seconds": int(idle_sec),
                "event_count": row["event_count"],
            }
        )
    return output


def recent_activity(task_db_id: int, *, limit: int = ACTIVITY_DEFAULT_LIMIT) -> list[dict]:
    """Recent worker_events for a task, joined with run/worker context.

    Newest-first. The UI renders a compact line per event:
      `12:04:18 · backend-developer · tool_use · Edit:routes_team.py`
    """
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT e.id, e.run_id, e.ts, e.kind, e.summary, e.seq,\n"
            "                      w.name AS worker_name, w.role AS role,\n"
            "                      r.status AS run_status\n"
            "               FROM worker_events e\n"
            "               JOIN runs r ON r.id = e.run_id\n"
            "               JOIN workers w ON w.id = r.worker_id\n"
            "               WHERE e.task_id = ?\n"
            "               ORDER BY e.id DESC\n"
            "               LIMIT ?",
            (task_db_id, int(limit)),
        ).fetchall()
    finally:
        conn.close()

    return [
        {
            "id": row["id"],
            "run_id": row["run_id"],
            "ts": row["ts"],
            "kind": row["kind"],
            "summary": row["summary"],
            "seq": row["seq"],
            "worker_name": row["worker_name"],
            "role": row["role"],
            "run_status": row["run_status"],
        }
        for row in rows
    ]
