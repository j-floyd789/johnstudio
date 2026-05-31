"""Tests for the built-in background workers (`johnstudio.workers_bg.*`).

Each worker is exercised by:
  - stubbing subprocess.run / filesystem state
  - emitting the appropriate event through a fresh hook bus
  - waiting for the runner thread to fire
  - asserting the correct external call was made (or skipped)
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

from johnstudio.background_workers import WorkerRegistry
from johnstudio.hooks import EventTypes, HookBus
from johnstudio.workers_bg.buildlog_append import BuildlogAppendWorker
from johnstudio.workers_bg.status_regen import StatusRegenWorker
from johnstudio.workers_bg.worktree_gc import WorktreeGCWorker


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_bus():
    b = HookBus()
    yield b
    b.clear()


@pytest.fixture
def fresh_registry(fresh_bus):
    r = WorkerRegistry(bus=fresh_bus)
    yield r
    r.clear()


def _wait_for_run(worker, count: int = 1, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(worker.recent_runs()) >= count:
            return
        time.sleep(0.01)
    raise AssertionError(f"worker {worker.name} did not finish a run in {timeout}s")


# ---------------------------------------------------------------------------
# StatusRegenWorker
# ---------------------------------------------------------------------------

def test_status_regen_invokes_both_scripts_when_present(
    fresh_bus, fresh_registry, tmp_path, monkeypatch,
):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "regen_status.py").write_text("# stub\n")
    (repo / "scripts" / "reconcile_task_state.py").write_text("# stub\n")

    calls: list[dict] = []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": list(cmd), "cwd": kwargs.get("cwd")})
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(
        "johnstudio.workers_bg.status_regen.subprocess.run", fake_run,
    )

    w = StatusRegenWorker()
    # Override the throttle so the test isn't slow (the framework still
    # runs the FIRST event immediately regardless of throttle).
    w.throttle_seconds = 0
    fresh_registry.register(w)
    fresh_registry.start()

    fresh_bus.emit(EventTypes.ARC_ITER_COMPLETE, {
        "project_repo": str(repo),
        "arc_name": "edge-hunt",
        "iter": 1,
    })
    _wait_for_run(w)

    runs = w.recent_runs()
    assert runs[0].ok is True, runs[0].error
    assert len(calls) == 2
    assert calls[0]["cmd"] == ["python3", "scripts/regen_status.py"]
    assert calls[1]["cmd"] == ["python3", "scripts/reconcile_task_state.py", "--write"]
    assert calls[0]["cwd"] == str(repo)


def test_status_regen_skips_missing_scripts(
    fresh_bus, fresh_registry, tmp_path, monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    # No scripts/ directory at all — both should be silently skipped.

    calls: list[list[str]] = []
    monkeypatch.setattr(
        "johnstudio.workers_bg.status_regen.subprocess.run",
        lambda cmd, **kw: calls.append(list(cmd)) or subprocess.CompletedProcess(cmd, 0),
    )

    w = StatusRegenWorker()
    w.throttle_seconds = 0
    fresh_registry.register(w)
    fresh_registry.start()
    fresh_bus.emit(EventTypes.TASK_MERGED, {"project_repo": str(repo)})
    _wait_for_run(w)
    assert calls == []
    assert w.recent_runs()[0].ok is True


def test_status_regen_skips_when_no_repo_in_payload(
    fresh_bus, fresh_registry, monkeypatch,
):
    def fake_run(cmd, **kw):  # pragma: no cover - must not be called
        raise AssertionError("subprocess.run was called despite missing repo")

    monkeypatch.setattr(
        "johnstudio.workers_bg.status_regen.subprocess.run", fake_run,
    )

    w = StatusRegenWorker()
    w.throttle_seconds = 0
    fresh_registry.register(w)
    fresh_registry.start()
    fresh_bus.emit(EventTypes.TASK_MERGED, {"task_id": 99})
    _wait_for_run(w)
    assert w.recent_runs()[0].ok is True  # skip is not a failure


def test_status_regen_failure_recorded(
    fresh_bus, fresh_registry, tmp_path, monkeypatch,
):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "regen_status.py").write_text("# stub\n")

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom!")

    monkeypatch.setattr(
        "johnstudio.workers_bg.status_regen.subprocess.run", fake_run,
    )
    w = StatusRegenWorker()
    w.throttle_seconds = 0
    fresh_registry.register(w)
    fresh_registry.start()
    fresh_bus.emit(EventTypes.TASK_MERGED, {"project_repo": str(repo)})
    _wait_for_run(w)
    assert w.recent_runs()[0].ok is False
    assert "exit=1" in (w.recent_runs()[0].error or "")


# ---------------------------------------------------------------------------
# WorktreeGCWorker
# ---------------------------------------------------------------------------

def test_worktree_gc_removes_matching_dirs(
    fresh_bus, fresh_registry, tmp_path, monkeypatch,
):
    repo = tmp_path / "repo"
    wt_root = repo / ".johnstudio" / "worktrees"
    (wt_root / "task-0042-feat-foo").mkdir(parents=True)
    (wt_root / "task-0042-other").mkdir(parents=True)
    (wt_root / "task-0099-bar").mkdir(parents=True)

    invocations: list[list[str]] = []

    def fake_run(cmd, **kw):
        invocations.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(
        "johnstudio.workers_bg.worktree_gc.subprocess.run", fake_run,
    )

    w = WorktreeGCWorker()
    w.throttle_seconds = 0
    fresh_registry.register(w)
    fresh_registry.start()
    fresh_bus.emit(EventTypes.TASK_MERGED, {
        "project_repo": str(repo),
        "task_number": 42,
    })
    _wait_for_run(w)
    assert w.recent_runs()[0].ok is True, w.recent_runs()[0].error

    removed = [c for c in invocations if "remove" in c]
    assert len(removed) == 2
    pruned = [c for c in invocations if c[:3] == ["git", "worktree", "prune"]]
    assert len(pruned) == 1
    # task-0099 left untouched.
    for c in removed:
        assert "task-0099-bar" not in " ".join(c)


def test_worktree_gc_idempotent_when_no_worktrees(
    fresh_bus, fresh_registry, tmp_path, monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    invocations: list[list[str]] = []
    monkeypatch.setattr(
        "johnstudio.workers_bg.worktree_gc.subprocess.run",
        lambda cmd, **kw: invocations.append(list(cmd))
        or subprocess.CompletedProcess(cmd, 0),
    )
    w = WorktreeGCWorker()
    w.throttle_seconds = 0
    fresh_registry.register(w)
    fresh_registry.start()
    fresh_bus.emit(EventTypes.TASK_MERGED, {
        "project_repo": str(repo), "task_number": 1,
    })
    _wait_for_run(w)
    # No removes (nothing to remove), but prune still runs.
    removes = [c for c in invocations if "remove" in c]
    prunes = [c for c in invocations if c[:3] == ["git", "worktree", "prune"]]
    assert removes == []
    assert len(prunes) == 1
    assert w.recent_runs()[0].ok is True


# ---------------------------------------------------------------------------
# BuildlogAppendWorker
# ---------------------------------------------------------------------------

def _seed_arc_state(repo: Path, arc_name: str, *, task_number: int, iter_num: int,
                    done_md_text: str = "") -> Path:
    arc_dir = repo / ".johnstudio" / "arcs" / arc_name
    arc_dir.mkdir(parents=True)
    done_md = arc_dir / f"iter-{iter_num:02d}" / "DONE.md"
    done_md.parent.mkdir(parents=True)
    done_md.write_text(done_md_text or f"# done\n\nIteration {iter_num} cleared.\n")
    state = {
        "name": arc_name,
        "current_iter": iter_num,
        "status": "cleared",
        "iterations": [
            {
                "iter": iter_num,
                "task_number": task_number,
                "artifact_path": str(done_md),
                "stop": True,
                "reason": "predicate cleared",
            }
        ],
    }
    (arc_dir / "STATE.json").write_text(json.dumps(state))
    return done_md


def test_buildlog_appends_one_line(fresh_bus, fresh_registry, tmp_path):
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    buildlog = repo / "docs" / "BUILDLOG.md"
    buildlog.write_text("- 2026-01-01 task-0001: seed line\n")
    _seed_arc_state(
        repo, "edge-hunt", task_number=42, iter_num=3,
        done_md_text="# done\n\nedge probe iter-3 — null result, moving on\n",
    )

    w = BuildlogAppendWorker()
    w.throttle_seconds = 0
    fresh_registry.register(w)
    fresh_registry.start()
    fresh_bus.emit(EventTypes.ARC_ITER_COMPLETE, {
        "project_repo": str(repo),
        "arc_name": "edge-hunt",
        "iter": 3,
    })
    _wait_for_run(w)
    assert w.recent_runs()[0].ok is True, w.recent_runs()[0].error
    text = buildlog.read_text()
    assert "task-0042" in text
    # The original seed line is still there.
    assert "task-0001: seed line" in text
    # Summary from DONE.md surfaced.
    assert "null result" in text


def test_buildlog_idempotent_when_last_line_already_mentions_task(
    fresh_bus, fresh_registry, tmp_path,
):
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    buildlog = repo / "docs" / "BUILDLOG.md"
    # Last line already references task-0042 — must not re-append.
    buildlog.write_text(
        "- 2026-01-01 task-0001: seed\n"
        "- 2026-05-29 task-0042 (edge-hunt iter 3): already done\n"
    )
    before = buildlog.read_text()
    _seed_arc_state(repo, "edge-hunt", task_number=42, iter_num=3)

    w = BuildlogAppendWorker()
    w.throttle_seconds = 0
    fresh_registry.register(w)
    fresh_registry.start()
    fresh_bus.emit(EventTypes.ARC_ITER_COMPLETE, {
        "project_repo": str(repo),
        "arc_name": "edge-hunt",
        "iter": 3,
    })
    _wait_for_run(w)
    assert buildlog.read_text() == before  # unchanged


def test_buildlog_skips_when_file_missing(
    fresh_bus, fresh_registry, tmp_path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    # No docs/BUILDLOG.md — worker must skip silently (not create it).
    _seed_arc_state(repo, "edge-hunt", task_number=42, iter_num=1)

    w = BuildlogAppendWorker()
    w.throttle_seconds = 0
    fresh_registry.register(w)
    fresh_registry.start()
    fresh_bus.emit(EventTypes.ARC_ITER_COMPLETE, {
        "project_repo": str(repo),
        "arc_name": "edge-hunt",
        "iter": 1,
    })
    _wait_for_run(w)
    assert not (repo / "docs" / "BUILDLOG.md").exists()
    assert w.recent_runs()[0].ok is True


def test_buildlog_skips_when_arc_state_missing(
    fresh_bus, fresh_registry, tmp_path,
):
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    bl = repo / "docs" / "BUILDLOG.md"
    bl.write_text("- existing\n")

    w = BuildlogAppendWorker()
    w.throttle_seconds = 0
    fresh_registry.register(w)
    fresh_registry.start()
    fresh_bus.emit(EventTypes.ARC_ITER_COMPLETE, {
        "project_repo": str(repo),
        "arc_name": "nonexistent",
        "iter": 1,
    })
    _wait_for_run(w)
    assert bl.read_text() == "- existing\n"


# ---------------------------------------------------------------------------
# Built-in registration helper
# ---------------------------------------------------------------------------

def test_register_all_registers_three_workers(fresh_registry):
    from johnstudio import workers_bg
    workers_bg.register_all(fresh_registry)
    names = sorted(w.name for w in fresh_registry.workers())
    assert names == ["buildlog-append", "status-regen", "worktree-gc"]
