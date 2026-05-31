"""Skill registry, sources, and routing endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from . import _helpers
from .. import config, skill_registry, skill_router, skill_source
from .schemas import AddSkillSourceRequest, DiscoverSkillsRequest

router = APIRouter(prefix="/api/skills", tags=["skills"])


@router.get("")
def list_skills(
    enabled_only: bool = False,
    category: str | None = None,
) -> list[dict]:
    return skill_registry.list_skills(enabled_only=enabled_only, category=category)


@router.get("/sources")
def list_sources() -> list[dict]:
    return skill_source.list_sources()


@router.post("/sources/scan")
def scan_sources() -> list[dict]:
    return skill_source.scan_sources()


@router.post("/source", status_code=201)
def add_source(payload: AddSkillSourceRequest) -> dict:
    return skill_source.add_source(payload.uri)


@router.get("/{skill_id}")
def get_skill(skill_id: str) -> dict:
    row = skill_registry.show_skill(skill_id)
    if not row:
        raise HTTPException(status_code=404, detail="skill not found")
    files = _helpers.read_skill_files(skill_id)
    return {**row, "files": files}


@router.post("/{skill_id}/enable")
def enable_skill(skill_id: str) -> dict:
    try:
        skill_registry.set_enabled(skill_id, True)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"skill_id": skill_id, "enabled": True}


@router.post("/{skill_id}/disable")
def disable_skill(skill_id: str) -> dict:
    try:
        skill_registry.set_enabled(skill_id, False)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"skill_id": skill_id, "enabled": False}


# ---------------------------------------------------------------------------
# Project-scoped routes (pin/unpin/discover) — mounted under a separate router
# so they nest under /api/projects/{project_id}/skills/...
# ---------------------------------------------------------------------------

project_skills_router = APIRouter(
    prefix="/api/projects/{project_id}/skills", tags=["skills"]
)


@project_skills_router.post("/{skill_id}/pin")
def pin_skill(project_id: int, skill_id: str) -> dict:
    p = _helpers.get_project_by_id(project_id)
    if not p:
        raise HTTPException(status_code=404, detail="project not found")
    return {"pinned": skill_registry.pin_skill(p["repo_path"], skill_id)}


@project_skills_router.post("/{skill_id}/unpin")
def unpin_skill(project_id: int, skill_id: str) -> dict:
    p = _helpers.get_project_by_id(project_id)
    if not p:
        raise HTTPException(status_code=404, detail="project not found")
    return {"pinned": skill_registry.unpin_skill(p["repo_path"], skill_id)}


@project_skills_router.post("/discover")
def discover_skills(project_id: int, payload: DiscoverSkillsRequest) -> list[dict]:
    p = _helpers.get_project_by_id(project_id)
    if not p:
        raise HTTPException(status_code=404, detail="project not found")
    pcfg = config.load_project_config(p["repo_path"])
    req = skill_router.RouteRequest(
        project=pcfg,
        agent_role=payload.agent_role,
        task_text=payload.task,
        relevant_files=payload.relevant_files,
        feedback=skill_router.previous_feedback(),
    )
    return [r.model_dump() for r in skill_router.route(req)]
