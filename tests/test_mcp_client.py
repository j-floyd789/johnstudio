from __future__ import annotations

import json

import pytest

from johnstudio import mcp_client


def test_role_allowlist_filters_servers():
    cfg = mcp_client.build_mcp_config("backend-developer")
    assert set(cfg["mcpServers"]) == {"github", "context7", "n8n"}


def test_unknown_role_falls_back_to_default():
    cfg = mcp_client.build_mcp_config("totally-unknown-role")
    assert set(cfg["mcpServers"]) == {"context7"}


def test_paid_server_is_refused():
    with pytest.raises(mcp_client.PaidServerError):
        mcp_client.build_mcp_config("backend-developer", extra_servers=["exa"])


def test_no_paid_servers_in_catalogue():
    for name, server in mcp_client.CATALOGUE.items():
        assert name not in mcp_client.PAID_DENYLIST
        assert server.free is True


def test_env_keys_resolved_only_when_present():
    cfg = mcp_client.build_mcp_config(
        "backend-developer", env={"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_x"}
    )
    assert cfg["mcpServers"]["github"]["env"] == {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_x"}
    # n8n env vars absent -> no env block emitted
    assert "env" not in cfg["mcpServers"]["n8n"]


def test_write_worker_mcp_json(tmp_path):
    path = mcp_client.write_worker_mcp_json("frontend-developer", tmp_path)
    assert path.name == ".mcp.json"
    data = json.loads(path.read_text())
    assert set(data["mcpServers"]) == {"playwright", "context7"}
    # Every emitted server has a runnable command (CLI-native, no API key URL).
    for s in data["mcpServers"].values():
        assert s["command"] == "npx"
        assert isinstance(s["args"], list) and s["args"]
