"""Generic terminal worker: runs an arbitrary shell command in tmux/subprocess."""
from __future__ import annotations

from pathlib import Path

from .base import BaseWorker
from ..models import WorkerConfig


class TerminalWorker(BaseWorker):
    def build_command(self, prompt_path: Path, *, log_path: Path | None = None) -> list[str]:
        return ["sh", "-c", f"{self.cfg.command} {prompt_path}"]


def make(worker_name: str, cfg: WorkerConfig) -> TerminalWorker:
    return TerminalWorker(worker_name, cfg)
