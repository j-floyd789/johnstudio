from __future__ import annotations

from pathlib import Path

import pytest

from johnstudio import project as project_mod


def test_detect_stack_nextjs_typescript(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "package.json").write_text("{}")
    (repo / "tsconfig.json").write_text("{}")
    (repo / "next.config.ts").write_text("")
    (repo / "tailwind.config.ts").write_text("")
    s = project_mod.detect_stack(repo)
    assert "javascript" in s.languages and "typescript" in s.languages
    assert "nextjs" in s.frameworks and "tailwind" in s.frameworks
    assert "npm" in s.package_managers


def test_detect_stack_python(tmp_path):
    repo = tmp_path / "p"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("")
    s = project_mod.detect_stack(repo)
    assert s.languages == ["python"]


def test_detect_stack_empty(tmp_path):
    (tmp_path / "empty").mkdir()
    s = project_mod.detect_stack(tmp_path / "empty")
    assert s.languages == [] and s.frameworks == [] and s.detected_files == []


def test_add_project_rejects_non_git(jh_home, tmp_path):
    repo = tmp_path / "notgit"
    repo.mkdir()
    with pytest.raises(project_mod.NotAGitRepoError):
        project_mod.add_project("demo", repo)


def test_add_project_rejects_missing(jh_home, tmp_path):
    with pytest.raises(FileNotFoundError):
        project_mod.add_project("demo", tmp_path / "nope")


def test_add_project_creates_everything(jh_home, git_repo):
    status = project_mod.add_project("demo", git_repo)

    assert status["base_branch"] == "main"
    # project.yaml written
    assert (git_repo / ".johnstudio" / "project.yaml").exists()
    # memory vault
    memdir = git_repo / ".johnstudio" / "memory"
    assert (memdir / "00_index.md").exists()
    assert (memdir / "decisions").is_dir()
    assert (memdir / "graph" / "projects").is_dir()
    # tasks/worktrees/logs scaffolding
    assert (git_repo / ".johnstudio" / "tasks").is_dir()
    assert (git_repo / ".johnstudio" / "worktrees").is_dir()
    # project entity page
    entity = memdir / "graph" / "projects" / "Project - demo.md"
    assert entity.exists()
    text = entity.read_text()
    assert "type: project" in text
    assert "name: demo" in text


def test_add_project_then_list(jh_home, git_repo):
    project_mod.add_project("demo", git_repo)
    rows = project_mod.list_projects()
    assert len(rows) == 1
    assert rows[0]["name"] == "demo"


def test_add_project_idempotent(jh_home, git_repo):
    project_mod.add_project("demo", git_repo)
    status2 = project_mod.add_project("demo", git_repo)
    assert status2["project_id"] >= 1
    rows = project_mod.list_projects()
    assert len(rows) == 1  # ON CONFLICT(name) → UPDATE, not duplicate insert


def test_add_project_default_test_commands(jh_home, git_repo):
    (git_repo / "pyproject.toml").write_text("")
    project_mod.add_project("demo", git_repo)
    from johnstudio import config
    cfg = config.load_project_config(git_repo)
    assert "pytest -q" in cfg.test_commands
