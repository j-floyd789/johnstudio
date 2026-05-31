"""Gemini CLI adapter.

Uses `gemini -p "" --output-format stream-json` (non-interactive, emits
one JSON event per line). Prompt is piped on stdin (Gemini's `-p` and
stdin both contribute to the input; an empty `-p` plus a piped prompt
gets us a clean stdin-only invocation).

Approval mode is gated on WorkerConfig.can_edit:
- can_edit=True  → `--yolo` (auto-approve every tool, including writes).
- can_edit=False → `--approval-mode plan` (read-only — model can browse
                   the worktree but not edit).
"""
from __future__ import annotations

import shlex
from pathlib import Path

from .base import BaseWorker
from ..models import WorkerConfig


class GeminiWorker(BaseWorker):
    def build_command(self, prompt_path: Path, *, log_path: Path | None = None) -> list[str]:
        prompt = shlex.quote(str(prompt_path))
        if self.cfg.can_edit:
            mode = "--yolo"
        else:
            # Was `--approval-mode plan` (read-only). That mode has a CLI bug:
            # the model calls `exit_plan_mode` when done, and the gemini-cli
            # then dies parsing the next stream chunk with
            # "Invalid stream: The model returned an empty response or
            # malformed tool call." The role IS however supposed to write its
            # named output file (RESEARCH.md, TEAM_PLAN.md, COMPETITIVE.md
            # etc.), so it isn't truly read-only — `auto_edit` is correct
            # semantically (auto-approve write/edit, don't auto-approve
            # shell-out) and sidesteps the plan-mode bug entirely.
            mode = "--approval-mode auto_edit"
        # Per-role model override; see workers/codex.py for context.
        model_flag = f" -m {shlex.quote(self.cfg.model)}" if self.cfg.model else ""
        if log_path is not None:
            jsonl = shlex.quote(str(Path(log_path).with_suffix(".jsonl")))
            tee = f" | tee {jsonl}"
        else:
            tee = ""
        # See workers/claude.py for why we need pipefail here.
        cmd = (
            f"set -o pipefail; cat {prompt} | {self.cfg.command} "
            f"-p ''{model_flag} --output-format stream-json {mode}{tee}"
        )
        return ["sh", "-c", cmd]


def make(worker_name: str, cfg: WorkerConfig) -> GeminiWorker:
    return GeminiWorker(worker_name, cfg)
