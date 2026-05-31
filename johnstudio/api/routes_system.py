"""System endpoints.

Split into two routers so the server can gate them differently:
- `public_router`: /health — the UI polls this to show the connection badge
  before it has a token; must be unauthenticated.
- `router`: /doctor — exposes environment info (paths, available CLIs);
  goes behind the bearer-token gate.
"""
from __future__ import annotations

from fastapi import APIRouter

from . import _helpers
from .. import __version__

public_router = APIRouter(prefix="/api", tags=["system-public"])
router = APIRouter(prefix="/api", tags=["system"])


@public_router.get("/health")
def health() -> dict:
    return {"ok": True, "version": __version__, "service": "johnstudio"}


@router.get("/doctor")
def doctor() -> dict:
    return _helpers.run_doctor()
