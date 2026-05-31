from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from johnstudio import workers
from johnstudio.models import WorkerConfig
from johnstudio.workers import stub


def _stub_cfg() -> WorkerConfig:
    return WorkerConfig(
        provider="terminal",
        command="python -m johnstudio.workers.stub",
        role="test_worker",
        can_edit=True, worktree=True, max_runtime_minutes=5,
        always_available=True,
    )


def test_make_worker_dispatch():
    w = workers.make_worker("terminal_stub", _stub_cfg())
    assert isinstance(w, stub.TerminalStubWorker)


def test_stub_main_writes_result_and_done(tmp_path):
    # Simulate the orchestrator setup: a worktree with a prompt file.
    prompt = tmp_path / "prompts" / "x.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("# Task\n\nDo the stub thing.\n")
    wt = tmp_path / "worktree"
    wt.mkdir()

    # Invoke as subprocess so cwd handling matches real launch.
    result = subprocess.run(
        [sys.executable, "-m", "johnstudio.workers.stub", str(prompt)],
        cwd=wt, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert (wt / "RESULT.md").exists()
    assert (wt / "DONE.md").exists()
    assert (wt / "STUB_NOTE.md").exists()
    done = (wt / "DONE.md").read_text()
    assert "status: COMPLETE" in done
    result_text = (wt / "RESULT.md").read_text()
    for section in (
        "## Summary", "## Files changed", "## Tests run", "## Risks",
        "## Blockers", "## Handoff requests", "## Skill feedback",
        "## New memory facts", "## Suggested tags/entities", "## Next recommended action",
    ):
        assert section in result_text


def test_stub_main_missing_prompt(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "johnstudio.workers.stub", str(tmp_path / "nope")],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode == 2


def test_stub_worker_is_always_available():
    w = workers.make_worker("terminal_stub", _stub_cfg())
    assert w.is_available()
