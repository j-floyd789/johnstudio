from __future__ import annotations

import os

import pytest
import yaml
from fastapi.testclient import TestClient

from johnstudio import config, init as init_mod
from johnstudio.server import create_app


@pytest.fixture
def stub_client(monkeypatch, tmp_path, git_repo):
    monkeypatch.setenv("JOHNSTUDIO_HOME", str(tmp_path / "home"))
    init_mod.run_init()
    # Rewire claude workers → stub (chain mode defaults to claude_review for
    # architect/rfc_reviewer/reviewer and claude_backend for implementer).
    cfg_path = config.global_config_path()
    cfg = yaml.safe_load(cfg_path.read_text())
    for w in ("claude_backend", "claude_review"):
        cfg["workers"][w] = {
            "provider": "terminal",
            "command": "python -m johnstudio.workers.stub",
            "role": cfg["workers"][w]["role"],
            "can_edit": cfg["workers"][w]["can_edit"],
            "worktree": cfg["workers"][w]["worktree"],
            "max_runtime_minutes": 5,
            "always_available": True,
        }
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    app = create_app(require_auth=False)
    c = TestClient(app)
    # Register the demo project
    c.post("/api/projects", json={"name": "demo", "repo_path": str(git_repo)})
    return c, git_repo


def _advance_until_human_or_terminal(client, pid, tn, max_steps=200, sleep=0.1):
    import time
    out = None
    for _ in range(max_steps):
        out = client.post(f"/api/projects/{pid}/chain/{tn}/advance").json()
        cur = out.get("current") or {}
        if cur.get("phase") in {"rfc_pending_approval", "pending_merge", "conflict", "merged", "rejected"}:
            return out
        if out.get("terminal"):
            return out
        time.sleep(sleep)
    return out


def test_chain_run_happy_path(stub_client):
    client, _ = stub_client
    os.environ["STUB_RFC_VERDICT"] = "approve"
    os.environ["STUB_REVIEW_VERDICT_1"] = "approve"

    r = client.post("/api/projects/1/chain/run", json={"task": "demo chain"})
    assert r.status_code == 201
    tn = r.json()["task_number"]

    # Advance until RFC pending approval
    out = _advance_until_human_or_terminal(client, 1, tn)
    assert (out["current"] or {}).get("phase") == "rfc_pending_approval"

    # Approve RFC
    r = client.post(f"/api/projects/1/chain/{tn}/approve-rfc", json={})
    assert r.status_code == 200

    # Advance until pending_merge
    out = _advance_until_human_or_terminal(client, 1, tn)
    assert (out["current"] or {}).get("phase") == "pending_merge"

    # Status reflects the gate
    s = client.get(f"/api/projects/1/chain/{tn}").json()
    assert s["human_gate"] is True

    # Artifact reads
    rfc = client.get(f"/api/projects/1/chain/{tn}/artifact", params={"kind": "rfc"}).json()
    assert rfc["exists"]
    assert "# RFC" in rfc["content"]
    review = client.get(f"/api/projects/1/chain/{tn}/artifact", params={"kind": "review_1"}).json()
    assert review["exists"]
    assert "Verdict: approve" in review["content"]


def test_chain_run_rfc_reject(stub_client):
    client, _ = stub_client
    os.environ["STUB_RFC_VERDICT"] = "needs-changes"

    r = client.post("/api/projects/1/chain/run", json={"task": "risky"})
    tn = r.json()["task_number"]
    _advance_until_human_or_terminal(client, 1, tn)
    r = client.post(f"/api/projects/1/chain/{tn}/reject-rfc", json={"reason": "scope mismatch"})
    assert r.status_code == 200
    s = client.get(f"/api/projects/1/chain/{tn}").json()
    assert (s["current"] or {}).get("phase") == "rejected"


def test_chain_merge_requires_confirm(stub_client):
    client, _ = stub_client
    os.environ["STUB_RFC_VERDICT"] = "approve"
    os.environ["STUB_REVIEW_VERDICT_1"] = "approve"

    r = client.post("/api/projects/1/chain/run", json={"task": "x"})
    tn = r.json()["task_number"]
    _advance_until_human_or_terminal(client, 1, tn)
    client.post(f"/api/projects/1/chain/{tn}/approve-rfc", json={})
    _advance_until_human_or_terminal(client, 1, tn)

    # confirm:false → 409
    r = client.post(f"/api/projects/1/chain/{tn}/merge", json={"confirm": False})
    assert r.status_code == 409

    # confirm:true → merge succeeds
    r = client.post(f"/api/projects/1/chain/{tn}/merge", json={"confirm": True})
    assert r.status_code == 200
    assert r.json().get("merged") is True
