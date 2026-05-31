"""Tests for the stdlib MCP server (johnstudio.mcp_server)."""
from __future__ import annotations

import io
import json

import pytest

from johnstudio import mcp_server, project


@pytest.fixture
def registered_project(jh_home, git_repo):
    """Register a project against an isolated JohnStudio home + real repo."""
    res = project.add_project("demo", git_repo)
    return {"name": "demo", "id": res["project_id"], "repo": git_repo}


def _req(method, params=None, req_id=1):
    msg = {"jsonrpc": "2.0", "method": method}
    if req_id is not None:
        msg["id"] = req_id
    if params is not None:
        msg["params"] = params
    return msg


def _call_value(name, arguments):
    """tools/call and decode the single JSON text content block back to a value."""
    resp = mcp_server.handle_message(_req("tools/call", {"name": name, "arguments": arguments}))
    assert "result" in resp, resp
    return json.loads(resp["result"]["content"][0]["text"])


def test_initialize_handshake():
    resp = mcp_server.handle_message(_req("initialize", {}))
    assert resp["result"]["protocolVersion"] == mcp_server.PROTOCOL_VERSION
    assert resp["result"]["serverInfo"]["name"] == "johnstudio"


def test_tools_list_exposes_handlers_without_leaking_callables():
    resp = mcp_server.handle_message(_req("tools/list"))
    tools = resp["result"]["tools"]
    names = {t["name"] for t in tools}
    assert "list_projects" in names and "search_memory" in names
    # Internal handler callable must never reach the wire.
    assert all("handler" not in t for t in tools)


def test_notification_returns_no_response():
    # No "id" -> notification -> must not be answered.
    assert mcp_server.handle_message(_req("notifications/initialized", req_id=None)) is None


def test_unknown_method_errors():
    resp = mcp_server.handle_message(_req("does/not/exist"))
    assert resp["error"]["code"] == mcp_server.METHOD_NOT_FOUND


def test_ping():
    resp = mcp_server.handle_message(_req("ping"))
    assert resp["result"] == {}


def test_list_and_get_project(registered_project):
    listed = _call_value("list_projects", {})
    assert any(p["name"] == "demo" for p in listed["projects"])

    got = _call_value("get_project", {"name": "demo"})
    assert got["name"] == "demo"
    assert got["id"] == registered_project["id"]


def test_get_unknown_project_is_clean_error(registered_project):
    resp = mcp_server.handle_message(
        _req("tools/call", {"name": "get_project", "arguments": {"name": "nope"}})
    )
    assert resp["error"]["code"] == mcp_server.INVALID_PARAMS
    assert "unknown project" in resp["error"]["message"]


def test_search_memory_finds_vault_content(registered_project):
    out = _call_value("search_memory", {"project": "demo", "query": "Project Brief"})
    assert out["matches"], "expected to match the seeded project_brief.md heading"
    assert any(m["path"] == "project_brief.md" for m in out["matches"])


def test_read_memory_note(registered_project):
    out = _call_value("read_memory_note", {"project": "demo", "path": "current_state.md"})
    assert "Current State" in out["content"]


def test_read_memory_note_blocks_traversal(registered_project):
    resp = mcp_server.handle_message(
        _req("tools/call", {
            "name": "read_memory_note",
            "arguments": {"project": "demo", "path": "../../../../etc/passwd"},
        })
    )
    assert resp["error"]["code"] == mcp_server.INVALID_PARAMS


def test_graph_entities_lists_project_entity(registered_project):
    out = _call_value("list_graph_entities", {"project": "demo"})
    # add_project() seeds a 'project' entity in the graph.
    assert any(e["entity_type"] == "project" for e in out["entities"])


def test_serve_stdio_roundtrip(registered_project):
    lines = "\n".join([
        json.dumps(_req("initialize", {})),
        json.dumps(_req("notifications/initialized", req_id=None)),
        json.dumps(_req("tools/call", {"name": "list_projects", "arguments": {}}, req_id=2)),
    ])
    out = io.StringIO()
    mcp_server.serve_stdio(stdin=io.StringIO(lines), stdout=out)
    responses = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
    # initialize (id=1) + tools/call (id=2); the notification produces nothing.
    ids = [r.get("id") for r in responses]
    assert ids == [1, 2]


def test_parse_error_on_garbage_line():
    out = io.StringIO()
    mcp_server.serve_stdio(stdin=io.StringIO("{not json}\n"), stdout=out)
    resp = json.loads(out.getvalue().strip())
    assert resp["error"]["code"] == mcp_server.PARSE_ERROR
