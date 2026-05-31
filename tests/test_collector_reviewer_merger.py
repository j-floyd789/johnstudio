from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from johnstudio import (
    collector,
    init as init_mod,
    merger,
    orchestrator,
    project as project_mod,
    reviewer,
    skill_importer,
)


@pytest.fixture
def stub_task(jh_home, git_repo):
    init_mod.run_init()
    project_mod.add_project("demo", git_repo)
    skill_importer.import_seeds()
    r = orchestrator.run("demo", "demo stub task", stub_only=True)
    assert orchestrator.wait_for_done(git_repo, r["task_number"], timeout=15.0)
    return r["task_number"], git_repo


def test_collect_writes_diffs_and_results(stub_task):
    task_n, repo = stub_task
    s = collector.collect(task_n, "demo")
    assert s["runs"]
    run = s["runs"][0]
    assert run["worker"] == "terminal_stub"
    assert run["done_present"]
    assert run["result_present"]
    # Stub created STUB_NOTE.md → diff should include it OR git status should.
    assert "STUB_NOTE.md" in run["files_changed"]
    task_folder = repo / ".johnstudio" / "tasks" / f"task-{task_n:04d}"
    assert (task_folder / "results" / "terminal_stub_RESULT.md").exists()
    assert (task_folder / "diffs" / "terminal_stub.diff").exists()


def test_collect_flags_protected_path(jh_home, git_repo):
    init_mod.run_init()
    project_mod.add_project("demo", git_repo)
    skill_importer.import_seeds()
    r = orchestrator.run("demo", "task with env", stub_only=True)
    assert orchestrator.wait_for_done(git_repo, r["task_number"], timeout=15.0)
    # Add a .env file to the stub worktree so collector flags it.
    wt = next((git_repo / ".johnstudio" / "worktrees").glob(f"task-{r['task_number']:04d}-*"))
    (wt / ".env").write_text("SECRET=x\n")
    subprocess.run(["git", "-C", str(wt), "add", "."], check=True)
    subprocess.run(["git", "-C", str(wt), "commit", "-qm", "leak"], check=True)
    s = collector.collect(r["task_number"], "demo")
    assert any(".env" in p for p in s["runs"][0]["protected_path_hits"])


def test_review_produces_final_review_and_merge_plan(stub_task):
    task_n, repo = stub_task
    r = reviewer.review(task_n, "demo")
    assert Path(r["final_review_path"]).exists()
    assert Path(r["merge_plan_path"]).exists()
    assert r["recommended"] == "terminal_stub"
    fr = Path(r["final_review_path"]).read_text()
    assert "Scores" in fr
    assert "terminal_stub" in fr


def test_merge_requires_confirmation(stub_task):
    task_n, repo = stub_task
    with pytest.raises(merger.MergeAborted):
        merger.merge(task_n, "demo", "terminal_stub")  # no confirm + no assume_yes


def test_merge_with_dry_run_keeps_tree_clean(stub_task):
    task_n, repo = stub_task
    out = merger.merge(task_n, "demo", "terminal_stub", dry_run=True)
    assert out["dry_run"] is True
    # working tree must have no modifications to tracked files (untracked .johnstudio is fine)
    cp = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain", "-uno"],
        capture_output=True, text=True,
    )
    assert cp.stdout.strip() == ""


def test_merge_success_updates_memory_and_graph(stub_task):
    task_n, repo = stub_task
    out = merger.merge(task_n, "demo", "terminal_stub", confirm=True)
    assert out["merged"] is True
    # Decision file written
    decs = list((repo / ".johnstudio" / "memory" / "decisions").glob("*.md"))
    assert decs
    # Graph entities updated (task + decision pages)
    tasks_dir = repo / ".johnstudio" / "memory" / "graph" / "tasks"
    dec_dir = repo / ".johnstudio" / "memory" / "graph" / "decisions"
    assert list(tasks_dir.glob("*.md"))
    assert list(dec_dir.glob("*.md"))
    # The merged change should be present on base branch
    cp = subprocess.run(
        ["git", "-C", str(repo), "log", "--oneline", "-5"],
        capture_output=True, text=True,
    )
    assert "Merge" in cp.stdout or "stub" in cp.stdout.lower()


def test_merge_refuses_when_tree_dirty(stub_task):
    task_n, repo = stub_task
    # Modify a tracked file — that's what blocks a merge (untracked files are fine).
    (repo / "README.md").write_text("# dirty\n")
    with pytest.raises(merger.MergeAborted):
        merger.merge(task_n, "demo", "terminal_stub", confirm=True)
