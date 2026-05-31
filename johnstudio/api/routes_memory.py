"""Memory vault and knowledge graph endpoints (project-scoped)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from . import _helpers
from .. import knowledge_graph as kg, memory
from .schemas import RelateNotesRequest

router = APIRouter(prefix="/api/projects/{project_id}/memory", tags=["memory"])


def _project_or_404(project_id: int) -> dict:
    p = _helpers.get_project_by_id(project_id)
    if not p:
        raise HTTPException(status_code=404, detail="project not found")
    return p


@router.get("/files")
def list_files(project_id: int) -> list[dict]:
    p = _project_or_404(project_id)
    return _helpers.list_memory_files(p["repo_path"])


@router.get("/file")
def get_file(project_id: int, path: str = Query(..., description="relative to memory vault root")) -> dict:
    p = _project_or_404(project_id)
    root = memory.memory_root(p["repo_path"])
    resolved = _helpers.safe_under(root, path)
    if resolved is None or not resolved.exists():
        raise HTTPException(status_code=404, detail="file not found or outside vault")
    return {"path": path, "content": _helpers.read_text_safely(resolved)}


@router.get("/entities")
def list_entities(project_id: int) -> list[dict]:
    _project_or_404(project_id)
    return kg.list_entities(project_id)


@router.get("/relationships")
def list_relationships(project_id: int) -> list[dict]:
    _project_or_404(project_id)
    return kg.list_relationships(project_id)


@router.get("/backlinks")
def backlinks(project_id: int, note: str = Query(...)) -> dict:
    p = _project_or_404(project_id)
    idx = kg.build_backlink_index(p["repo_path"])
    return {"note": note, "sources": idx.get(note, [])}


@router.post("/validate")
def validate_vault(project_id: int) -> dict:
    p = _project_or_404(project_id)
    from ..memory import ROOT_FILES, VAULT_DIRS, GRAPH_DIRS, memory_root, graph_root
    root = memory_root(p["repo_path"])
    gr = graph_root(p["repo_path"])
    missing_files = [f for f in ROOT_FILES if not (root / f).exists()]
    missing_dirs = [d for d in VAULT_DIRS if not (root / d).exists()]
    missing_graph = [d for d in GRAPH_DIRS if not (gr / d).exists()]
    ok = not (missing_files or missing_dirs or missing_graph)
    return {
        "ok": ok,
        "missing_files": missing_files,
        "missing_dirs": missing_dirs,
        "missing_graph_dirs": missing_graph,
    }


@router.post("/repair")
def repair_vault(project_id: int) -> dict:
    p = _project_or_404(project_id)
    memory.init_vault(p["repo_path"])
    return {"repaired": True}


@router.post("/relate", status_code=201)
def relate_notes(project_id: int, payload: RelateNotesRequest) -> dict:
    p = _project_or_404(project_id)
    rows = kg.list_entities(project_id)
    types = {r["name"]: r["entity_type"] for r in rows}
    if payload.note_a not in types or payload.note_b not in types:
        raise HTTPException(status_code=404, detail="entities must exist before linking")
    kg.link_entities(
        project_id,
        (types[payload.note_a], payload.note_a),
        (types[payload.note_b], payload.note_b),
        payload.relation,
    )
    return {"ok": True}
