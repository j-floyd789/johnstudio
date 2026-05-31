"""Project registration: validate repo, detect stack, write project.yaml,
initialize memory vault, create the Project entity in the knowledge graph.
"""
from __future__ import annotations

from pathlib import Path

from . import config, db, knowledge_graph, memory, utils
from .models import ProjectConfig, ProjectStack

# ---------------------------------------------------------------------------
# Stack detection
# ---------------------------------------------------------------------------

# Pure-deterministic mapping: marker file -> ([languages], [frameworks], [pkg mgrs])
STACK_MARKERS: list[tuple[str, tuple[list[str], list[str], list[str]]]] = [
    ("package.json", (["javascript", "typescript"], [], ["npm"])),
    ("pnpm-lock.yaml", ([], [], ["pnpm"])),
    ("yarn.lock", ([], [], ["yarn"])),
    ("bun.lockb", ([], [], ["bun"])),
    ("pyproject.toml", (["python"], [], ["pip"])),
    ("requirements.txt", (["python"], [], ["pip"])),
    ("Pipfile", (["python"], [], ["pipenv"])),
    ("poetry.lock", (["python"], [], ["poetry"])),
    ("Cargo.toml", (["rust"], [], ["cargo"])),
    ("go.mod", (["go"], [], ["go"])),
    ("pom.xml", (["java"], [], ["maven"])),
    ("build.gradle", (["java", "kotlin"], [], ["gradle"])),
    ("build.gradle.kts", (["kotlin"], [], ["gradle"])),
    ("Gemfile", (["ruby"], ["rails"], ["bundler"])),
    ("composer.json", (["php"], [], ["composer"])),
    ("next.config.js", ([], ["nextjs"], [])),
    ("next.config.mjs", ([], ["nextjs"], [])),
    ("next.config.ts", ([], ["nextjs"], [])),
    ("vite.config.js", ([], ["vite"], [])),
    ("vite.config.ts", ([], ["vite"], [])),
    ("tailwind.config.js", ([], ["tailwind"], [])),
    ("tailwind.config.ts", ([], ["tailwind"], [])),
    ("tsconfig.json", (["typescript"], [], [])),
    ("prisma/schema.prisma", ([], ["prisma"], [])),
    ("svelte.config.js", ([], ["svelte"], [])),
    ("nuxt.config.ts", ([], ["nuxt"], [])),
    ("Dockerfile", ([], ["docker"], [])),
    ("docker-compose.yml", ([], ["docker"], [])),
    ("docker-compose.yaml", ([], ["docker"], [])),
]


def detect_stack(repo_path: str | Path) -> ProjectStack:
    repo = Path(repo_path).expanduser().resolve()
    langs: set[str] = set()
    fws: set[str] = set()
    pms: set[str] = set()
    detected: list[str] = []
    for marker, (l, f, p) in STACK_MARKERS:
        if (repo / marker).exists():
            detected.append(marker)
            langs.update(l)
            fws.update(f)
            pms.update(p)
    return ProjectStack(
        languages=sorted(langs),
        frameworks=sorted(fws),
        package_managers=sorted(pms),
        detected_files=detected,
    )


# ---------------------------------------------------------------------------
# add-project
# ---------------------------------------------------------------------------

class NotAGitRepoError(RuntimeError):
    pass


def _is_git_repo(repo: Path) -> bool:
    return (repo / ".git").exists()


def _detect_base_branch(repo: Path) -> str:
    """Read HEAD via `cat .git/HEAD` to avoid invoking git CLI in tests.

    Returns 'main' if undetectable.
    """
    head = repo / ".git" / "HEAD"
    if not head.exists():
        return "main"
    try:
        text = head.read_text().strip()
    except OSError:
        return "main"
    if text.startswith("ref: refs/heads/"):
        return text.split("ref: refs/heads/", 1)[1].strip() or "main"
    return "main"


