"""Per-project Obsidian-compatible Markdown memory vault.

Vault layout under `<repo>/.johnstudio/memory/`:
    00_index.md
    project_brief.md
    architecture.md
    current_state.md
    coding_standards.md
    commands.md
    environment.md
    database_schema.md
    api_contracts.md
    decisions/
    bugs/
    runs/
    summaries/
    handoffs/
    agent_lessons/
    graph/
        people/
        projects/
        tasks/
        agents/
        systems/
        concepts/
        decisions/
        bugs/
        files/
        features/
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from . import utils

ROOT_FILES: dict[str, str] = {
    "00_index.md": (
        "# Memory Index\n\n"
        "- [[project_brief]]\n- [[architecture]]\n- [[current_state]]\n"
        "- [[coding_standards]]\n- [[commands]]\n- [[environment]]\n"
        "- [[database_schema]]\n- [[api_contracts]]\n\n"
        "## Folders\n- decisions/ — architectural and product decisions\n"
        "- bugs/ — open & resolved bugs\n- runs/ — per-task run summaries\n"
        "- summaries/ — periodic distillations\n- handoffs/ — session handoff capsules\n"
        "- agent_lessons/ — per-agent learnings\n- graph/ — knowledge-graph entity pages\n"
    ),
    "project_brief.md": "# Project Brief\n\n_Describe what this project is and who it serves._\n",
    "architecture.md": "# Architecture\n\n_Describe major components, data flow, and external services._\n",
    "current_state.md": "# Current State\n\n_What's working now. What's in-flight._\n",
    "coding_standards.md": "# Coding Standards\n\n_Conventions specific to this project._\n",
    "commands.md": "# Commands\n\n## Run\n## Test\n## Lint\n## Build\n",
    "environment.md": "# Environment\n\n_Required env vars (names only, no secrets)._\n",
    "database_schema.md": "# Database Schema\n\n_Tables, columns, relationships._\n",
    "api_contracts.md": "# API Contracts\n\n_Endpoints and payload shapes._\n",
}

VAULT_DIRS: list[str] = [
    "decisions", "bugs", "runs", "summaries", "handoffs", "agent_lessons",
]

GRAPH_DIRS: list[str] = [
    "people", "projects", "tasks", "agents", "systems",
    "concepts", "decisions", "bugs", "files", "features",
]


def memory_root(repo_path: str | Path) -> Path:
    return Path(repo_path).expanduser().resolve() / ".johnstudio" / "memory"


def graph_root(repo_path: str | Path) -> Path:
    return memory_root(repo_path) / "graph"


def init_vault(repo_path: str | Path) -> Path:
    root = memory_root(repo_path)
    root.mkdir(parents=True, exist_ok=True)
    for fname, content in ROOT_FILES.items():
        utils.write_text(root / fname, content)
    for d in VAULT_DIRS:
        (root / d).mkdir(parents=True, exist_ok=True)
    gr = graph_root(repo_path)
    for d in GRAPH_DIRS:
        (gr / d).mkdir(parents=True, exist_ok=True)
    return root


def seed_from_codebase(repo_path: str | Path, *, name: str = "", stack: dict | None = None) -> None:
    """Populate project_brief / architecture / current_state from the repo itself
    (README + git + file tree) — deterministic, no LLM, so it's free. Only seeds
    files still holding the empty placeholder, so it won't clobber edited memory.
    """
    import subprocess
    repo = Path(repo_path).expanduser().resolve()
    root = memory_root(repo)
    stack = stack or {}

    def _is_placeholder(fname: str) -> bool:
        try:
            cur = (root / fname).read_text(encoding="utf-8")
        except OSError:
            return False
        return "_Describe" in cur or "_What's working" in cur

    def _git(args: list[str], limit: int = 4000) -> str:
        try:
            return subprocess.run(
                ["git", "-C", str(repo)] + args,
                capture_output=True, text=True, timeout=5,
            ).stdout[:limit]
        except Exception:
            return ""

    langs = ", ".join(stack.get("languages") or []) or "—"
    fws = ", ".join(stack.get("frameworks") or []) or "—"
    pkgs = ", ".join(stack.get("package_managers") or []) or "—"

    readme = ""
    for cand in ("README.md", "README.rst", "README.txt", "readme.md"):
        rp = repo / cand
        if rp.exists():
            try:
                readme = rp.read_text(encoding="utf-8", errors="replace")[:1500]
            except OSError:
                pass
            break

    if _is_placeholder("project_brief.md"):
        brief = f"# Project Brief\n\n**{name or repo.name}** — {langs} project.\n\n"
        if readme.strip():
            brief += "## From README\n\n" + readme.strip() + "\n"
        utils.write_text(root / "project_brief.md", brief, overwrite=True)

    if _is_placeholder("architecture.md"):
        tree = _git(["ls-files"], limit=20000)
        tops = sorted({ln.split("/")[0] for ln in tree.splitlines() if ln.strip()})[:40]
        arch = (
            f"# Architecture\n\n## Stack\n- Languages: {langs}\n- Frameworks: {fws}\n"
            f"- Package managers: {pkgs}\n\n## Top-level layout\n"
            + "\n".join(f"- `{t}`" for t in tops) + "\n"
        )
        utils.write_text(root / "architecture.md", arch, overwrite=True)

    if _is_placeholder("current_state.md"):
        branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], limit=80).strip()
        log = _git(["log", "--oneline", "-15"], limit=2000)
        cs = (
            f"# Current State\n\nOn branch `{branch}`.\n\n## Recent commits\n```\n"
            + log.strip() + "\n```\n"
        )
        utils.write_text(root / "current_state.md", cs, overwrite=True)


def update_current_state(repo_path: str | Path, content: str) -> Path:
    p = memory_root(repo_path) / "current_state.md"
    p.write_text(content, encoding="utf-8")
    return p


def write_run_summary(repo_path: str | Path, task_id: int, content: str) -> Path:
    p = memory_root(repo_path) / "runs" / f"task-{task_id:04d}.md"
    utils.write_text(p, content, overwrite=True)
    return p


def write_decision(repo_path: str | Path, slug: str, content: str) -> Path:
    p = memory_root(repo_path) / "decisions" / f"{datetime.utcnow().date()}-{slug}.md"
    utils.write_text(p, content, overwrite=True)
    return p


def write_handoff(repo_path: str | Path, task_id: int, content: str) -> Path:
    p = memory_root(repo_path) / "handoffs" / f"task-{task_id:04d}-handoff.md"
    utils.write_text(p, content, overwrite=True)
    return p


def append_lesson(repo_path: str | Path, agent_name: str, lesson: str) -> Path:
    p = memory_root(repo_path) / "agent_lessons" / f"{agent_name}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text(f"# Lessons — {agent_name}\n\n", encoding="utf-8")
    with p.open("a", encoding="utf-8") as f:
        f.write(f"- {datetime.utcnow().date()}: {lesson}\n")
    return p
