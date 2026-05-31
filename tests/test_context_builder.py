from __future__ import annotations

from pathlib import Path

from johnstudio import config, context_builder, project as project_mod, skill_importer
from johnstudio.models import ProjectStack, WorkerConfig


def _setup(jh_home, git_repo):
    project_mod.add_project("demo", git_repo)
    skill_importer.import_seeds()
    pcfg = config.load_project_config(git_repo)
    pcfg.stack = ProjectStack(languages=["typescript"], frameworks=["react", "nextjs"])
    config.write_project_config(pcfg)
    return pcfg


REQUIRED_SECTIONS = [
    "## Role", "## Scope", "## Task", "## Project Summary", "## Current State",
    "## Relevant Files", "## Selected Skills", "## Knowledge Graph Links",
    "## Safety Rules", "## Rule Precedence", "## Output Contract", "## Completion Signal",
]


def test_context_pack_contains_all_required_sections(jh_home, git_repo):
    pcfg = _setup(jh_home, git_repo)
    worker = WorkerConfig(
        provider="claude", command="claude", role="frontend_implementer",
        can_edit=True, worktree=True, max_runtime_minutes=30,
    )
    pack, md = context_builder.build_context_pack(
        project_cfg=pcfg, project_name="demo",
        worker_name="claude_frontend", worker_cfg=worker,
        task_id=1, task_title="Login page",
        task_description="Build /login with email+password",
        worktree_path=Path("/tmp/wt"), relevant_files=["app/login/page.tsx"],
    )
    for s in REQUIRED_SECTIONS:
        assert s in md, f"Missing section: {s}"
    assert "Rule Precedence" in md
    assert "RESULT.md" in md
    assert "DONE.md" in md
    # Skill picks should be present
    assert any("frontend-react-specialist" in s.skill_id for s in pack.selected_skills)


def test_context_pack_review_only_worker_has_no_edit_scope(jh_home, git_repo):
    pcfg = _setup(jh_home, git_repo)
    worker = WorkerConfig(
        provider="gemini", command="gemini", role="architecture_reviewer",
        can_edit=False, worktree=False, max_runtime_minutes=30,
    )
    _, md = context_builder.build_context_pack(
        project_cfg=pcfg, project_name="demo",
        worker_name="gemini_review", worker_cfg=worker,
        task_id=1, task_title="Review",
        task_description="Review the work",
        worktree_path=None,
    )
    assert "READ-ONLY" in md
    assert "No worktree assigned" in md


def test_context_pack_includes_safety_paths(jh_home, git_repo):
    pcfg = _setup(jh_home, git_repo)
    worker = WorkerConfig(
        provider="terminal", command="x", role="test_worker",
        can_edit=True, worktree=True, max_runtime_minutes=5, always_available=True,
    )
    _, md = context_builder.build_context_pack(
        project_cfg=pcfg, project_name="demo",
        worker_name="terminal_stub", worker_cfg=worker,
        task_id=1, task_title="t", task_description="d",
        worktree_path=Path("/tmp/wt"),
    )
    assert ".env" in md
    assert "rm -rf" in md
    assert "git push --force" in md


def test_context_pack_respects_token_budget(jh_home, git_repo):
    pcfg = _setup(jh_home, git_repo)
    worker = WorkerConfig(
        provider="claude", command="claude", role="backend_implementer",
        can_edit=True, worktree=True, max_runtime_minutes=30,
    )
    pack, _ = context_builder.build_context_pack(
        project_cfg=pcfg, project_name="demo",
        worker_name="claude_backend", worker_cfg=worker,
        task_id=1, task_title="Hello endpoint",
        task_description="Add /api/hello returning 200 with tests and security review",
        worktree_path=Path("/tmp/wt"), relevant_files=["app/api/hello/route.ts"],
    )
    total = sum(s.tokens for s in pack.selected_skills)
    assert total <= 8000
    assert len(pack.selected_skills) <= 6
