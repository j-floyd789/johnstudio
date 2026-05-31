from __future__ import annotations

import subprocess
from pathlib import Path

from johnstudio import git_worktree as gw


def test_branch_name_and_path():
    assert gw.branch_name_for(1, "claude_backend") == "ai/task-0001/claude-backend"
    p = gw.worktree_path_for("/x", 7, "terminal_stub")
    assert p == Path("/x/.johnstudio/worktrees/task-0007-terminal-stub")


def test_add_remove_worktree(git_repo, tmp_path):
    wt = tmp_path / "wt"
    branch = "ai/task-0001/stub"
    gw.add_worktree(git_repo, wt, branch, base="main")
    assert wt.exists()
    assert (wt / "README.md").exists()
    lst = gw.list_worktrees(git_repo)
    assert any(w.get("worktree") and Path(w["worktree"]).resolve() == wt.resolve() for w in lst)
    gw.remove_worktree(git_repo, wt, force=True)
    assert not wt.exists()


def test_diff_against(git_repo, tmp_path):
    wt = tmp_path / "wt"
    gw.add_worktree(git_repo, wt, "ai/task-0001/diffs", base="main")
    (wt / "new.md").write_text("hello\n")
    subprocess.run(["git", "-C", str(wt), "add", "."], check=True)
    subprocess.run(["git", "-C", str(wt), "commit", "-qm", "add new"], check=True)
    diff = gw.diff_against(wt, base="main")
    assert "new.md" in diff
    stat = gw.diff_stat(wt, base="main")
    assert "new.md" in stat


def test_is_clean_and_status(git_repo):
    assert gw.is_clean(git_repo)
    # Untracked file does NOT make the tree "dirty" by default (merge-safe semantics).
    (Path(git_repo) / "junk.txt").write_text("x")
    assert gw.is_clean(git_repo)
    assert not gw.is_clean(git_repo, include_untracked=True)
    # But modifying a TRACKED file does mark dirty.
    (Path(git_repo) / "README.md").write_text("# changed\n")
    assert not gw.is_clean(git_repo)
    assert "junk.txt" in gw.status(git_repo)


def test_merge_branch_dry_run(git_repo, tmp_path):
    wt = tmp_path / "wt"
    branch = "ai/task-0001/merge-test"
    gw.add_worktree(git_repo, wt, branch, base="main")
    (wt / "added.md").write_text("hi\n")
    subprocess.run(["git", "-C", str(wt), "add", "."], check=True)
    subprocess.run(["git", "-C", str(wt), "commit", "-qm", "add"], check=True)
    # Dry-run merge on base
    code, out = gw.merge_branch(git_repo, branch, dry_run=True)
    assert code == 0
    assert gw.is_clean(git_repo), "Dry-run should leave working tree clean"
