"""Iteration-arc endpoints: launch, list, inspect, step.

# RECONSTRUCTED: the module header (imports + router) and the tail endpoints
# (get-one, step) were lost; restored from sibling route modules + the
# iteration_arc/arc_launcher public surface. The models, helpers, launch and
# list handlers in the middle are the original recovered source.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import _helpers
from .. import arc_launcher, iteration_arc

router = APIRouter(prefix="/api/projects/{project_id}/arcs", tags=["arcs"])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class LaunchArcRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=4000,
                        description="One-line English description of what the arc should achieve")
    arc_name: str | None = Field(None, max_length=80,
                                 description="Optional override; auto-derived from the prompt if omitted")
    max_iterations: int = Field(10, ge=1, le=50)
    webhook_url: str | None = Field(None, max_length=2000,
                                    description="Optional URL POSTed when the arc terminates")
    auto_advance: bool = Field(True,
                               description="If true, kick off iteration 1 immediately")


class StepArcRequest(BaseModel):
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _project_or_404(project_id: int) -> dict:
    p = _helpers.get_project_by_id(project_id)
    if not p:
        raise HTTPException(status_code=404, detail="project not found")
    return p


def _arcs_root(repo_path: str) -> Path:
    return Path(repo_path) / ".johnstudio" / "arcs"


def _read_arc_status(arc_folder: Path) -> dict:
    """Hydrate a compact status dict from on-disk ARC.yaml + STATE.json."""
    cfg_p = arc_folder / "ARC.yaml"
    state_p = arc_folder / "STATE.json"
    if not cfg_p.exists() or not state_p.exists():
        raise HTTPException(status_code=404, detail=f"arc {arc_folder.name!r} not found")
    cfg = iteration_arc.ArcConfig.from_yaml(cfg_p)
    state = iteration_arc.ArcState.from_json(state_p)
    return {
        "name": cfg.name,
        "project_name": cfg.project_name,
        "max_iterations": cfg.max_iterations,
        "seed_text": cfg.seed_text,
        "webhook_url": cfg.webhook_url,
        "status": state.status,
        "current_iter": state.current_iter,
        "iterations": state.iterations,
        "last_update": state.last_update,
        "webhook": {
            "fired_at": state.webhook_fired_at,
            "ok": state.webhook_ok,
            "detail": state.webhook_detail,
        },
        "arc_folder": str(arc_folder),
    }


# ---------------------------------------------------------------------------
# Launcher
# ---------------------------------------------------------------------------

@router.post("/launch", status_code=201)
def launch_arc(project_id: int, payload: LaunchArcRequest) -> dict:
    """Fire-and-forget: one prompt → fully-configured + auto-advanced arc."""
    proj = _project_or_404(project_id)
    try:
        return arc_launcher.launch_from_prompt(
            repo=Path(proj["repo_path"]),
            project_name=proj["name"],
            prompt=payload.prompt,
            arc_name=payload.arc_name,
            max_iterations=payload.max_iterations,
            webhook_url=payload.webhook_url,
            auto_advance=payload.auto_advance,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------

@router.get("")
def list_arcs(project_id: int) -> dict:
    """List every arc folder under the project. Cheap on-disk scan."""
    proj = _project_or_404(project_id)
    root = _arcs_root(proj["repo_path"])
    out: list[dict] = []
    if root.exists():
        for d in sorted(root.iterdir()):
            if not d.is_dir():
                continue
            cfg_p = d / "ARC.yaml"
            state_p = d / "STATE.json"
            if not cfg_p.exists():
                continue
            try:
                cfg = iteration_arc.ArcConfig.from_yaml(cfg_p)
                state = (
                    iteration_arc.ArcState.from_json(state_p) if state_p.exists()
                    else iteration_arc.ArcState(name=d.name)
                )
                out.append({
                    "name": cfg.name,
                    "status": state.status,
                    "current_iter": state.current_iter,
                    "max_iterations": cfg.max_iterations,
                    "seed_text": cfg.seed_text,
                    "last_update": state.last_update,
                    "webhook_url": cfg.webhook_url,
                })
            except Exception as e:
                out.append({"name": d.name, "error": f"unreadable: {e}"})
    return {"project_id": project_id, "arcs": out}


@router.get("/{arc_name}")
def get_arc(project_id: int, arc_name: str) -> dict:
    """Full status for a single arc, hydrated from ARC.yaml + STATE.json."""
    proj = _project_or_404(project_id)
    arc_folder = _arcs_root(proj["repo_path"]) / arc_name
    return _read_arc_status(arc_folder)


# ---------------------------------------------------------------------------
# Stepping
# ---------------------------------------------------------------------------

@router.post("/{arc_name}/step")
def step_arc(project_id: int, arc_name: str, payload: StepArcRequest | None = None) -> dict:
    """Advance the arc by one iteration; evaluates the predicate and may terminate.

    # RECONSTRUCTED: endpoint shape inferred from iteration_arc.step_arc(repo,
    # arc_name) -> dict. The original may have run this in the background; here
    # it delegates synchronously and returns the post-step status.
    """
    proj = _project_or_404(project_id)
    try:
        iteration_arc.step_arc(Path(proj["repo_path"]), arc_name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"arc {arc_name!r} not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    arc_folder = _arcs_root(proj["repo_path"]) / arc_name
    return _read_arc_status(arc_folder)
