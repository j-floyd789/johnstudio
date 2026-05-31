"""Tests for the team-mode approve flow: plan-validity gate (Item 2) and
the 202 + background spawn / no-double-spawn idempotency (Item 3).

These exercise team_orchestrator.approve_plan_and_run end-to-end against a
real sqlite + task folder, but stub out `spawn_and_track` so no real
worker subprocess is launched.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from johnstudio import (
    db,
    init as init_mod,
    project as project_mod,
    skill_importer,
    team_orchestrator,
)


VALID_PLAN = """\
# Team plan

## Summary
Add a small endpoint.

## Team
```yaml
claude_vp:
  - role: backend-developer
    brief: "Implement the endpoint."
    output: "app.py + RESULT.md"
```
"""

# backend-developer lives in claude_vp; placing it under codex_vp is an
# An invalid plan the gate must reject. (A role↔VP *mismatch* is now
# auto-corrected, so to exercise the reject/needs_replan path we use an
# UNKNOWN role, which the validator still refuses.)
INVALID_VP_PLAN = """\
# Team plan

## Summary
Add a small endpoint.

## Team
```yaml
claude_vp:
  - role: totally-fake-role
    brief: "Implement the endpoint."
    output: "app.py + RESULT.md"
```
"""


@pytest.fixture
def initialized(jh_home, git_repo):
    init_mod.run_init()
    project_mod.add_project("demo", git_repo)
    skill_importer.import_seeds()
    return git_repo


class _FakeSpawn:
    _next = 1000

    def __init__(self):
        _FakeSpawn._next += 1
        self.run_id = _FakeSpawn._next
        self.pid = 40000 + _FakeSpawn._next


def _begin(repo: Path, plan_md: str) -> int:
    """Create a team task, write a TEAM_PLAN.md, return task_db_id —
    without spawning the real planner."""
    out = team_orchestrator.begin_team_task(
        project_name="demo", task_text="add an endpoint",
    )
    tid = out["task_db_id"]
    tf = Path(out["task_folder"])
    (tf / "TEAM_PLAN.md").write_text(plan_md, encoding="utf-8")
    return tid


def _status(tid: int) -> str:
    conn = db.connect()
    try:
        return conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"]
    finally:
        conn.close()


def _patch_spawn(monkeypatch, sink: list | None = None, fail_on: int | None = None):
    calls = {"n": 0}

    def fake_spawn(**kwargs):
        i = calls["n"]
        calls["n"] += 1
        if fail_on is not None and i == fail_on:
            raise RuntimeError("simulated worktree failure")
        if sink is not None:
            sink.append(kwargs.get("role").name if kwargs.get("role") else None)
        return _FakeSpawn()

    monkeypatch.setattr(team_orchestrator, "spawn_and_track", fake_spawn)
    return calls


def _wait_spawn_done(tid, repo, task_number, timeout=5.0):
    tf = team_orchestrator._task_folder(repo, task_number)
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = team_orchestrator._read_team_state(tf)
        if st.get("spawn_state") in ("spawned", "failed"):
            return st
        time.sleep(0.02)
    return team_orchestrator._read_team_state(tf)


# ---------------------------------------------------------------------------
# Item 2 — plan-validity gate fires up front, never wedges at 'running'
# ---------------------------------------------------------------------------

def test_invalid_plan_parks_at_needs_replan_not_running(initialized, monkeypatch):
    repo = initialized
    _patch_spawn(monkeypatch)
    tid = _begin(repo, INVALID_VP_PLAN)

    out = team_orchestrator.approve_plan_and_run(tid)
    assert out.get("plan_invalid") is True
    assert out["status"] == "needs_replan"
    # The bad role↔VP pair is surfaced.
    assert "totally-fake-role" in out["error"]
    # Crucially the row is NOT stuck at 'running'.
    assert _status(tid) == "needs_replan"


def test_invalid_plan_retry_does_not_say_already_running(initialized, monkeypatch):
    repo = initialized
    _patch_spawn(monkeypatch)
    tid = _begin(repo, INVALID_VP_PLAN)

    first = team_orchestrator.approve_plan_and_run(tid)
    assert first.get("plan_invalid") is True
    # A second approve must NOT return already_running (the wedge bug).
    second = team_orchestrator.approve_plan_and_run(tid)
    assert second.get("already_running") is not True
    assert second.get("plan_invalid") is True


def test_replan_reissues_planner_after_invalid_plan(initialized, monkeypatch):
    repo = initialized
    sink: list = []
    _patch_spawn(monkeypatch, sink=sink)
    out = team_orchestrator.begin_team_task(project_name="demo", task_text="x")
    tid = out["task_db_id"]
    tf = Path(out["task_folder"])
    (tf / "TEAM_PLAN.md").write_text(INVALID_VP_PLAN, encoding="utf-8")

    team_orchestrator.approve_plan_and_run(tid)
    assert _status(tid) == "needs_replan"

    rp = team_orchestrator.replan_team_task(tid)
    assert rp.get("replanned") is True
    assert _status(tid) == "planning"
    # The lead-planner was spawned again.
    assert "lead-planner" in sink
    # The rejected plan was archived.
    assert not (tf / "TEAM_PLAN.md").exists() or list(tf.glob("TEAM_PLAN.rejected.*.md"))


# ---------------------------------------------------------------------------
# Item 3 — 202 + background spawn, single-winner idempotency
# ---------------------------------------------------------------------------

def test_valid_plan_returns_accepted_immediately(initialized, monkeypatch):
    repo = initialized
    sink: list = []
    _patch_spawn(monkeypatch, sink=sink)
    out = team_orchestrator.begin_team_task(project_name="demo", task_text="x")
    tid = out["task_db_id"]
    tf = Path(out["task_folder"])
    (tf / "TEAM_PLAN.md").write_text(VALID_PLAN, encoding="utf-8")

    res = team_orchestrator.approve_plan_and_run(tid)
    assert res.get("accepted") is True
    assert res["status"] == "running"
    assert res["expected_specialists"] >= 1
    # Row flips to running synchronously (gate), spawn happens in bg.
    assert _status(tid) == "running"

    st = _wait_spawn_done(tid, repo, out["task_number"])
    assert st["spawn_state"] == "spawned"
    # backend-developer + standing-rule code-reviewer at least.
    assert "backend-developer" in sink


def test_concurrent_approve_spawns_team_once(initialized, monkeypatch):
    repo = initialized
    sink: list = []
    lock = threading.Lock()

    # Slow the spawn so both threads overlap inside approve.
    def fake_spawn(**kwargs):
        time.sleep(0.05)
        with lock:
            sink.append(kwargs["role"].name)
        return _FakeSpawn()

    monkeypatch.setattr(team_orchestrator, "spawn_and_track", fake_spawn)

    out = team_orchestrator.begin_team_task(project_name="demo", task_text="x")
    tid = out["task_db_id"]
    tf = Path(out["task_folder"])
    (tf / "TEAM_PLAN.md").write_text(VALID_PLAN, encoding="utf-8")

    results: list = []

    def call():
        results.append(team_orchestrator.approve_plan_and_run(tid))

    threads = [threading.Thread(target=call) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    accepted = [r for r in results if r.get("accepted")]
    already = [r for r in results if r.get("already_running")]
    # Exactly one caller wins the gate; the rest see already_running.
    assert len(accepted) == 1, results
    assert len(already) == 3, results

    st = _wait_spawn_done(tid, repo, out["task_number"])
    assert st["spawn_state"] == "spawned"
    # The team was spawned exactly once: no role appears twice.
    assert len(sink) == len(set(sink)), sink


def test_background_spawn_failure_rolls_back_to_planning(initialized, monkeypatch):
    repo = initialized
    # Fail on the 2nd specialist so the loop aborts partway.
    _patch_spawn(monkeypatch, fail_on=1)
    out = team_orchestrator.begin_team_task(project_name="demo", task_text="x")
    tid = out["task_db_id"]
    tf = Path(out["task_folder"])
    (tf / "TEAM_PLAN.md").write_text(VALID_PLAN, encoding="utf-8")

    res = team_orchestrator.approve_plan_and_run(tid)
    assert res.get("accepted") is True

    st = _wait_spawn_done(tid, repo, out["task_number"])
    assert st["spawn_state"] == "failed"
    # Rolled back so the user can retry without 'already_running'.
    assert _status(tid) == "planning"
