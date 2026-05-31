"""End-to-end chain tests using the chain-aware terminal_stub.

Each `run_phase` is synchronous when the worker is the stub (it returns when the
subprocess exits). We just need to call `complete_current_phase_if_ready` after
each phase and the state machine moves forward.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from johnstudio import (
    chain,
    init as init_mod,
    project as project_mod,
)


# All chain phases use claude_backend by default, but the stub is selected when
# you override the worker config. We override the workers map in the global
# config so every phase runs through the stub instead of real claude.
@pytest.fixture
def stub_chain_home(monkeypatch, tmp_path):
    monkeypatch.setenv("JOHNSTUDIO_HOME", str(tmp_path / "home"))
    init_mod.run_init()
    # Rewrite global config so claude_backend uses the stub command.
    from johnstudio import config
    import yaml as _yaml
    cfg_path = config.global_config_path()
    cfg = _yaml.safe_load(cfg_path.read_text())
    for w in ("claude_backend", "claude_frontend", "codex_tests", "gemini_review", "security_review", "claude_review"):
        cfg["workers"][w] = {
            "provider": "terminal",
            "command": "python -m johnstudio.workers.stub",
            "role": cfg["workers"][w]["role"],
            "can_edit": cfg["workers"][w]["can_edit"],
            "worktree": cfg["workers"][w]["worktree"],
            "max_runtime_minutes": 5,
            "always_available": True,
        }
    cfg_path.write_text(_yaml.safe_dump(cfg, sort_keys=False))
    return tmp_path


@pytest.fixture
def chain_project(stub_chain_home, git_repo):
    project_mod.add_project("demo", git_repo)
    return git_repo


def _wait_phase_done(task_db_id: int, timeout: float = 10.0) -> dict:
    """Poll `complete_current_phase_if_ready` until it advances or times out."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        out = chain.complete_current_phase_if_ready(task_db_id)
        if out.get("completed") or out.get("awaiting_human") or out.get("terminal"):
            return out
        time.sleep(0.1)
    raise TimeoutError(f"phase didn't complete in {timeout}s: {out}")


def _advance_and_relaunch(task_db_id: int, *, timeout: float = 10.0) -> dict:
    """Wait for current phase, advance, then if next phase is non-human launch it."""
    done = _wait_phase_done(task_db_id, timeout=timeout)
    cur = chain.current_phase(task_db_id)
    if cur and cur.phase not in chain.HUMAN_GATES and cur.phase not in chain.TERMINAL and cur.status == "pending":
        chain.run_phase(task_db_id)
    return done


# ---------------------------------------------------------------------------
# Happy path: RFC → approve → impl → review-approve → merge
# ---------------------------------------------------------------------------

def test_chain_happy_path(chain_project):
    # Drive every verdict to "approve"
    os.environ["STUB_RFC_VERDICT"] = "approve"
    os.environ["STUB_REVIEW_VERDICT_1"] = "approve"

    start = chain.begin_chain(project_name="demo", task_text="add a hello endpoint")
    tid = start["task_db_id"]

    # Phase 1: RFC drafting
    chain.run_phase(tid)
    out = _advance_and_relaunch(tid)
    assert out["completed"] == "rfc_drafting"
    assert out["next"] == "rfc_review"

    # Phase 2: RFC review
    out = _advance_and_relaunch(tid)
    assert out["completed"] == "rfc_review"
    assert out["verdict"] == "approve"
    assert out["next"] == "rfc_pending_approval"
    assert out["human_gate"] is True

    # Human approves the RFC
    chain.approve_rfc(tid)
    cur = chain.current_phase(tid)
    assert cur.phase == chain.Phase.IMPLEMENTING

    # Phase 3: Implementing
    chain.run_phase(tid)
    out = _advance_and_relaunch(tid)
    assert out["completed"] == "implementing"
    assert out["next"] == "reviewing"

    # Phase 4: Reviewing round 1 — approve
    out = _advance_and_relaunch(tid)
    assert out["completed"] == "reviewing"
    assert out["verdict"] == "approve"
    assert out["next"] == "pending_merge"
    assert out["human_gate"] is True

    # Human triggers merge bookkeeping (real merge handled by merger.merge)
    chain.mark_merged(tid)
    cur = chain.current_phase(tid)
    assert cur.phase == chain.Phase.MERGED


# ---------------------------------------------------------------------------
# Review pushes back once, then approves: one revision round
# ---------------------------------------------------------------------------

