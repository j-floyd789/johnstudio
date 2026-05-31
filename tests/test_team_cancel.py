"""Item 10 — mid-flight team-task cancellation (team_orchestrator.cancel_team_task)."""
from __future__ import annotations

import subprocess
import sys
import time

import pytest

from johnstudio import db, init as init_mod, project as project_mod, team_orchestrator
from johnstudio.hooks import EventTypes, bus


@pytest.fixture
def initialized(jh_home, git_repo):
    init_mod.run_init()
    project_mod.add_project("demo", git_repo)
    return git_repo


def _make_task(repo) -> int:
    """Insert a task row + scaffold the task folder; return task_db_id."""
    pinfo = project_mod.get_project("demo")
    conn = db.connect()
    db.init_schema(conn)
    cur = conn.execute(
        """INSERT INTO tasks (project_id, task_number, title, description, status, base_branch)
           VALUES (?,1,?,?,?,?) RETURNING id""",
        (pinfo["id"], "t", "t", "running", "main"),
    )
    tid = int(cur.fetchone()["id"])
    conn.commit()
    conn.close()
    return tid


def _add_run(tid: int, *, pid, status="running") -> int:
    conn = db.connect()
    cur = conn.execute(
        """INSERT INTO workers (name, provider, role, command, can_edit, worktree_enabled)
           VALUES (?,?,?,?,0,0) ON CONFLICT(name) DO UPDATE SET provider=excluded.provider
           RETURNING id""",
        (f"w{pid}", "claude", "r", "cmd"),
    )
    wid = int(cur.fetchone()["id"])
    cur = conn.execute(
        """INSERT INTO runs (task_id, worker_id, status, pid, started_at)
           VALUES (?,?,?,?,'now') RETURNING id""",
        (tid, wid, status, pid),
    )
    rid = int(cur.fetchone()["id"])
    conn.commit()
    conn.close()
    return rid


def _run_status(rid: int) -> str:
    conn = db.connect()
    s = conn.execute("SELECT status FROM runs WHERE id=?", (rid,)).fetchone()["status"]
    conn.close()
    return s


def _task_status(tid: int) -> str:
    conn = db.connect()
    s = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"]
    conn.close()
    return s


def test_cancel_kills_live_worker_and_marks_terminal(initialized):
    repo = initialized
    tid = _make_task(repo)
    # A real long-lived child process so cancel has a PID to actually kill.
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    rid = _add_run(tid, pid=proc.pid)

    killed_events = []
    tok = bus.subscribe(EventTypes.WORKER_KILLED, lambda e, p: killed_events.append(p))
    try:
        out = team_orchestrator.cancel_team_task(tid)
    finally:
        bus.unsubscribe(tok)

    assert out["count"] == 1
    assert out["cancelled"][0]["killed"] is True
    assert _run_status(rid) == "stopped"
    assert _task_status(tid) == "cancelled"
    assert killed_events and killed_events[0]["run_id"] == rid

    # Process must be gone.
    proc.wait(timeout=5)
    assert proc.poll() is not None


def test_cancel_is_idempotent(initialized):
    repo = initialized
    tid = _make_task(repo)
    # Run already terminal — cancel must not touch it.
    rid = _add_run(tid, pid=None, status="completed")

    out1 = team_orchestrator.cancel_team_task(tid)
    assert out1["count"] == 0
    assert _run_status(rid) == "completed"   # untouched
    assert _task_status(tid) == "cancelled"

    # Second call is a safe no-op.
    out2 = team_orchestrator.cancel_team_task(tid)
    assert out2["count"] == 0
    assert _task_status(tid) == "cancelled"


def test_cancel_missing_task_raises(initialized):
    with pytest.raises(KeyError):
        team_orchestrator.cancel_team_task(999999)
