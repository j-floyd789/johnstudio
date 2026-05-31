"""tmux controller with subprocess fallback.

If tmux is not available, workers are launched as background subprocesses with
stdout/stderr redirected to `<task>/logs/<worker>.log`. Either way, the
controller exposes the same surface to the orchestrator.
"""
from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from . import utils

# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------

def is_available() -> bool:
    return utils.which("tmux") is not None


# ---------------------------------------------------------------------------
# tmux mode
# ---------------------------------------------------------------------------

def new_session(name: str, cwd: str | Path) -> None:
    if session_exists(name):
        return
    cp = utils.run(["tmux", "new-session", "-d", "-s", name, "-c", str(cwd)])
    if cp.returncode != 0:
        raise RuntimeError(f"tmux new-session failed: {cp.stderr or cp.stdout}")


def session_exists(name: str) -> bool:
    cp = utils.run(["tmux", "has-session", "-t", name])
    return cp.returncode == 0


def kill_session(name: str) -> None:
    utils.run(["tmux", "kill-session", "-t", name])


def split_pane(session: str, cwd: str | Path, cmd: str) -> str:
    """Split horizontally and run `cmd` in the new pane. Returns pane id like '%3'."""
    cp = utils.run(
        ["tmux", "split-window", "-t", session, "-h", "-c", str(cwd), "-P", "-F", "#{pane_id}",
         f"sh -c {shlex.quote(cmd + '; exec sh -i')}"],
    )
    if cp.returncode != 0:
        raise RuntimeError(f"tmux split-window failed: {cp.stderr or cp.stdout}")
    return cp.stdout.strip()


def send_keys(session: str, pane: str, text: str) -> None:
    target = f"{session}:.{pane}" if pane.startswith("%") else f"{session}:{pane}"
    utils.run(["tmux", "send-keys", "-t", target, text, "C-m"])


def capture_pane(session: str, pane: str, *, max_lines: int = 500) -> str:
    target = f"{session}:.{pane}" if pane.startswith("%") else f"{session}:{pane}"
    cp = utils.run(["tmux", "capture-pane", "-pt", target, "-S", f"-{max_lines}"])
    return cp.stdout


def list_panes(session: str) -> list[dict]:
    cp = utils.run(
        ["tmux", "list-panes", "-t", session, "-F", "#{pane_id} #{pane_current_command} #{pane_current_path}"]
    )
    if cp.returncode != 0:
        return []
    out: list[dict] = []
    for line in cp.stdout.splitlines():
        parts = line.split(" ", 2)
        if len(parts) >= 3:
            out.append({"pane_id": parts[0], "cmd": parts[1], "cwd": parts[2]})
    return out


# ---------------------------------------------------------------------------
# Subprocess fallback
# ---------------------------------------------------------------------------

def launch_subprocess(cmd: list[str], cwd: str | Path, log_path: str | Path) -> int:
    """Launch a detached subprocess. Returns PID. stdout/stderr → log_path."""
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    f = log_path.open("ab")
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=f, stderr=f, stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc.pid