def test_chain_one_revise_round(chain_project):
    os.environ["STUB_RFC_VERDICT"] = "approve"
    os.environ["STUB_REVIEW_VERDICT_1"] = "needs-changes"
    os.environ["STUB_REVIEW_VERDICT_2"] = "approve"

    start = chain.begin_chain(project_name="demo", task_text="add /api/health")
    tid = start["task_db_id"]

    # RFC drafting + review
    chain.run_phase(tid); _advance_and_relaunch(tid)   # → rfc_review
    _advance_and_relaunch(tid)                          # → rfc_pending_approval
    chain.approve_rfc(tid)                              # → implementing

    chain.run_phase(tid); _advance_and_relaunch(tid)   # impl → reviewing
    # Reviewer says needs-changes
    out = _advance_and_relaunch(tid)
    assert out["completed"] == "reviewing"
    assert out["verdict"] == "needs-changes"
    assert out["next"] == "revising"

    # Revise then re-review → approve
    out = _advance_and_relaunch(tid)
    assert out["completed"] == "revising"
    assert out["next"] == "reviewing"

    out = _advance_and_relaunch(tid)
    assert out["completed"] == "reviewing"
    assert out["verdict"] == "approve"
    assert out["next"] == "pending_merge"


# ---------------------------------------------------------------------------
# Reviewer pushes back twice: hits conflict
# ---------------------------------------------------------------------------

def test_chain_conflict_after_max_rounds(chain_project):
    os.environ["STUB_RFC_VERDICT"] = "approve"
    os.environ["STUB_REVIEW_VERDICT_1"] = "needs-changes"
    os.environ["STUB_REVIEW_VERDICT_2"] = "needs-changes"

    start = chain.begin_chain(project_name="demo", task_text="big refactor")
    tid = start["task_db_id"]

    chain.run_phase(tid); _advance_and_relaunch(tid)
    _advance_and_relaunch(tid)
    chain.approve_rfc(tid)
    chain.run_phase(tid); _advance_and_relaunch(tid)
    _advance_and_relaunch(tid)   # review round 1 → needs-changes → revising
    _advance_and_relaunch(tid)   # revising → reviewing round 2

    out = _advance_and_relaunch(tid)
    assert out["completed"] == "reviewing"
    assert out["verdict"] == "needs-changes"
    assert out["next"] == "conflict"
    assert out["human_gate"] is True


# ---------------------------------------------------------------------------
# RFC review says reject → human rejects RFC → chain terminates
# ---------------------------------------------------------------------------

def test_chain_rfc_rejected_by_human(chain_project):
    os.environ["STUB_RFC_VERDICT"] = "needs-changes"

    start = chain.begin_chain(project_name="demo", task_text="risky thing")
    tid = start["task_db_id"]

    chain.run_phase(tid); _advance_and_relaunch(tid)
    out = _advance_and_relaunch(tid)
    assert out["next"] == "rfc_pending_approval"

    chain.reject_rfc(tid, reason="scope is wrong")
    cur = chain.current_phase(tid)
    assert cur.phase == chain.Phase.REJECTED


# ---------------------------------------------------------------------------
# Phase rows are inspectable
# ---------------------------------------------------------------------------

def test_chain_phase_rows_recorded(chain_project):
    os.environ["STUB_RFC_VERDICT"] = "approve"
    os.environ["STUB_REVIEW_VERDICT_1"] = "approve"

    start = chain.begin_chain(project_name="demo", task_text="t")
    tid = start["task_db_id"]
    chain.run_phase(tid); _advance_and_relaunch(tid)
    _advance_and_relaunch(tid)
    chain.approve_rfc(tid)
    chain.run_phase(tid); _advance_and_relaunch(tid)
    _advance_and_relaunch(tid)

    phases = chain.list_phases(tid)
    names = [p.phase.value for p in phases]
    # Every phase from rfc_drafting through pending_merge should be recorded.
    assert "rfc_drafting" in names
    assert "rfc_review" in names
    assert "rfc_pending_approval" in names
    assert "implementing" in names
    assert "reviewing" in names
    assert "pending_merge" in names
    # The approved rfc_review row has verdict=approve
    rfc_rev = [p for p in phases if p.phase == chain.Phase.RFC_REVIEW][0]
    assert rfc_rev.verdict == chain.Verdict.APPROVE
