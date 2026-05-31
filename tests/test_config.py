from __future__ import annotations

import os
from pathlib import Path

import pytest

from johnstudio import config
from johnstudio.models import ProjectConfig


def test_home_dir_respects_env(monkeypatch, tmp_path):
    monkeypatch.setenv("JOHNSTUDIO_HOME", str(tmp_path / "home"))
    assert config.home_dir() == (tmp_path / "home").resolve()


def test_home_dir_default(monkeypatch):
    monkeypatch.delenv("JOHNSTUDIO_HOME", raising=False)
    assert config.home_dir() == Path.home() / ".johnstudio"


def test_load_default_config_yaml_has_required_sections():
    data = config.load_default_config_yaml()
    for key in ("user", "runtime", "tools", "workers", "safety", "skill_registry", "memory"):
        assert key in data
    assert "terminal_stub" in data["workers"]
    assert data["workers"]["terminal_stub"]["always_available"] is True


def test_write_default_config_then_load(monkeypatch, tmp_path):
    monkeypatch.setenv("JOHNSTUDIO_HOME", str(tmp_path))
    p = config.write_default_config()
    assert p.exists()
    cfg = config.load_global_config()
    assert cfg.runtime.max_active_agents == 6
    assert cfg.workers["terminal_stub"].always_available is True
    assert ".env" in cfg.safety.blocked_paths
    assert cfg.skill_registry.imported_skills_default_enabled is False


def test_write_default_config_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("JOHNSTUDIO_HOME", str(tmp_path))
    p1 = config.write_default_config()
    mtime1 = p1.stat().st_mtime_ns
    p2 = config.write_default_config()
    assert p1 == p2
    assert p2.stat().st_mtime_ns == mtime1


def test_project_config_roundtrip(tmp_path):
    repo = tmp_path / "demo"
    repo.mkdir()
    cfg = ProjectConfig(
        name="demo",
        repo_path=str(repo),
        base_branch="main",
        test_commands=["pytest -q"],
    )
    written = config.write_project_config(cfg)
    assert written.exists()
    loaded = config.load_project_config(repo)
    assert loaded.name == "demo"
    assert loaded.repo_path == str(repo.resolve())
    assert loaded.test_commands == ["pytest -q"]


def test_load_project_config_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        config.load_project_config(tmp_path)
