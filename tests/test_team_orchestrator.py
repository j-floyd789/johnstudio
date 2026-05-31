"""Coverage for team_orchestrator: state machine, idempotency gates,
traversal guard, needs-changes detection, orphan recovery.

team_orchestrator.py is 1,400+ LOC and had zero direct tests before
this file. We focus on the deterministic surfaces — anything that can
be exercised without spawning a real Claude CLI.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from johnstudio import db, project as project_mod, team_orchestrator as to


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def coolproject(jh_home, git_repo):
    """Register a test project so team_orchestrator helpers can resolve it."""
    proj = project_mod.add_project("demo", git_repo)
    return {"project_name": "demo", "repo_path": git_repo, "project_id": proj["project_id"]}


def _mk_task(coolproject, status="planning", budget_usd=None):
    conn = db.connect()
    db.init_schema(conn)
    cur = conn.execute(
        """INSERT INTO tasks (project_id, task_number, title, description, status, base_branch, budget_usd)
           VALUES (?,?,?,?,?,?,?) RETURNING id, task_number""",
        (coolproject["project_id"], 1, "t", "the task", status, "main", budget_usd),
    )
    row = cur.fetchone()
    tid = int(row["id"])
    tf = Path(coolproject["repo_path"]) / ".johnstudio" / "tasks" / f"task-{int(row['task_number']):04d}"
    tf.mkdir(parents=True, exist_ok=True)
    (tf / "TEAM_STATE.json").write_text(
        json.dumps({
            "task_db_id": tid, "task_number": int(row["task_number"]),
            "project_name": coolproject["project_name"],
            "status": status, "assignments": [], "plan": {"summary": "x", "assignments": []},
        }),
        encoding="utf-8",
    )
    conn.commit(); conn.close()
    return tid, tf


# ---------------------------------------------------------------------------
# _try_transition: SQL-rowcount-gated idempotency
# ---------------------------------------------------------------------------

def test_try_transition_wins_once(coolproject):
    tid, _ = _mk_task(coolproject, status="planning")
    assert to._try_transition(tid, "planning", "running") is True
    # Second caller with the same precondition loses.
    assert to._try_transition(tid, "planning", "running") is False


def test_try_transition_rejects_wrong_precondition(coolproject):
    tid, _ = _mk_task(coolproject, status="running")
    assert to._try_transition(tid, "planning", "anything") is False


def test_advance_no_op_for_terminal_states(coolproject):
    tid, _ = _mk_task(coolproject, status="merged")
    out = to.advance_team_task(tid)
    assert out["status"] == "merged"
    assert out["no_op"] is True


# ---------------------------------------------------------------------------
# Traversal guard on _resolve_artifact_path
# ---------------------------------------------------------------------------

def test_resolve_artifact_rejects_absolute_path(tmp_path):
    """The planner-influenced reads list should never resolve absolute
    paths even if the file exists."""
    target = tmp_path / "secret.txt"; target.write_text("nope")
    out = to._resolve_artifact_path(str(target), tmp_path / "tf", {"assignments": []})
    assert out is None


def test_resolve_artifact_rejects_traversal(tmp_path):
    tf = tmp_path / "tf"; tf.mkdir()
    secret = tmp_path / "secret.txt"; secret.write_text("nope")
    # ../secret.txt should NOT resolve, even though the file exists.
    out = to._resolve_artifact_path("../secret.txt", tf, {"assignments": []})
    assert out is None


def test_resolve_artifact_finds_within_task_folder(tmp_path):
    tf = tmp_path / "tf"; tf.mkdir()
    (tf / "RFC.md").write_text("ok")
    out = to._resolve_artifact_path("RFC.md", tf, {"assignments": []})
    assert out is not None and out.read_text() == "ok"


def test_resolve_artifact_finds_within_team_notes_subfolder(tmp_path):
    tf = tmp_path / "tf"; (tf / "team_notes").mkdir(parents=True)
    (tf / "team_notes" / "CROSS_REVIEW.md").write_text("ok")
    out = to._resolve_artifact_path("CROSS_REVIEW.md", tf, {"assignments": []})
    assert out is not None


def test_resolve_artifact_finds_within_worktree(tmp_path):
    tf = tmp_path / "tf"; tf.mkdir()
    wt = tmp_path / "wt"; wt.mkdir()
    (wt / "RESULT.md").write_text("yes")
    state = {"assignments": [{"role": "backend-developer", "worktree": str(wt)}]}
    out = to._resolve_artifact_path("RESULT.md", tf, state)
    assert out is not None


# ---------------------------------------------------------------------------
# _check_needs_revision: parses verdicts across cross-review files
# ---------------------------------------------------------------------------

def test_check_needs_revision_returns_empty_when_no_files(coolproject):
    tid, tf = _mk_task(coolproject, status="reviewing")
    state = json.loads((tf / "TEAM_STATE.json").read_text())
    out = to._check_needs_revision(state, Path(coolproject["repo_path"]), 1)
    assert out == []


def test_check_needs_revision_routes_to_editor_specialists(coolproject):
    tid, tf = _mk_task(coolproject, status="reviewing")
    notes = tf / "team_notes"; notes.mkdir(parents=True, exist_ok=True)
    (notes / "CROSS_REVIEW_security-auditor_0.md").write_text(
        "# Cross review\n## Verdict: needs-changes\n## Required\n1. Sanitize input.\n",
        encoding="utf-8",
    )
    state = json.loads((tf / "TEAM_STATE.json").read_text())
    # Backend-developer is can_edit=True per the catalog → it's the
    # legitimate target of the revision feedback.
    state["assignments"] = [
        {"role": "backend-developer", "vp": "claude_vp",
         "worktree": str(tf / "wt-backend")},
    ]
    (tf / "wt-backend").mkdir(parents=True, exist_ok=True)
    out = to._check_needs_revision(state, Path(coolproject["repo_path"]), 1)
    assert len(out) == 1
    assert out[0]["assignment"]["role"] == "backend-developer"
    assert "Sanitize" in out[0]["review_text"]


def test_check_needs_revision_ignores_approve_verdicts(coolproject):
    tid, tf = _mk_task(coolproject, status="reviewing")
    notes = tf / "team_notes"; notes.mkdir(parents=True, exist_ok=True)
    (notes / "CROSS_REVIEW_x.md").write_text("## Verdict: approve\nGreat work.\n")
    state = json.loads((tf / "TEAM_STATE.json").read_text())
    state["assignments"] = [{"role": "backend-developer", "vp": "claude_vp",
                              "worktree": str(tf / "wt")}]
    (tf / "wt").mkdir()
    out = to._check_needs_revision(state, Path(coolproject["repo_path"]), 1)
    assert out == []


# ---------------------------------------------------------------------------
# Budget guard on approve_plan_and_run
# ---------------------------------------------------------------------------

def test_check_budget_reports_over(coolproject):
    tid, _ = _mk_task(coolproject, status="planning", budget_usd=0.10)
    # Drop a cost-bearing event in directly.
    from johnstudio import worker_events
    conn = db.connect(); conn.execute("UPDATE tasks SET cost_usd = 0.50 WHERE id = ?", (tid,))
    conn.commit(); conn.close()
    bs = to.check_budget(tid)
    assert bs["over_budget"] is True


def test_approve_refuses_over_budget_task(coolproject):
    tid, tf = _mk_task(coolproject, status="planning", budget_usd=0.10)
    conn = db.connect(); conn.execute("UPDATE tasks SET cost_usd = 999 WHERE id = ?", (tid,))
    conn.commit(); conn.close()
    out = to.approve_plan_and_run(tid)
    assert out.get("refused") is True
    assert out["reason"] == "budget_exceeded"


# ---------------------------------------------------------------------------
# recover_orphan_runs: dead PIDs marked stopped, live PIDs reattached
# ---------------------------------------------------------------------------

def test_recover_orphan_runs_marks_dead_pid_as_stopped(coolproject):
    tid, tf = _mk_task(coolproject, status="planning")
    # Insert a worker + run with a PID that definitely doesn't exist.
    conn = db.connect()
    cur = conn.execute(
        """INSERT INTO workers (name, provider, role, command, can_edit, worktree_enabled)
           VALUES ('w','claude','x','claude',1,1) RETURNING id"""
    )
    wid = int(cur.fetchone()["id"])
    cur = conn.execute(
        """INSERT INTO runs (task_id, worker_id, status, prompt_path, pid, started_at)
           VALUES (?,?,?,?,?,?) RETURNING id""",
        (tid, wid, "launched", str(tf / "prompts" / "x.md"), 99999999, "2026-01-01"),
    )
    rid = int(cur.fetchone()["id"])
    conn.commit(); conn.close()

    out = to.recover_orphan_runs()
    assert out["marked_stopped"] >= 1

    # Verify the row was actually flipped.
    conn = db.connect()
    row = conn.execute("SELECT status FROM runs WHERE id = ?", (rid,)).fetchone()
    conn.close()
    assert row["status"] == "stopped"


def test_pid_alive_handles_nonexistent():
    assert to._pid_alive(99999999) is False


def test_pid_alive_self():
    assert to._pid_alive(os.getpid()) is True


# ---------------------------------------------------------------------------
# get_team_state shape
# ---------------------------------------------------------------------------

def test_get_team_state_returns_task_metadata(coolproject):
    tid, tf = _mk_task(coolproject, status="planning")
    out = to.get_team_state(tid)
    assert out["task_db_id"] == tid
    assert out["task_number"] == 1
    assert out["project_name"] == "demo"
    assert out["plan_exists"] is False


def test_get_team_state_with_plan(coolproject):
    tid, tf = _mk_task(coolproject, status="planning")
    plan_md = """# Plan

## Summary
A small change.

## Team
```yaml
claude_vp:
  - role: backend-developer
    brief: "Implement X."
    output: "RESULT.md"
```
"""
    (tf / "TEAM_PLAN.md").write_text(plan_md, encoding="utf-8")
    out = to.get_team_state(tid)
    assert out["plan_exists"] is True
    assert out["plan_valid"] is True
    assert out["plan"]["summary"].startswith("A small change")
    assert out["plan"]["assignments"][0]["role"] == "backend-developer"


def test_get_team_state_reports_invalid_plan(coolproject):
    tid, tf = _mk_task(coolproject, status="planning")
    (tf / "TEAM_PLAN.md").write_text("# Plan\n\nno team section here", encoding="utf-8")
    out = to.get_team_state(tid)
    assert out["plan_exists"] is True
    assert out["plan_valid"] is False
    assert "missing" in (out.get("plan_error") or "").lower() or out.get("plan_error")
