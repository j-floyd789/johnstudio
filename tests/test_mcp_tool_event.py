"""Item 20 — MCP_TOOL_CALLED emit on tools/call."""
from __future__ import annotations

import pytest

from johnstudio import mcp_server, project
from johnstudio.hooks import EventTypes, bus


@pytest.fixture
def registered_project(jh_home, git_repo):
    res = project.add_project("demo", git_repo)
    return {"name": "demo", "id": res["project_id"], "repo": git_repo}


def _req(name, arguments):
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}


def test_tools_call_emits_mcp_tool_called(jh_home, registered_project):
    seen = []
    tok = bus.subscribe(EventTypes.MCP_TOOL_CALLED, lambda e, p: seen.append(p))
    try:
        resp = mcp_server.handle_message(_req("list_projects", {}))
        assert "result" in resp, resp
    finally:
        bus.unsubscribe(tok)
    assert len(seen) == 1
    assert seen[0]["tool"] == "list_projects"
    assert "args_summary" in seen[0]
    assert seen[0]["task_id"] is None


def test_unknown_tool_does_not_emit(jh_home):
    seen = []
    tok = bus.subscribe(EventTypes.MCP_TOOL_CALLED, lambda e, p: seen.append(p))
    try:
        resp = mcp_server.handle_message(_req("does_not_exist", {}))
        # unknown-tool path raises ToolError before emit
        assert "error" in resp
    finally:
        bus.unsubscribe(tok)
    assert seen == []


def test_args_summary_is_bounded():
    big = {"blob": "x" * 5000}
    s = mcp_server._args_summary(big, max_len=120)
    assert len(s) <= 120


def test_task_id_lifted_from_args():
    seen = []
    tok = bus.subscribe(EventTypes.MCP_TOOL_CALLED, lambda e, p: seen.append(p))
    try:
        mcp_server._emit_tool_called("some_tool", {"task_id": 42, "x": 1})
    finally:
        bus.unsubscribe(tok)
    assert seen and seen[0]["task_id"] == 42
