"""`johnstudio init` and `johnstudio research` implementations."""
from __future__ import annotations

import shutil
from pathlib import Path

from . import config, db, utils

GLOBAL_MEMORY_FILES: dict[str, str] = {
    "john_preferences.md": (
        "# John's Preferences\n\n"
        "_This file is yours — record preferences that should apply across all projects._\n\n"
        "## Tone\n- Concise; no filler.\n\n"
        "## Engineering defaults\n- Tests required before merge.\n- No giant CLAUDE.md files.\n"
    ),
    "coding_defaults.md": (
        "# Coding Defaults\n\n"
        "- Prefer simple, transparent solutions over clever ones.\n"
        "- Avoid premature abstraction.\n"
        "- Don't add fallbacks for cases that can't happen.\n"
        "- Strict typing where the language supports it.\n"
    ),
    "safety_rules.md": (
        "# Safety Rules (global)\n\n"
        "- Never commit secrets (.env, *.pem, *.key, ~/.ssh/**, ~/.aws/**).\n"
        "- Never `git push --force` to shared branches.\n"
        "- Require explicit human confirmation before merge.\n"
        "- Dependency installs require approval.\n"
    ),
    "successful_patterns.md": (
        "# Successful Patterns\n\n"
        "_JohnStudio appends here when a pattern leads to a successful merge._\n"
    ),
    "failed_patterns.md": (
        "# Failed Patterns\n\n"
        "_JohnStudio appends here when an approach fails or is reverted._\n"
    ),
}

GLOBAL_GRAPH_DIRS: list[str] = [
    "people",
    "concepts",
    "agents",
]


def run_init() -> dict:
    """Idempotently set up the global JohnStudio home, DB, memory, and graph folders.

    Returns a status dict describing what exists.
    """
    home = config.home_dir()
    home.mkdir(parents=True, exist_ok=True)

    config.write_default_config()

    conn = db.connect()
    db_status = db.init_schema(conn)
    conn.close()

    (home / "logs").mkdir(exist_ok=True)
    (home / "sources").mkdir(exist_ok=True)

    sr = home / "skill-registry"
    for sub in ("skills", "agents", "hooks", "commands", "indexes"):
        (sr / sub).mkdir(parents=True, exist_ok=True)

    gm = home / "global-memory"
    gm.mkdir(parents=True, exist_ok=True)
    for fname, content in GLOBAL_MEMORY_FILES.items():
        utils.write_text(gm / fname, content)
    for sub in GLOBAL_GRAPH_DIRS:
        (gm / sub).mkdir(parents=True, exist_ok=True)
    # Seed Person entity for John.
    utils.write_text(
        gm / "people" / "Person - John.md",
        "---\n"
        "id: person-john\n"
        "type: person\n"
        "name: John\n"
        "tags: [person, owner]\n"
        "---\n\n"
        "# Person - John\n\n"
        "Owner of JohnStudio.\n",
    )

    tools = {
        "tmux": bool(shutil.which("tmux")),
        "git": bool(shutil.which("git")),
        "claude": bool(shutil.which("claude")),
        "codex": bool(shutil.which("codex")),
        "gemini": bool(shutil.which("gemini")),
        "terminal_stub": True,  # always available
    }

    # Import the 10 bundled seed skills as local-curated / enabled. Idempotent.
    seeds_imported = 0
    try:
        from . import skill_importer
        seeds_imported = len(skill_importer.import_seeds())
    except Exception:
        # Don't let a seed-import failure break init.
        pass

    return {
        "seeds_imported": seeds_imported,
        "home": str(home),
        "config_path": str(config.global_config_path()),
        "db_path": str(db.db_path()),
        "fts5": db_status["fts5"],
        "tools_detected": tools,
        "skill_registry_path": str(sr),
        "global_memory_path": str(gm),
    }


# ---------------------------------------------------------------------------
# research
# ---------------------------------------------------------------------------

def run_research(target: Path | None = None) -> Path:
    """Copy the baked-in research report to `docs/research/repo_research_report.md`.

    With no target, writes to the current working directory's `docs/research/`.
    Works fully offline — the seed file ships in the package distribution.
    """
    seed = utils.package_root() / "seeds" / "research_report.md"
    if not seed.exists():
        raise FileNotFoundError(f"Baked research report missing at {seed}")
    dst = target or (Path.cwd() / "docs" / "research" / "repo_research_report.md")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(seed.read_text(encoding="utf-8"), encoding="utf-8")
    return dst
