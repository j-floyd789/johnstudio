"""Project endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from . import _helpers
from .. import knowledge_graph as kg, project as project_mod
from .schemas import CreateProjectRequest

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.get("")
def list_projects() -> list[dict]:
    return project_mod.list_projects()


@router.post("", status_code=201)
def create_project(payload: CreateProjectRequest) -> dict:
    try:
        status = project_mod.add_project(payload.name, payload.repo_path)
    except project_mod.NotAGitRepoError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return status


@router.get("/{project_id}")
def get_project(project_id: int) -> dict:
    p = _helpers.get_project_by_id(project_id)
    if not p:
        raise HTTPException(status_code=404, detail=f"project id={project_id} not found")
    from .. import config
    cfg = config.load_project_config(p["repo_path"])
    return {**p, "config": cfg.model_dump(mode="json")}


@router.get("/{project_id}/memory")
def get_project_memory(project_id: int) -> dict:
    p = _helpers.get_project_by_id(project_id)
    if not p:
        raise HTTPException(status_code=404, detail="project not found")
    return {
        "files": _helpers.list_memory_files(p["repo_path"]),
        "vault_root": str(__import__("johnstudio.memory", fromlist=["memory_root"]).memory_root(p["repo_path"])),
    }


@router.get("/{project_id}/graph")
def get_project_graph(project_id: int) -> dict:
    p = _helpers.get_project_by_id(project_id)
    if not p:
        raise HTTPException(status_code=404, detail="project not found")
    return {
        "entities": kg.list_entities(project_id),
        "relationships": kg.list_relationships(project_id),
    }
