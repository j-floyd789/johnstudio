from __future__ import annotations

from pathlib import Path

import pytest

from johnstudio import (
    init as init_mod,
    orchestrator,
    project as project_mod,
    skill_importer,
)


@pytest.fixture
def initialized(jh_home, git_repo):
    init_mod.run_init()
    project_mod.add_project("demo", git_repo)
    skill_importer.import_seeds()
    return git_repo


def test_dry_run_does_not_create_worktrees(initialized, git_repo):
    r = orchestrator.run("demo", "add a hello endpoint", dry_run=True, stub_only=True)
    assert r["dry_run"] is True
    assert r["team"] == ["terminal_stub"]
    folder = Path(r["task_folder"])
    assert (folder / "TASK.md").exists()
    assert (folder / "DRY_RUN_PLAN.md").exists()
    # No worktree created on dry-run.
    wts = list((git_repo / ".johnstudio" / "worktrees").glob(f"task-{r['task_number']:04d}-*"))
    assert wts == []


def test_stub_only_run_e2e(initialized, git_repo):
    r = orchestrator.run("demo", "add a hello endpoint", stub_only=True)
    assert r["team"] == ["terminal_stub"]
    # Worktree should have been created.
    wts = list((git_repo / ".johnstudio" / "worktrees").glob(f"task-{r['task_number']:04d}-*"))
    assert len(wts) == 1
    # The stub worker writes RESULT.md and DONE.md within ~few seconds.
    done = orchestrator.wait_for_done(git_repo, r["task_number"], timeout=15.0)
    assert done, "terminal_stub did not write DONE.md in time"
    for wt in wts:
        assert (wt / "RESULT.md").exists()
        assert (wt / "DONE.md").exists()
        assert (wt / "STUB_NOTE.md").exists()


def test_status_reports_runs(initialized, git_repo):
    r = orchestrator.run("demo", "task A", stub_only=True)
    orchestrator.wait_for_done(git_repo, r["task_number"], timeout=15.0)
    s = orchestrator.status(r["task_number"], "demo")
    assert s["task_number"] == r["task_number"]
    assert s["runs"]
    assert all(run["done_md_exists"] for run in s["runs"])


def test_stop_marks_runs_stopped(initialized, git_repo):
    r = orchestrator.run("demo", "task B", stub_only=True)
    out = orchestrator.stop(r["task_number"], "demo")
    assert out["task_number"] == r["task_number"]
    s = orchestrator.status(r["task_number"], "demo")
    assert s["status"] == "stopped"


def test_cleanup_prunes_worktrees(initialized, git_repo):
    r = orchestrator.run("demo", "task C", stub_only=True)
    orchestrator.wait_for_done(git_repo, r["task_number"], timeout=15.0)
    orchestrator.stop(r["task_number"], "demo")
    out = orchestrator.cleanup(r["task_number"], "demo", prune_worktrees=True)
    assert len(out["removed_worktrees"]) == 1


def test_resume_rewrites_prompt(initialized, git_repo):
    r = orchestrator.run("demo", "task D", stub_only=True)
    orchestrator.wait_for_done(git_repo, r["task_number"], timeout=15.0)
    out = orchestrator.resume(r["task_number"], "demo", "terminal_stub")
    assert Path(out["prompt"]).exists()
