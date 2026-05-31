"""Thin adapters between API routes and existing modules.

These are deliberately small wrappers — no business logic. The goal is to keep
route files declarative and to centralize anything API-shaped (DB lookups by id,
path-safety guards, file reads with size caps) in one place.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

from .. import (
    config,
    db,
    init as init_mod,
    knowledge_graph as kg,
    memory,
    workers,
)
from ..models import GlobalConfig

MAX_TEXT_BYTES = 200_000  # cap for file reads served over the API


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

def get_project_by_id(project_id: int) -> dict | None:
    conn = db.connect()
    db.init_schema(conn)
    row = conn.execute(
        "SELECT id, name, repo_path, base_branch FROM projects WHERE id = ?",
        (project_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def list_tasks_for_project(project_id: int) -> list[dict]:
    conn = db.connect()
    db.init_schema(conn)
    rows = conn.execute(
        """SELECT id, task_number, title, description, status,
                  base_branch, created_at, updated_at
           FROM tasks WHERE project_id = ?
           ORDER BY task_number DESC""",
        (project_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def task_folder(repo_path: str | Path, task_number: int) -> Path:
    return Path(repo_path) / ".johnstudio" / "tasks" / f"task-{task_number:04d}"


# ---------------------------------------------------------------------------
# Safe file I/O
# ---------------------------------------------------------------------------

def read_text_safely(path: Path, *, max_bytes: int = MAX_TEXT_BYTES) -> str:
    if not path.exists() or not path.is_file():
        return ""
    data = path.read_bytes()
    if len(data) > max_bytes:
        return data[:max_bytes].decode("utf-8", errors="replace") + "\n…(truncated)\n"
    return data.decode("utf-8", errors="replace")


def safe_under(root: Path, rel_path: str) -> Path | None:
    """Resolve `rel_path` against `root` and ensure the result stays inside.

    Returns the resolved path or None if traversal would escape `root`.
    """
    root = root.resolve()
    candidate = (root / rel_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

def read_skill_files(skill_id: str) -> dict:
    base = config.home_dir() / "skill-registry" / "skills" / skill_id
    return {
        "metadata_yaml": read_text_safely(base / "metadata.yaml"),
        "distilled_md": read_text_safely(base / "distilled.md"),
        "summary_md": read_text_safely(base / "summary.md"),
        "original_md": read_text_safely(base / "original.md"),
        "source_json": read_text_safely(base / "source.json"),
        "score_json": read_text_safely(base / "score.json"),
    }


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------

def list_workers(global_cfg: GlobalConfig) -> list[dict]:
    out: list[dict] = []
    for name, cfg in global_cfg.workers.items():
        w = workers.make_worker(name, cfg)
        out.append({
            "name": name,
            "provider": cfg.provider,
            "role": cfg.role,
            "command": cfg.command,
            "can_edit": cfg.can_edit,
            "worktree": cfg.worktree,
            "max_runtime_minutes": cfg.max_runtime_minutes,
            "always_available": cfg.always_available,
            "is_available": w.is_available(),
        })
    return out


# ---------------------------------------------------------------------------
# Memory vault listing
# ---------------------------------------------------------------------------

def list_memory_files(repo_path: str | Path) -> list[dict]:
    root = memory.memory_root(repo_path)
    if not root.exists():
        return []
    out: list[dict] = []
    for p in sorted(root.rglob("*.md")):
        if not p.is_file():
            continue
        out.append({
            "path": str(p.relative_to(root)),
            "bytes": p.stat().st_size,
        })
    return out


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------

def run_doctor() -> dict:
    """Aggregated availability + DB + FTS status. Cheap; safe to call often."""
    global_cfg = config.load_global_config()
    home = config.home_dir()
    conn = db.connect()
    fts = db.has_fts5(conn)
    conn.close()
    tools = {
        "tmux": bool(shutil.which("tmux")),
        "git": bool(shutil.which("git")),
        "claude": bool(shutil.which("claude")),
        "codex": bool(shutil.which("codex")),
        "gemini": bool(shutil.which("gemini")),
    }
    return {
        "home": str(home),
        "config_path": str(config.global_config_path()),
        "db_path": str(db.db_path()),
        "fts5": fts,
        "tools": tools,
        "workers": list_workers(global_cfg),
        "role_model_probes": _role_model_probes(),
    }


def _role_model_probes() -> list[dict]:
    """Smoke-test each role's CLI + declared model with a trivial prompt.

    The classic failure mode: a role's frontmatter declares
    `model: gemini-3-pro` (typo) or `model: gpt-5` (unsupported on a
    ChatGPT account), the team task launches fine, but the specialist
    400/404s immediately when it tries to talk to its model. The user
    sees "task hung" and only discovers the cause by digging through
    logs. This probe surfaces it in `/doctor` instead so the UI can
    show a clear "role X uses model Y which is unreachable" warning
    before any user task runs.

    Probes are cached for 5 minutes — they cost a real API call each.
    """
    import subprocess
    from .. import team
    now = time.time()
    if _ROLE_PROBE_CACHE.get("ts", 0) + 300 > now:
        return _ROLE_PROBE_CACHE["data"]
    try:
        catalog = team.load_role_catalog()
    except Exception as e:
        out = [{"error": f"role catalog failed to load: {e}"}]
        _ROLE_PROBE_CACHE.update(ts=now, data=out)
        return out
    out: list[dict] = []
    # Group by (provider, model) so we probe each unique combo once.
    seen: dict[tuple[str, str], dict] = {}
    for r in catalog.values():
        key = (r.provider, r.model or "<default>")
        if key in seen:
            seen[key]["roles"].append(r.name)
            continue
        result = {"provider": r.provider, "model": r.model or "<default>", "roles": [r.name]}
        cmd = _probe_command(r.provider, r.model)
        if cmd is None:
            result["ok"] = None
            result["note"] = f"no probe defined for provider {r.provider!r}"
        else:
            try:
                cp = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
                stderr = (cp.stderr or "")[-300:]
                stdout = (cp.stdout or "")[-300:]
                result["ok"] = cp.returncode == 0 and "ok" in (stdout.lower() + stderr.lower())
                if not result["ok"]:
                    # Surface common failure signatures.
                    bad = stderr + stdout
                    if "not supported" in bad or "ModelNotFoundError" in bad or "404" in bad or "Requested entity was not found" in bad:
                        result["error"] = "model not available for this account/CLI"
                    elif "unauthorized" in bad.lower() or "401" in bad:
                        result["error"] = "auth missing / expired"
                    elif "not found" in bad.lower() and "command" in bad.lower():
                        result["error"] = "CLI binary missing on PATH"
                    else:
                        result["error"] = "probe failed (see stderr)"
                    result["stderr"] = stderr
            except subprocess.TimeoutExpired:
                result["ok"] = False
                result["error"] = "probe timed out (network or auth hang)"
            except FileNotFoundError:
                result["ok"] = False
                result["error"] = "CLI binary missing on PATH"
        seen[key] = result
        out.append(result)
    _ROLE_PROBE_CACHE.update(ts=now, data=out)
    return out


_ROLE_PROBE_CACHE: dict = {"ts": 0, "data": []}


def _probe_command(provider: str, model: str | None) -> list[str] | None:
    """One-shot CLI invocation that should print "OK" on success.

    Kept minimal — we just want to verify that the model name is accepted.
    """
    if provider == "claude":
        # claude --print is non-interactive; --model may be omitted.
        cmd = ["claude", "--print"]
        if model:
            cmd += ["--model", model]
        cmd += ["Reply with exactly: OK"]
        return cmd
    if provider == "codex":
        # codex exec --model X "<prompt>"
        cmd = ["codex", "exec"]
        if model:
            cmd += ["--model", model]
        cmd += ["Reply with exactly: OK"]
        return cmd
    if provider == "gemini":
        # gemini -p "<prompt>" -m model
        cmd = ["gemini", "-p", "Reply with exactly: OK"]
        if model:
            cmd += ["-m", model]
        return cmd
    return None


# ---------------------------------------------------------------------------
# Safety report wrapper
# ---------------------------------------------------------------------------

def safety_report_from_collect(summary: dict) -> dict:
    """Extract per-worker safety flags from a `collector.collect()` summary."""
    runs = []
    for r in summary.get("runs", []):
        runs.append({
            "worker": r["worker"],
            "protected_path_hits": r.get("protected_path_hits", []),
            "dangerous_command_hits": r.get("dangerous_command_hits", []),
            "approval_command_hits": r.get("approval_command_hits", []),
        })
    return {"task_number": summary.get("task_number"), "runs": runs}
