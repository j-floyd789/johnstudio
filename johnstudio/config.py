"""Global and project config loading.

`JOHNSTUDIO_HOME` env var overrides the default `~/.johnstudio` location.
This is required for tests to run hermetically.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

from .models import GlobalConfig, ProjectConfig

DEFAULT_HOME_DIRNAME = ".johnstudio"
SEEDS_DIR = Path(__file__).resolve().parent.parent / "seeds"
DEFAULT_CONFIG_PATH = SEEDS_DIR / "default_config.yaml"


def home_dir() -> Path:
    """Return the active JohnStudio home directory.

    Honors the `JOHNSTUDIO_HOME` env var so tests can point at a tmp dir.
    """
    override = os.environ.get("JOHNSTUDIO_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / DEFAULT_HOME_DIRNAME


def global_config_path() -> Path:
    return home_dir() / "config.yaml"


def load_default_config_yaml() -> dict:
    """Read the bundled default config as a dict."""
    with DEFAULT_CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def write_default_config(target: Path | None = None) -> Path:
    """Write the bundled default config to the global config path (or `target`).

    Returns the path written. Does not overwrite an existing file.
    """
    dst = target or global_config_path()
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        return dst
    data = load_default_config_yaml()
    with dst.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)
    return dst


def load_global_config() -> GlobalConfig:
    """Load the global config from disk. If absent, return the default."""
    p = global_config_path()
    if not p.exists():
        return GlobalConfig.model_validate(load_default_config_yaml())
    with p.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return GlobalConfig.model_validate(data)


def project_config_path(repo_path: str | Path) -> Path:
    return Path(repo_path).expanduser().resolve() / ".johnstudio" / "project.yaml"


def load_project_config(repo_path: str | Path) -> ProjectConfig:
    p = project_config_path(repo_path)
    if not p.exists():
        raise FileNotFoundError(f"No JohnStudio project config at {p}")
    with p.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return ProjectConfig.model_validate(data)


def write_project_config(cfg: ProjectConfig) -> Path:
    p = project_config_path(cfg.repo_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        yaml.safe_dump(cfg.model_dump(mode="json"), f, sort_keys=False)
    return p
