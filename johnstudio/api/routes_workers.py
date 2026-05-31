"""Worker availability and doctor endpoints."""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException

from . import _helpers
from .. import config, workers

router = APIRouter(prefix="/api/workers", tags=["workers"])


@router.get("")
def list_workers() -> list[dict]:
    return _helpers.list_workers(config.load_global_config())


@router.get("/doctor")
def doctor() -> dict:
    return _helpers.run_doctor()


@router.post("/{worker_name}/test")
def test_worker(worker_name: str) -> dict:
    """Smoke-test a worker.

    For terminal_stub: run end-to-end in a temp dir and report RESULT/DONE presence.
    For real CLIs (claude/codex/gemini): report only that the binary is available.
    Full interactive verification is out of MVP scope.
    """
    cfg = config.load_global_config()
    if worker_name not in cfg.workers:
        raise HTTPException(status_code=404, detail=f"unknown worker: {worker_name}")
    wcfg = cfg.workers[worker_name]
    w = workers.make_worker(worker_name, wcfg)
    available = w.is_available()
    if not available:
        return {"available": False, "tested": False, "note": "binary not found on PATH"}
    if wcfg.provider != "terminal":
        return {"available": True, "tested": False, "note": "real CLI; binary present but interactive test not run"}

    # terminal_stub end-to-end
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        prompt = td_path / "prompt.md"
        prompt.write_text("# test\n")
        cp = subprocess.run(
            [sys.executable, "-m", "johnstudio.workers.stub", str(prompt)],
            cwd=td_path, capture_output=True, text=True, timeout=15,
        )
        ok = cp.returncode == 0 and (td_path / "RESULT.md").exists() and (td_path / "DONE.md").exists()
        return {
            "available": True,
            "tested": True,
            "ok": ok,
            "stdout": cp.stdout[-500:],
            "stderr": cp.stderr[-500:],
        }
