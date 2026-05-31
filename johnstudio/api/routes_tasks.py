"""Task lifecycle + artifact endpoints."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException

import json

from . import _helpers
from .. import collector, db, merger, orchestrator, reviewer
from .schemas import CleanupRequest, MergeRequest, ResumeRequest, RunTaskRequest

router = APIRouter(prefix="/api/projects/{project_id}/tasks", tags=["tasks"])


def _project_name_or_404(project_id: int) -> tuple[str, str]:
    p = _helpers.get_project_by_id(project_id)
    if not p:
        raise HTTPException(status_code=404, detail="project not found")
    return p["name"], p["repo_path"]


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

@router.get("")
def list_tasks(project_id: int) -> list[dict]:
    _project_name_or_404(project_id)
    return _helpers.list_tasks_for_project(project_id)


@router.post("/run", status_code=201)
def run_task(project_id: int, payload: RunTaskRequest) -> dict:
    name, _ = _project_name_or_404(project_id)
    try:
        return orchestrator.run(
            name,
            payload.task,
            dry_run=payload.dry_run,
            stub_only=payload.stub_only,
            requested_workers=payload.workers,
            max_agents=payload.max_agents,
            relevant_files=payload.relevant_files or None,
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{task_number}")
def get_task(project_id: int, task_number: int) -> dict:
    name, _ = _project_name_or_404(project_id)
    try:
        return orchestrator.status(task_number, name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{task_number}/diffs")
def task_diffs(project_id: int, task_number: int, include_text: bool = True) -> dict:
    """Per-worker diffs for the UI diff viewer (item 15).

    Returns each worker's changed-file list + diffstat (from the `diffs`
    table written by the collector) and, by default, the diff text read
    from disk (capped). Run `collect` first if a task has no diffs yet.
    """
    _project_name_or_404(project_id)
    conn = db.connect()
    try:
        db.init_schema(conn)
        trow = conn.execute(
            "SELECT id FROM tasks WHERE project_id = ? AND task_number = ?",
            (project_id, task_number),
        ).fetchone()
        if not trow:
            raise HTTPException(status_code=404, detail="task not found")
        rows = conn.execute(
            """SELECT d.diff_path, d.files_changed_json, d.stats_json, w.name AS worker
               FROM diffs d JOIN workers w ON w.id = d.worker_id
               WHERE d.task_id = ? ORDER BY d.id""",
            (int(trow["id"]),),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        entry = {
            "worker": r["worker"],
            "files_changed": json.loads(r["files_changed_json"] or "[]"),
            "stat": json.loads(r["stats_json"] or "{}"),
            "diff_path": r["diff_path"],
        }
        if include_text and r["diff_path"]:
            try:
                # errors="replace": diffs can contain invalid UTF-8 bytes; a
                # decode error must not 500 the endpoint.
                text = Path(r["diff_path"]).read_text(encoding="utf-8", errors="replace")
                entry["diff_text"] = text[:200_000]  # cap to keep responses sane
                entry["truncated"] = len(text) > 200_000
            except OSError:
                entry["diff_text"] = ""
                entry["error"] = "diff file unreadable"
        out.append(entry)
    return {"task_number": task_number, "diffs": out}


@router.post("/{task_number}/collect")
def collect_task(project_id: int, task_number: int) -> dict:
    name, _ = _project_name_or_404(project_id)
    try:
        return collector.collect(task_number, name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{task_number}/review")
def review_task(project_id: int, task_number: int) -> dict:
    name, _ = _project_name_or_404(project_id)
    try:
        return reviewer.review(task_number, name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{task_number}/merge")
def merge_task(project_id: int, task_number: int, payload: MergeRequest) -> dict:
    name, _ = _project_name_or_404(project_id)
    try:
        return merger.merge(
            task_number,
            name,
            payload.worker_name,
            dry_run=payload.dry_run,
            confirm=payload.confirm,
        )
    except merger.MergeAborted as e:
        raise HTTPException(status_code=409, detail=str(e))
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{task_number}/stop")
def stop_task(project_id: int, task_number: int) -> dict:
    name, _ = _project_name_or_404(project_id)
    try:
        return orchestrator.stop(task_number, name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{task_number}/cleanup")
def cleanup_task(project_id: int, task_number: int, payload: CleanupRequest) -> dict:
    name, _ = _project_name_or_404(project_id)
    try:
        return orchestrator.cleanup(task_number, name, prune_worktrees=payload.prune_worktrees)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{task_number}/resume")
def resume_task(project_id: int, task_number: int, payload: ResumeRequest) -> dict:
    name, _ = _project_name_or_404(project_id)
    try:
        return orchestrator.resume(task_number, name, payload.worker_name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------

def _read_dir_listing(folder: Path) -> list[dict]:
    if not folder.exists():
        return []
    out = []
    for p in sorted(folder.iterdir()):
        if p.is_file():
            out.append({
                "name": p.name,
                "path": str(p),
                "bytes": p.stat().st_size,
                "content": _helpers.read_text_safely(p),
            })
    return out


@router.get("/{task_number}/context-packs")
def get_context_packs(project_id: int, task_number: int) -> list[dict]:
    _, repo = _project_name_or_404(project_id)
    return _read_dir_listing(_helpers.task_folder(repo, task_number) / "prompts")


@router.get("/{task_number}/results")
def get_results(project_id: int, task_number: int) -> list[dict]:
    _, repo = _project_name_or_404(project_id)
    return _read_dir_listing(_helpers.task_folder(repo, task_number) / "results")


@router.get("/{task_number}/diffs")
def get_diffs(project_id: int, task_number: int) -> list[dict]:
    _, repo = _project_name_or_404(project_id)
    return _read_dir_listing(_helpers.task_folder(repo, task_number) / "diffs")


@router.get("/{task_number}/logs")
def get_logs(project_id: int, task_number: int) -> list[dict]:
    _, repo = _project_name_or_404(project_id)
    return _read_dir_listing(_helpers.task_folder(repo, task_number) / "logs")


@router.get("/{task_number}/review")
def get_review_markdown(project_id: int, task_number: int) -> dict:
    _, repo = _project_name_or_404(project_id)
    folder = _helpers.task_folder(repo, task_number)
    return {
        "exists": (folder / "FINAL_REVIEW.md").exists(),
        "content": _helpers.read_text_safely(folder / "FINAL_REVIEW.md"),
    }


@router.get("/{task_number}/merge-plan")
def get_merge_plan_markdown(project_id: int, task_number: int) -> dict:
    _, repo = _project_name_or_404(project_id)
    folder = _helpers.task_folder(repo, task_number)
    return {
        "exists": (folder / "MERGE_PLAN.md").exists(),
        "content": _helpers.read_text_safely(folder / "MERGE_PLAN.md"),
    }


@router.get("/{task_number}/safety-report")
def get_safety_report(project_id: int, task_number: int) -> dict:
    name, _ = _project_name_or_404(project_id)
    try:
        summary = collector.collect(task_number, name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return _helpers.safety_report_from_collect(summary)
