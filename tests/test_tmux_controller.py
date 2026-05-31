from __future__ import annotations

import time
from pathlib import Path

import pytest

from johnstudio import tmux_controller as tm


def test_subprocess_fallback_runs_and_logs(tmp_path):
    log = tmp_path / "log.txt"
    pid = tm.launch_subprocess(
        ["python3", "-c", "import sys; sys.stdout.write('hello-stub\\n'); sys.stdout.flush()"],
        cwd=tmp_path, log_path=log,
    )
    assert pid > 0
    # Give it a moment to write.
    for _ in range(30):
        if log.exists() and "hello-stub" in log.read_text():
            break
        time.sleep(0.1)
    assert "hello-stub" in log.read_text()


@pytest.mark.skipif(not tm.is_available(), reason="tmux not installed")
def test_tmux_session_lifecycle(tmp_path):
    name = f"johnstudio-test-{tmp_path.name}"
    try:
        tm.new_session(name, cwd=tmp_path)
        assert tm.session_exists(name)
        panes = tm.list_panes(name)
        assert len(panes) >= 1
        tm.send_keys(name, panes[0]["pane_id"], "echo hi")
        time.sleep(0.3)
        out = tm.capture_pane(name, panes[0]["pane_id"])
        assert "hi" in out or "echo" in out
    finally:
        tm.kill_session(name)
        assert not tm.session_exists(name)