def _default_test_commands(stack: ProjectStack) -> list[str]:
    cmds: list[str] = []
    if "npm" in stack.package_managers or "pnpm" in stack.package_managers or "yarn" in stack.package_managers:
        cmds.append("npm test")
    if "python" in stack.languages:
        cmds.append("pytest -q")
    if "cargo" in stack.package_managers:
        cmds.append("cargo test")
    if "go" in stack.languages:
        cmds.append("go test ./...")
    return cmds


def add_project(name: str, repo_path: str | Path) -> dict:
    """Register a project. Returns a status dict with config path and stack."""
    # Trim whitespace defensively — copy/paste from a browser or terminal
    # often picks up a stray leading/trailing space, and Path treats those
    # as part of the path component (so " /Users/x" is no longer
    # absolute and resolve() prepends the cwd).
    if isinstance(repo_path, str):
        repo_path = repo_path.strip()
    repo = Path(repo_path).expanduser().resolve()
    if not repo.exists():
        raise FileNotFoundError(f"Repo path does not exist: {repo}")
    if not _is_git_repo(repo):
        raise NotAGitRepoError(f"Not a git repository: {repo}")

    stack = detect_stack(repo)
    base = _detect_base_branch(repo)

    cfg = ProjectConfig(
        name=name,
        repo_path=str(repo),
        base_branch=base,
        test_commands=_default_test_commands(stack),
        stack=stack,
    )
    project_yaml = config.write_project_config(cfg)

    # Memory vault + graph folders
    memory.init_vault(repo)
    # Seed the vault from the codebase (deterministic, no LLM) so the first
    # planner/specialists have real project context instead of empty templates.
    try:
        memory.seed_from_codebase(repo, name=name, stack=stack)
    except Exception:
        pass  # best-effort; empty templates remain if seeding fails
    (repo / ".johnstudio" / "tasks").mkdir(parents=True, exist_ok=True)
    (repo / ".johnstudio" / "worktrees").mkdir(parents=True, exist_ok=True)
    (repo / ".johnstudio" / "logs").mkdir(parents=True, exist_ok=True)

    # DB row
    conn = db.connect()
    db.init_schema(conn)
    cur = conn.execute(
        """INSERT INTO projects (name, repo_path, base_branch)
           VALUES (?, ?, ?)
           ON CONFLICT(name) DO UPDATE SET
               repo_path = excluded.repo_path,
               base_branch = excluded.base_branch,
               updated_at = CURRENT_TIMESTAMP
           RETURNING id""",
        (name, str(repo), base),
    )
    project_id = cur.fetchone()["id"]
    conn.commit()
    conn.close()

    # Project entity in knowledge graph
    knowledge_graph.create_entity(
        project_id=project_id,
        repo_path=repo,
        entity_type="project",
        name=name,
        tags=sorted(set(stack.languages + stack.frameworks)),
        metadata={
            "repo_path": str(repo),
            "base_branch": base,
            "languages": stack.languages,
            "frameworks": stack.frameworks,
        },
        body=(
            f"# Project - {name}\n\n"
            f"- Repo: `{repo}`\n- Base branch: `{base}`\n"
            f"- Languages: {', '.join(stack.languages) or 'unknown'}\n"
            f"- Frameworks: {', '.join(stack.frameworks) or 'none detected'}\n"
            f"- Detected markers: {', '.join(stack.detected_files) or 'none'}\n"
        ),
    )

    return {
        "project_id": project_id,
        "project_yaml": str(project_yaml),
        "stack": stack.model_dump(),
        "base_branch": base,
    }


def list_projects() -> list[dict]:
    conn = db.connect()
    db.init_schema(conn)
    cur = conn.execute("SELECT id, name, repo_path, base_branch FROM projects ORDER BY name")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_project(name: str) -> dict | None:
    conn = db.connect()
    db.init_schema(conn)
    cur = conn.execute(
        "SELECT id, name, repo_path, base_branch FROM projects WHERE name = ?",
        (name,),
    )
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None
