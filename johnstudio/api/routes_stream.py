"""SSE: live event stream per project.

The graph UI opens one stream per project page (`/p/:id/graph`) and
receives a single unified feed of:
- `worker_event` — one row from `worker_events` (assistant text, tool use,
  result, etc.)
- `task_state` — task/run status transitions (pending → running → ...).
- `phase_state` — chain-mode phase transitions.

Browser EventSource can't send custom headers, so we accept the bearer
token either via header (preferred — the React client uses `fetch` with
streaming, which can set headers) OR via the `token` query param as a
fallback for plain `EventSource` consumers. The handler authenticates
once and then streams.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from .. import auth, config, db


router = APIRouter(prefix="/api/projects/{project_id}", tags=["stream"])


def _require_token(authorization: str | None, token_qp: str | None) -> None:
    expected = auth.get_or_create_token()
    presented = None
    if authorization:
        parts = authorization.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            presented = parts[1].strip()
    if not presented and token_qp:
        presented = token_qp
    import hmac
    if not presented or not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")


def _row_to_dict(r: sqlite3.Row) -> dict:
    return {k: r[k] for k in r.keys()}


@router.get("/stream")
async def project_stream(
    project_id: int,
    request: Request,
    since_id: int = Query(0, ge=0),
    token: str | None = Query(None),
):
    """SSE stream of all worker_events for this project, plus task/phase
    state transitions. Heartbeat every 15s so proxies don't time out.
    """
    _require_token(request.headers.get("authorization"), token)

    async def gen() -> AsyncIterator[bytes]:
        last_event_id = since_id
        last_task_state: dict[int, str] = {}
        last_phase_state: dict[int, tuple[str, str]] = {}
        last_heartbeat = asyncio.get_event_loop().time()
        # Hook-event feed (item 16/20): tail <home>/events.jsonl from the
        # current end forward, so the UI gets a live feed of mcp.tool_called,
        # worker.exited, cost.threshold_crossed, etc. as `hook_event` SSE.
        event_log = config.home_dir() / "events.jsonl"
        try:
            hook_offset = event_log.stat().st_size  # only NEW events from connect
        except OSError:
            hook_offset = 0
        # Initial snapshot of tasks + phases so the UI can render the
        # current topology immediately on connect.
        yield _sse_event("snapshot", _snapshot(project_id))

        while True:
            if await request.is_disconnected():
                return

            # 0. New hook-bus events (mcp.tool_called, worker.exited, cost.*, ...)
            # Binary tailing keyed on byte offsets (text-mode tell() returns an
            # opaque cookie that diverges from st_size on non-ASCII content,
            # causing duplicate/replayed events). Only advance past COMPLETE
            # lines so a mid-line append isn't dropped.
            try:
                size = event_log.stat().st_size
                if size < hook_offset:        # log rotated/truncated
                    hook_offset = 0
                if size > hook_offset:
                    with event_log.open("rb") as f:
                        f.seek(hook_offset)
                        chunk = f.read(size - hook_offset)
                    nl = chunk.rfind(b"\n")
                    if nl != -1:
                        complete = chunk[:nl + 1]
                        hook_offset += len(complete)   # leave any partial trailing line
                        for raw in complete.split(b"\n"):
                            raw = raw.strip()
                            if not raw:
                                continue
                            try:
                                rec = json.loads(raw.decode("utf-8", "replace"))
                            except ValueError:
                                continue
                            yield _sse_event("hook_event", rec)
            except OSError:
                pass

            # 1. New worker_events
            conn = db.connect()
            try:
                rows = conn.execute(
                    """SELECT e.* FROM worker_events e
                       JOIN tasks t ON t.id = e.task_id
                       WHERE t.project_id = ? AND e.id > ?
                         AND t.status != 'archived'
                       ORDER BY e.id ASC LIMIT 200""",
                    (project_id, last_event_id),
                ).fetchall()
                for row in rows:
                    yield _sse_event("worker_event", _row_to_dict(row))
                    last_event_id = max(last_event_id, int(row["id"]))

                # 2. Task state transitions
                task_rows = conn.execute(
                    """SELECT t.id, t.task_number, t.title, t.status,
                              json_group_array(json_object(
                                'id', r.id, 'worker', w.name,
                                'status', r.status, 'pane', r.tmux_pane,
                                'worktree', r.worktree_path
                              )) AS runs_json
                       FROM tasks t
                       LEFT JOIN runs r ON r.task_id = t.id
                       LEFT JOIN workers w ON w.id = r.worker_id
                       WHERE t.project_id = ? AND t.status != 'archived'
                       GROUP BY t.id""",
                    (project_id,),
                ).fetchall()
                for tr in task_rows:
                    tid = int(tr["id"])
                    state = tr["status"]
                    if last_task_state.get(tid) != state:
                        last_task_state[tid] = state
                        yield _sse_event("task_state", {
                            "task_id": tid,
                            "task_number": tr["task_number"],
                            "title": tr["title"],
                            "status": state,
                            "runs": json.loads(tr["runs_json"] or "[]"),
                        })

                # 3. Phase state transitions (chain mode)
                phase_rows = conn.execute(
                    """SELECT p.* FROM task_phases p
                       JOIN tasks t ON t.id = p.task_id
                       WHERE t.project_id = ? AND t.status != 'archived'""",
                    (project_id,),
                ).fetchall()
                for pr in phase_rows:
                    pid = int(pr["id"])
                    key = (pr["status"], pr["verdict"] or "")
                    if last_phase_state.get(pid) != key:
                        last_phase_state[pid] = key
                        yield _sse_event("phase_state", _row_to_dict(pr))
            finally:
                conn.close()

            # Heartbeat (every 15s of no other events).
            now = asyncio.get_event_loop().time()
            if now - last_heartbeat > 15:
                yield b": heartbeat\n\n"
                last_heartbeat = now

            await asyncio.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",  # disable any intermediate buffering
    })


def _snapshot(project_id: int) -> dict:
    """Live-graph snapshot. Excludes `archived` tasks (the user explicitly
    moved them out of the active workspace) so the tree shows only
    in-flight work. Archived tasks remain queryable elsewhere by status."""
    conn = db.connect()
    try:
        tasks = conn.execute(
            """SELECT id, task_number, title, status FROM tasks
               WHERE project_id = ? AND status != 'archived' ORDER BY id""",
            (project_id,),
        ).fetchall()
        runs = conn.execute(
            """SELECT r.id, r.task_id, r.status, r.tmux_pane, r.worktree_path,
                      w.name AS worker
               FROM runs r JOIN tasks t ON t.id = r.task_id
               LEFT JOIN workers w ON w.id = r.worker_id
               WHERE t.project_id = ? AND t.status != 'archived'""",
            (project_id,),
        ).fetchall()
        phases = conn.execute(
            """SELECT p.* FROM task_phases p
               JOIN tasks t ON t.id = p.task_id
               WHERE t.project_id = ? AND t.status != 'archived'""",
            (project_id,),
        ).fetchall()
        latest_events = conn.execute(
            """SELECT e.* FROM worker_events e
               JOIN tasks t ON t.id = e.task_id
               WHERE t.project_id = ? AND t.status != 'archived'
               ORDER BY e.id DESC LIMIT 100""",
            (project_id,),
        ).fetchall()
        return {
            "project_id": project_id,
            "tasks": [_row_to_dict(t) for t in tasks],
            "runs": [_row_to_dict(r) for r in runs],
            "phases": [_row_to_dict(p) for p in phases],
            "recent_events": [_row_to_dict(e) for e in reversed(latest_events)],
        }
    finally:
        conn.close()


def _sse_event(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode("utf-8")
