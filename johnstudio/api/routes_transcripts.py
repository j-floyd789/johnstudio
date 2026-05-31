"""Claude Code transcript surfacing — see `johnstudio.transcripts`.

Given a `run_id`, find the corresponding `~/.claude/projects/.../<sid>.jsonl`
and return its parsed contents. Optionally split into main + sidechain
(subagent) streams. Powers the "deep view" panel in the graph UI.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from .. import db, transcripts


router = APIRouter(prefix="/api/projects/{project_id}", tags=["transcripts"])


@router.get("/runs/{run_id}/transcript")
def run_transcript(
    project_id: int, run_id: int,
    limit: int = Query(500, ge=1, le=5000),
    only_sidechain: bool = Query(False),
) -> dict:
    """Return the on-disk Claude Code transcript for a worker run.

    Resolution order:
    1. Pull the worker's cwd from `runs` (worktree_path) and `tasks` (task folder).
    2. Resolve the session_id from the first `system:init` worker_event.
    3. Find `<encoded-cwd>/<session_id>.jsonl` under ~/.claude/projects.
    4. Parse up to `limit` lines.
    """
    conn = db.connect()
    db.init_schema(conn)
    try:
        row = conn.execute(
            """SELECT r.id, r.task_id, r.worktree_path, r.prompt_path,
                      t.project_id, t.task_number, p.repo_path
               FROM runs r
               JOIN tasks t ON t.id = r.task_id
               JOIN projects p ON p.id = t.project_id
               WHERE r.id = ? AND t.project_id = ?""",
            (run_id, project_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="run not found")
        ev = conn.execute(
            """SELECT raw_json FROM worker_events
               WHERE run_id = ? AND kind LIKE 'system%'
               ORDER BY id ASC LIMIT 1""",
            (run_id,),
        ).fetchone()
    finally:
        conn.close()

    cwd = row["worktree_path"]
    if not cwd:
        # Read-only workers run from the task folder.
        cwd = str(Path(row["repo_path"]) / ".johnstudio" / "tasks" / f"task-{int(row['task_number']):04d}")

    session_id = ""
    if ev and ev["raw_json"]:
        try:
            init = json.loads(ev["raw_json"])
            session_id = init.get("session_id") or ""
        except json.JSONDecodeError:
            pass

    if not session_id:
        # Fall back to the most recent transcript under this cwd.
        recents = transcripts.find_recent_transcripts(cwd, limit=1)
        if not recents:
            raise HTTPException(
                status_code=404,
                detail=f"no Claude Code transcripts found at {transcripts.transcript_dir_for_cwd(cwd)}",
            )
        path = recents[0]
    else:
        path = transcripts.find_session_transcript(cwd, session_id)
        if path is None:
            raise HTTPException(
                status_code=404,
                detail=f"no transcript for session {session_id} under {transcripts.transcript_dir_for_cwd(cwd)}",
            )

    entries = transcripts.read_transcript(
        path, limit=limit,
        include_sidechain=not only_sidechain or only_sidechain,
        only_sidechain=only_sidechain,
    )
    n_side = sum(1 for e in entries if e.get("isSidechain"))
    return {
        "run_id": run_id,
        "cwd": cwd,
        "session_id": session_id,
        "transcript_path": str(path),
        "encoded_dir": str(transcripts.transcript_dir_for_cwd(cwd)),
        "entries": entries,
        "n_total": len(entries),
        "n_sidechain": n_side,
    }


@router.get("/runs/{run_id}/transcript/list")
def run_transcript_list(project_id: int, run_id: int) -> dict:
    """List the recent transcript files we'd consider for this run's cwd."""
    conn = db.connect()
    db.init_schema(conn)
    try:
        row = conn.execute(
            """SELECT r.worktree_path, t.task_number, p.repo_path
               FROM runs r JOIN tasks t ON t.id = r.task_id
               JOIN projects p ON p.id = t.project_id
               WHERE r.id = ? AND t.project_id = ?""",
            (run_id, project_id),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="run not found")
    cwd = row["worktree_path"] or str(
        Path(row["repo_path"]) / ".johnstudio" / "tasks" / f"task-{int(row['task_number']):04d}"
    )
    files = transcripts.find_recent_transcripts(cwd, limit=20)
    return {
        "cwd": cwd,
        "encoded_dir": str(transcripts.transcript_dir_for_cwd(cwd)),
        "files": [{"path": str(p), "mtime": p.stat().st_mtime} for p in files],
    }
