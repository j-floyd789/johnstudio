"""Worker base class. Workers know how to launch themselves given a prompt path
and a worktree. They never spawn other workers — only the orchestrator does that.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .. import tmux_controller
from ..models import WorkerConfig


@dataclass
class LaunchHandle:
    worker_name: str
    pid: int | None = None
    tmux_session: str | None = None
    tmux_pane: str | None = None
    log_path: str | None = None


class BaseWorker:
    """Subclass for each provider (claude/codex/gemini/terminal)."""

    def __init__(self, worker_name: str, cfg: WorkerConfig):
        self.worker_name = worker_name
        self.cfg = cfg

    def is_available(self) -> bool:
        from .. import utils
        if self.cfg.always_available:
            return True
        return bool(utils.which(self.cfg.command))

    # -----------------------------------------------------------------
    # Launch
    # -----------------------------------------------------------------

    def launch(
        self,
        *,
        cwd: Path,
        prompt_path: Path,
        log_path: Path,
        session: str | None = None,
    ) -> LaunchHandle:
        """Launch the worker. Concrete subclasses override `build_command`."""
        cmd = self.build_command(prompt_path, log_path=log_path)
        # Prefer tmux when available AND a session is provided.
        if session and tmux_controller.is_available():
            # Adapters return cmd lists like ["sh", "-c", "<pipeline>"];
            # plain " ".join leaks the pipeline's shell metachars (|, ;) up
            # to the OUTER sh -c that tmux wraps around it, which mis-parses
            # the whole thing. shlex.join preserves quoting so the inner
            # sh -c receives the pipeline as a single argument.
            import shlex
            pane = tmux_controller.split_pane(session, cwd=cwd, cmd=shlex.join(cmd))
            return LaunchHandle(
                worker_name=self.worker_name,
                tmux_session=session, tmux_pane=pane,
                log_path=str(log_path),
            )
        pid = tmux_controller.launch_subprocess(cmd, cwd=cwd, log_path=log_path)
        return LaunchHandle(
            worker_name=self.worker_name,
            pid=pid, log_path=str(log_path),
        )

    def build_command(self, prompt_path: Path, *, log_path: Path | None = None) -> list[str]:
        """Build the shell command list for this worker.

        `log_path` is passed by `launch()` so adapters that want to capture
        a structured event stream (e.g. Claude's `--output-format stream-json`)
        can tee it to a sidecar file alongside the human-readable log.
        Most adapters ignore it.
        """
        raise NotImplementedError
