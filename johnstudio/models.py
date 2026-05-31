"""Pydantic models for JohnStudio's primary contracts."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Global config
# ---------------------------------------------------------------------------

class RuntimeConfig(BaseModel):
    max_active_agents: int = 6
    max_agent_depth: int = 1
    default_timeout_minutes: int = 45
    require_human_merge: bool = True
    allow_worker_spawn: bool = False
    default_stub_only: bool = False


class ToolBinary(BaseModel):
    command: str


class ToolsConfig(BaseModel):
    tmux: ToolBinary = Field(default_factory=lambda: ToolBinary(command="tmux"))
    git: ToolBinary = Field(default_factory=lambda: ToolBinary(command="git"))


class WorkerConfig(BaseModel):
    provider: Literal["terminal", "claude", "codex", "gemini"]
    command: str
    role: str
    can_edit: bool = False
    worktree: bool = False
    max_runtime_minutes: int = 30
    always_available: bool = False
    # Optional per-role model override (e.g. "claude-opus-4-7", "haiku-…")
    # surfaced from seeds/roles/<vp>/<role>.md frontmatter.
    model: str | None = None
    effort: str | None = None   # reasoning effort: high|medium|low (per-provider mapped)
    # Optional per-role tool allowlist. When set, the worker adapter plumbs
    # it into the CLI's --allowed-tools flag so a specialist can't reach
    # for Task / Bash / Edit when its role says read-only.
    allowed_tools: list[str] | None = None
    # If true, this role's Claude CLI can invoke the `Task` tool to spawn
    # built-in subagents (Explore, Plan, implementer, verifier). Default
    # False — see team.load_role_catalog for the catalog-level guard.
    can_spawn_subagents: bool = False


class SafetyConfig(BaseModel):
    blocked_paths: list[str] = Field(default_factory=list)
    dangerous_commands: list[str] = Field(default_factory=list)
    require_approval_commands: list[str] = Field(default_factory=list)


class SkillRegistryConfig(BaseModel):
    max_skills_per_agent: int = 6
    max_skill_tokens_per_agent: int = 8000
    max_single_skill_tokens: int = 2500
    use_distilled_skills: bool = True
    imported_skills_default_enabled: bool = False
    imported_skills_default_trust_level: str = "unreviewed"


class MemoryConfig(BaseModel):
    use_markdown_vault: bool = True
    use_knowledge_graph: bool = True
    use_wiki_links: bool = True
    use_yaml_frontmatter: bool = True
    use_tags: bool = True
    auto_tag_after_collect: bool = True
    auto_link_after_collect: bool = True


class UserConfig(BaseModel):
    name: str = "John"


class GlobalConfig(BaseModel):
    version: int = 1
    user: UserConfig = Field(default_factory=UserConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    workers: dict[str, WorkerConfig] = Field(default_factory=dict)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    skill_registry: SkillRegistryConfig = Field(default_factory=SkillRegistryConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)


# ---------------------------------------------------------------------------
# Project config
# ---------------------------------------------------------------------------

class ProjectStack(BaseModel):
    languages: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    package_managers: list[str] = Field(default_factory=list)
    detected_files: list[str] = Field(default_factory=list)


class ProjectRules(BaseModel):
    require_tests_before_merge: bool = True
    max_files_changed_per_worker: int = 40
    protected_paths: list[str] = Field(default_factory=list)


class ProjectMemoryConfig(BaseModel):
    graph_enabled: bool = True
    obsidian_compatible: bool = True


class ProjectConfig(BaseModel):
    version: int = 1
    name: str
    repo_path: str
    base_branch: str = "main"
    test_commands: list[str] = Field(default_factory=list)
    stack: ProjectStack = Field(default_factory=ProjectStack)
    pinned_skills: list[str] = Field(default_factory=list)
    rules: ProjectRules = Field(default_factory=ProjectRules)
    memory: ProjectMemoryConfig = Field(default_factory=ProjectMemoryConfig)

    @field_validator("repo_path")
    @classmethod
    def _absolute_repo_path(cls, v: str) -> str:
        return str(Path(v).expanduser().resolve())


# ---------------------------------------------------------------------------
# Operational models
# ---------------------------------------------------------------------------

class Task(BaseModel):
    id: int | None = None
    project_id: int
    task_number: int
    title: str
    description: str
    status: Literal["pending", "running", "collected", "reviewed", "merged", "stopped"] = "pending"
    base_branch: str = "main"
    created_at: datetime | None = None
    updated_at: datetime | None = None


class Run(BaseModel):
    id: int | None = None
    task_id: int
    worker_id: int
    status: Literal["pending", "launched", "running", "completed", "stopped", "unavailable"] = "pending"
    tmux_session: str | None = None
    tmux_pane: str | None = None
    worktree_path: str | None = None
    branch_name: str | None = None
    prompt_path: str | None = None
    result_path: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


# ---------------------------------------------------------------------------
# Skill models
# ---------------------------------------------------------------------------

class SkillMetadata(BaseModel):
    id: str
    name: str
    type: Literal["skill", "agent", "rule", "command", "hook", "mcp"] = "skill"
    source_repo: str | None = None
    source_path: str | None = None
    category: str = "general-guidance"
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    agent_roles: list[str] = Field(default_factory=list)
    file_patterns: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    priority: Literal["low", "medium", "high"] = "medium"
    max_context_tokens: int = 2500
    trust_level: Literal["local-curated", "unreviewed", "reviewed", "trusted"] = "unreviewed"
    enabled: bool = False
    created_at: str | None = None
    updated_at: str | None = None


class SkillRouteResult(BaseModel):
    skill_id: str
    score: float
    rationale: str
    tokens: int


class ContextPack(BaseModel):
    worker_name: str
    task_id: int
    role: str
    scope: str
    task_title: str
    task_description: str
    project_summary: str
    current_state: str
    relevant_files: list[str] = Field(default_factory=list)
    selected_skills: list[SkillRouteResult] = Field(default_factory=list)
    graph_links: list[str] = Field(default_factory=list)
    safety_notes: list[str] = Field(default_factory=list)
    retrieved_memory: str = ""


# ---------------------------------------------------------------------------
# Graph models
# ---------------------------------------------------------------------------

class GraphEntity(BaseModel):
    id: int | None = None
    project_id: int
    entity_id: str
    entity_type: str
    name: str
    path: str
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class GraphRelationship(BaseModel):
    id: int | None = None
    project_id: int
    from_entity_id: str
    to_entity_id: str
    relation_type: str
    source_note_path: str | None = None
    confidence: float = 1.0


# ---------------------------------------------------------------------------
# Review/merge models
# ---------------------------------------------------------------------------

class ReviewScore(BaseModel):
    worker_name: str
    score: int
    breakdown: dict[str, int] = Field(default_factory=dict)
    flags: list[str] = Field(default_factory=list)


class MergePlan(BaseModel):
    selected_worker: str
    branch: str
    files: list[str] = Field(default_factory=list)
    expected_conflicts: list[str] = Field(default_factory=list)
    tests_to_run: list[str] = Field(default_factory=list)
    rollback_plan: str = ""
