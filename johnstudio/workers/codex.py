"""Codex CLI adapter.

Uses `codex exec --json` (non-interactive, emits JSONL events to stdout).
Same launch pattern as Claude: prompt piped on stdin, stdout teed to a
sidecar `.jsonl` file so worker_events.py can parse it in real time.

Sandbox policy is gated on WorkerConfig.can_edit:
- can_edit=True  → `--dangerously-bypass-approvals-and-sandbox`
                   (the worktree is the isolation boundary; matches the
                    claude_backend autonomy model).
- can_edit=False → `--sandbox read-only` so reviewer-style roles can't
                   silently edit code.
"""
from __future__ import annotations

import shlex
from pathlib import Path

from .base import BaseWorker
from ..models import WorkerConfig


class CodexWorker(BaseWorker):
    def build_command(self, prompt_path: Path, *, log_path: Path | None = None) -> list[str]:
        prompt = shlex.quote(str(prompt_path))
        if self.cfg.can_edit:
            sandbox = "--dangerously-bypass-approvals-and-sandbox"
        else:
            sandbox = "--sandbox read-only"
        # Per-role model override from the WorkerConfig.model field
        # (populated from seeds/roles/<vp>/<role>.md frontmatter for
        # team mode; can be set in seeds/default_config.yaml workers
        # for parallel/chain mode).
        model_flag = f" -m {shlex.quote(self.cfg.model)}" if self.cfg.model else ""
        effort_flag = f" -c model_reasoning_effort={shlex.quote(self.cfg.effort)}" if getattr(self.cfg, "effort", None) else ""
        if log_path is not None:
            jsonl = shlex.quote(str(Path(log_path).with_suffix(".jsonl")))
            tee = f" | tee {jsonl}"
        else:
            tee = ""
        # Trailing `-` tells codex exec to read the prompt from stdin.
        # See workers/claude.py for why we need pipefail here.
        cmd = (
            f"set -o pipefail; cat {prompt} | {self.cfg.command} exec {sandbox}{model_flag}{effort_flag} --json -{tee}"
        )
        return ["sh", "-c", cmd]


def make(worker_name: str, cfg: WorkerConfig) -> CodexWorker:
    return CodexWorker(worker_name, cfg)
