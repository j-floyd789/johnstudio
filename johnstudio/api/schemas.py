"""Pydantic request/response schemas for the API layer."""
from __future__ import annotations

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

class CreateProjectRequest(BaseModel):
    name: str
    repo_path: str


class ProjectResponse(BaseModel):
    id: int
    name: str
    repo_path: str
    base_branch: str


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

class RunTaskRequest(BaseModel):
    task: str
    stub_only: bool = False
    dry_run: bool = False
    workers: list[str] | None = None
    max_agents: int | None = None
    relevant_files: list[str] = Field(default_factory=list)


class MergeRequest(BaseModel):
    worker_name: str
    confirm: bool = False
    dry_run: bool = False
    reason: str | None = None  # optional human note recorded with decision


class ResumeRequest(BaseModel):
    worker_name: str


class CleanupRequest(BaseModel):
    prune_worktrees: bool = False


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

class AddSkillSourceRequest(BaseModel):
    uri: str


class DiscoverSkillsRequest(BaseModel):
    task: str
    agent_role: str = "backend_implementer"
    relevant_files: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

class RelateNotesRequest(BaseModel):
    note_a: str
    note_b: str
    relation: str = "related"
