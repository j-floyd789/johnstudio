"""Chain mode endpoints: RFC → implement → review → revise → merge."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import _helpers
from .. import chain, merger

router = APIRouter(prefix="/api/projects/{project_id}/chain", tags=["chain"])


class BeginChainRequest(BaseModel):
    task: str
    architect: str = "claude_review"
    rfc_reviewer: str = "claude_review"
    implementer: str = "claude_backend"
    reviewer: str = "claude_review"


class ApproveRfcRequest(BaseModel):
    note: str | None = None


class RejectRfcRequest(BaseModel):
    reason: str | None = None


class MergeRequest(BaseModel):
    confirm: bool = False


def _project_name(project_id: int) -> str:
    p = _helpers.get_project_by_id(project_id)
    if not p:
        raise HTTPException(status_code=404, detail="project not found")
    return p["name"]


def _task_db_id(project_id: int, task_number: int) -> int:
    from .. import db
    conn = db.connect()
    db.init_schema(conn)
    row = conn.execute(
        "SELECT id FROM tasks WHERE project_id = ? AND task_number = ?",
        (project_id, task_number),
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="task not found")
    return int(row["id"])


def _phase_to_dict(p: chain.PhaseRow) -> dict:
    return {
        "id": p.id,
        "phase": p.phase.value,
        "round": p.round,
        "status": p.status,
        "verdict": p.verdict.value if p.verdict else None,
        "artifact_path": p.artifact_path,
        "notes": p.notes,
        "started_at": p.started_at,
        "completed_at": p.completed_at,
    }


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

@router.post("/run", status_code=201)
def chain_run(project_id: int, payload: BeginChainRequest) -> dict:
    name = _project_name(project_id)
    try:
        start = chain.begin_chain(
            project_name=name,
            task_text=payload.task,
            architect_worker=payload.architect,
            rfc_reviewer_worker=payload.rfc_reviewer,
            implementer_worker=payload.implementer,
            reviewer_worker=payload.reviewer,
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    launch = chain.run_phase(start["task_db_id"])
    return {**start, "launch": launch}


@router.post("/{task_number}/advance")
def chain_advance(project_id: int, task_number: int) -> dict:
    tid = _task_db_id(project_id, task_number)
    out = chain.complete_current_phase_if_ready(tid)
    cur = chain.current_phase(tid)
    if cur and cur.phase not in chain.HUMAN_GATES and cur.phase not in chain.TERMINAL and cur.status == "pending":
        out["launch"] = chain.run_phase(tid)
    out["current"] = _phase_to_dict(cur) if cur else None
    return out


@router.get("/{task_number}")
def chain_status(project_id: int, task_number: int) -> dict:
    tid = _task_db_id(project_id, task_number)
    phases = chain.list_phases(tid)
    cur = chain.current_phase(tid)
    return {
        "task_number": task_number,
        "phases": [_phase_to_dict(p) for p in phases],
        "current": _phase_to_dict(cur) if cur else None,
        "human_gate": (cur.phase in chain.HUMAN_GATES) if cur else False,
        "terminal": (cur.phase in chain.TERMINAL) if cur else False,
    }


@router.get("/{task_number}/artifact")
def chain_artifact(project_id: int, task_number: int, kind: str) -> dict:
    """Read one of the chain's artifacts: rfc | rfc_review | review_<n> | result."""
    from pathlib import Path

    p = _helpers.get_project_by_id(project_id)
    if not p:
        raise HTTPException(status_code=404, detail="project not found")
    repo = Path(p["repo_path"])
    tf = chain.task_folder(repo, task_number)
    wt = repo / ".johnstudio" / "worktrees" / f"task-{task_number:04d}-chain"

    if kind == "rfc":
        path = tf / "RFC.md"
    elif kind == "rfc_review":
        path = tf / "RFC_REVIEW.md"
    elif kind == "result":
        path = wt / "RESULT.md"
    elif kind.startswith("review_"):
        try:
            n = int(kind.split("_", 1)[1])
        except ValueError:
            raise HTTPException(status_code=400, detail="bad review kind")
        path = wt / f"REVIEW_{n}.md"
    else:
        raise HTTPException(status_code=400, detail=f"unknown kind: {kind}")
    return {
        "kind": kind,
        "exists": path.exists(),
        "path": str(path),
        "content": _helpers.read_text_safely(path),
    }


# ---------------------------------------------------------------------------
# Human gates
# ---------------------------------------------------------------------------

@router.post("/{task_number}/approve-rfc")
def chain_approve_rfc(project_id: int, task_number: int, payload: ApproveRfcRequest) -> dict:
    tid = _task_db_id(project_id, task_number)
    try:
        chain.approve_rfc(tid, note=payload.note)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    launch = chain.run_phase(tid)
    return {"approved": True, "launch": launch}


@router.post("/{task_number}/reject-rfc")
def chain_reject_rfc(project_id: int, task_number: int, payload: RejectRfcRequest) -> dict:
    tid = _task_db_id(project_id, task_number)
    try:
        return chain.reject_rfc(tid, reason=payload.reason)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/{task_number}/merge")
def chain_merge(project_id: int, task_number: int, payload: MergeRequest) -> dict:
    if not payload.confirm:
        raise HTTPException(status_code=409, detail="confirm=true required")
    name = _project_name(project_id)
    tid = _task_db_id(project_id, task_number)
    cur = chain.current_phase(tid)
    if not cur or cur.phase not in (chain.Phase.PENDING_MERGE, chain.Phase.CONFLICT):
        raise HTTPException(status_code=409, detail=f"chain not in mergeable state (current: {cur.phase.value if cur else 'none'})")
    try:
        out = merger.merge(task_number, name, "chain", confirm=True)
    except merger.MergeAborted as e:
        raise HTTPException(status_code=409, detail=str(e))
    if out.get("merged"):
        chain.mark_merged(tid)
    return out


@router.post("/{task_number}/reject")
def chain_reject(project_id: int, task_number: int, payload: RejectRfcRequest) -> dict:
    tid = _task_db_id(project_id, task_number)
    return chain.reject_task(tid, reason=payload.reason)
