"""Shared fixtures."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def jh_home(monkeypatch, tmp_path):
    """Isolated JohnStudio home for tests."""
    home = tmp_path / "jh-home"
    monkeypatch.setenv("JOHNSTUDIO_HOME", str(home))
    return home


@pytest.fixture
def git_repo(tmp_path):
    """Create a real, minimal git repo at tmp_path/repo and return its path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("# demo\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True)
    return repo
