"""Claude Code CLI adapter.

Uses `claude --print` (non-interactive) and pipes the JohnStudio context pack
in on stdin. Runs as a subprocess inside the worker's git worktree.

Why --print:
    - One-shot: exits when done, so the orchestrator can mark the run completed.
    - No tmux send-keys timing assumptions.
    - Verified end-to-end against claude 2.x: produces RESULT.md + DONE.md and
      commits inside the worktree when the prompt asks.

Permission model — gated on WorkerConfig.can_edit:
    - can_edit=True  (implementer roles): `--dangerously-skip-permissions`.
      The worktree is the safety boundary: separate branch, throwaway path
      under `.johnstudio/worktrees/`, human merge gate before anything lands.
    - can_edit=False (RFC drafter / reviewer roles): `--permission-mode
      acceptEdits`. The agent can still write its single output artifact
      (RFC.md / REVIEW_<n>.md) without prompting, but any Bash invocation
      requires a TTY-style approval that --print cannot answer, so shell
      execution fails closed. This enforces role discipline — reviewers don't
      silently edit code, RFC drafters don't curl the internet.
"""
from __future__ import annotations

import shlex
from pathlib import Path

from .base import BaseWorker
from ..models import WorkerConfig


class ClaudeWorker(BaseWorker):
    def build_command(self, prompt_path: Path, *, log_path: Path | None = None) -> list[str]:
        prompt = shlex.quote(str(prompt_path))
        if self.cfg.can_edit:
            perm_flag = "--dangerously-skip-permissions"
        else:
            perm_flag = "--permission-mode acceptEdits"
        # Per-role model override (Opus for hard work, Haiku for trivia).
        model_flag = f" --model {shlex.quote(self.cfg.model)}" if self.cfg.model else ""
        effort_flag = f" --effort {shlex.quote(self.cfg.effort)}" if getattr(self.cfg, "effort", None) else ""
        # Per-role tool allowlist — read-only reviewers can't reach for Edit,
        # researchers can't shell out, etc. The catalog enforces that `Task`
        # is in `allowed_tools` ONLY for roles with `can_spawn_subagents:
        # true` in their frontmatter; this worker just plumbs both sides
        # consistently. If subagents are opted in, we drop the belt-and-
        # suspenders Task deny so the CLI honors the allowlist.
        if self.cfg.allowed_tools:
            allowed = list(self.cfg.allowed_tools)
            if self.cfg.can_spawn_subagents and "Task" not in allowed:
                allowed.append("Task")
            tools_csv = ",".join(allowed)
            tools_flag = f" --allowed-tools {shlex.quote(tools_csv)}"
            if not self.cfg.can_spawn_subagents:
                # Belt-and-suspenders explicit deny for roles that haven't
                # opted in to subagent spawning.
                tools_flag += " --disallowed-tools Task"
        else:
            tools_flag = ""
        # stream-json emits one JSON object per line: system/init, assistant
        # turns (text + tool_use), user turns (tool_result), and the final
        # result. The graph view's "current step" text per node is derived
        # from this stream. We tee it to <log_path>.jsonl so a tailer can
        # parse events without competing with the tmux pane buffer.
        if log_path is not None:
            jsonl = shlex.quote(str(Path(log_path).with_suffix(".jsonl")))
            tee = f" | tee {jsonl}"
        else:
            tee = ""
        # pipefail makes a `cat | claude | tee` pipeline exit with the
        # leftmost non-zero status. Without it, `tee`'s success masks a
        # crashed/OOM/rate-limited `claude`, and the run sits at
        # `status='launched'` forever.
        cmd = (
            f"set -o pipefail; cat {prompt} | {self.cfg.command} "
            f"--print {perm_flag}{model_flag}{effort_flag}{tools_flag} --output-format stream-json --verbose"
            f"{tee}"
        )
        return ["sh", "-c", cmd]


def make(worker_name: str, cfg: WorkerConfig) -> ClaudeWorker:
    return ClaudeWorker(worker_name, cfg)
