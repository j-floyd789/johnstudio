"""Worker factory."""
from __future__ import annotations

from . import claude, codex, gemini, stub, terminal
from ..models import WorkerConfig

_PROVIDER_FACTORIES = {
    "claude": claude.make,
    "codex": codex.make,
    "gemini": gemini.make,
}


def make_worker(worker_name: str, cfg: WorkerConfig):
    if cfg.provider == "terminal":
        if "johnstudio.workers.stub" in cfg.command:
            return stub.make(worker_name, cfg)
        return terminal.make(worker_name, cfg)
    factory = _PROVIDER_FACTORIES.get(cfg.provider)
    if not factory:
        raise ValueError(f"Unknown provider: {cfg.provider}")
    return factory(worker_name, cfg)
