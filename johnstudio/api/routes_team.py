"""Team mode endpoints: planner spawn → plan approval → specialist spawn.

See RFC 0001 and team_orchestrator.py for the underlying flow.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from . import _helpers
from .. import arc_budget, db, team, team_orchestrator, team_progress


router = APIRouter(prefix="/api/projects/{project_id}/team", tags=["team"])


class BeginTeamRequest(BaseModel):
    task: str = Field(..., min_length=1, max_length=8000,
                      description="What the team should build (1–8000 chars)")
    budget_usd: float | None = Field(None, ge=0)  # optional hard cap on notional
                                          # cost (Claude's total_cost_usd, summed)


def _project_name(project_id: int) -> str:
    p = _helpers.get_project_by_id(project_id)
    if not p:
        raise HTTPException(status_code=404, detail="project not found")
    return p["name"]


def _task_db_id(project_id: int, task_number: int) -> int:
    conn = db.connect()
    db.init_schema(conn)
    try:
        row = conn.execute(
            "SELECT id FROM tasks WHERE project_id = ? AND task_number = ?",
            (project_id, task_number),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="task not found")
    return int(row["id"])


# ---------------------------------------------------------------------------
# Catalog (handy for the UI)
# ---------------------------------------------------------------------------

@router.get("/catalog")
def team_catalog(project_id: int) -> dict:
    catalog = team.load_role_catalog()
    by_vp = team.roles_by_vp(catalog)
    return {
        "total": len(catalog),
        "by_vp": {
            vp: [r.to_dict() for r in roles]
            for vp, roles in by_vp.items()
        },
    }


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

@router.post("/run", status_code=201)
def team_run(project_id: int, payload: BeginTeamRequest) -> dict:
    name = _project_name(project_id)
    try:
        out = team_orchestrator.begin_team_task(
            project_name=name, task_text=payload.task,
            budget_usd=payload.budget_usd,
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return out


@router.get("/{task_number}/budget")
def team_budget(project_id: int, task_number: int) -> dict:
    """Current rolling cost + budget posture for a team task."""
    tid = _task_db_id(project_id, task_number)
    return team_orchestrator.check_budget(tid)


@router.get("/{task_number}/cost")
def team_cost(project_id: int, task_number: int) -> dict:
    """Per-worker cost/token breakdown for the UI cost meter (item 13)."""
    tid = _task_db_id(project_id, task_number)
    return arc_budget.task_cost_breakdown(tid)


@router.post("/{task_number}/plan-critic")
def team_plan_critic(project_id: int, task_number: int) -> dict:
    """Spawn the product-manager role to critique the planner's
    TEAM_PLAN.md. The UI shows the resulting PLAN_CRITIQUE.md alongside
    the plan so the user reviews both before approving.
    """
    tid = _task_db_id(project_id, task_number)
    return team_orchestrator.run_plan_critic(tid)


@router.get("/{task_number}/plan-critique")
def team_plan_critique(project_id: int, task_number: int) -> dict:
    """Return the current PLAN_CRITIQUE.md content if it exists."""
    from pathlib import Path
    p = _helpers.get_project_by_id(project_id)
    if not p:
        raise HTTPException(status_code=404, detail="project not found")
    crit = Path(p["repo_path"]) / ".johnstudio" / "tasks" / f"task-{task_number:04d}" / "PLAN_CRITIQUE.md"
    if not crit.exists():
        return {"exists": False, "path": str(crit)}
    return {"exists": True, "path": str(crit), "content": crit.read_text(encoding="utf-8")}


@router.get("/{task_number}")
def team_status(project_id: int, task_number: int) -> dict:
    tid = _task_db_id(project_id, task_number)
    return team_orchestrator.get_team_state(tid)


@router.get("/{task_number}/plan")
def team_plan_raw(project_id: int, task_number: int) -> dict:
    tid = _task_db_id(project_id, task_number)
    state = team_orchestrator.get_team_state(tid)
    if not state.get("plan_exists"):
        raise HTTPException(status_code=404, detail="planner hasn't produced TEAM_PLAN.md yet")
    from pathlib import Path
    p = Path(state["plan_path"])
    return {
        "path": str(p),
        "content": p.read_text(encoding="utf-8"),
        "plan_valid": state.get("plan_valid", False),
        "plan_error": state.get("plan_error"),
        "plan": state.get("plan"),
    }


@router.post("/{task_number}/approve")
def team_approve(project_id: int, task_number: int, response: Response) -> dict:
    """Approve the plan and launch the team.

    The plan is validated synchronously (fast); the specialist spawn runs
    in the background. On success we return **202 Accepted** immediately
    with the task id — the client follows spawn progress over the SSE
    stream (per-specialist `runs` rows + WORKER_SPAWNED events) rather
    than blocking on the HTTP call (which used to time out at 120s while
    the server kept spawning).

    Non-202 cases keep their natural status:
    - plan failed validation  → 200 with {"plan_invalid": true, ...}
      (task parked at 'needs_replan'; POST /replan to re-issue planner)
    - another approve already owns the spawn → 200 {"already_running": true}
    - budget exceeded → 200 {"refused": true, ...}
    """
    tid = _task_db_id(project_id, task_number)
    try:
        out = team_orchestrator.approve_plan_and_run(tid)
    except (RuntimeError, team.PlanError) as e:
        raise HTTPException(status_code=409, detail=str(e))
    if out.get("accepted"):
        response.status_code = 202
    return out


@router.post("/{task_number}/replan")
def team_replan(project_id: int, task_number: int) -> dict:
    """Re-issue the lead planner after a rejected plan.

    Clears the 'needs_replan' state and re-spawns the planner with the
    prior validation error inlined. Refused once specialists are running.
    """
    tid = _task_db_id(project_id, task_number)
    try:
        return team_orchestrator.replan_team_task(tid)
    except (RuntimeError, team.PlanError) as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/{task_number}/cancel")
def team_cancel(project_id: int, task_number: int) -> dict:
    """Cancel a team task mid-flight.

    Kills every live specialist subprocess (PID + tmux pane), marks their
    runs 'stopped' and the task 'cancelled', emits WORKER_KILLED per
    specialist. Idempotent — safe to call on an already-finished task
    (returns count=0).
    """
    tid = _task_db_id(project_id, task_number)
    try:
        return team_orchestrator.cancel_team_task(tid)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{task_number}/advance")
def team_advance(project_id: int, task_number: int) -> dict:
    """Tick the state machine. Idempotent.

    - running → spawn cross-VP reviewers once every specialist is DONE
    - reviewing → consolidate MERGE_PLAN.md once every reviewer is DONE
    - pending_merge → human gate; no-op
    """
    tid = _task_db_id(project_id, task_number)
    try:
        return team_orchestrator.advance_team_task(tid)
    except (RuntimeError, team.PlanError) as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.get("/{task_number}/progress")
def get_team_progress(project_id: int, task_number: int) -> dict:
    """0..100 score + phase + stuck-run summary for the UI progress bar.

    Read-only; safe to poll. Backs cluster E (progress score + stuck flag).
    """
    tid = _task_db_id(project_id, task_number)
    return team_progress.compute_progress(tid)


@router.get("/{task_number}/activity")
def get_team_activity(project_id: int, task_number: int, limit: int = 30) -> dict:
    """Recent worker_events for this task, newest first, with role/worker
    name joined for the activity feed.
    """
    tid = _task_db_id(project_id, task_number)
    events = team_progress.recent_activity(tid, limit=limit)
    return {"task_number": task_number, "count": len(events), "events": events}


@router.get("/{task_number}/merge-plan")
def team_merge_plan(project_id: int, task_number: int) -> dict:
    """Return the current MERGE_PLAN.md content, if any."""
    tid = _task_db_id(project_id, task_number)
    state = team_orchestrator.get_team_state(tid)
    path = state.get("merge_plan_path")
    if not path:
        raise HTTPException(status_code=404, detail="MERGE_PLAN.md has not been generated yet")
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"{path} no longer exists")
    return {"path": path, "content": p.read_text(encoding="utf-8")}
