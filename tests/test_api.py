from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from johnstudio import init as init_mod
from johnstudio.server import create_app


@pytest.fixture
def client(jh_home, git_repo):
    init_mod.run_init()
    app = create_app(require_auth=False)
    return TestClient(app)


def _add_project(client, name="demo", repo_path=None):
    r = client.post("/api/projects", json={"name": name, "repo_path": str(repo_path)})
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------

def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert "version" in r.json()


def test_doctor(client):
    r = client.get("/api/doctor")
    assert r.status_code == 200
    data = r.json()
    assert "tools" in data
    assert "workers" in data
    names = {w["name"] for w in data["workers"]}
    assert "terminal_stub" in names


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

def test_projects_crud(client, git_repo):
    assert client.get("/api/projects").json() == []
    out = _add_project(client, repo_path=git_repo)
    assert out["project_id"] == 1
    rows = client.get("/api/projects").json()
    assert len(rows) == 1 and rows[0]["name"] == "demo"
    r = client.get("/api/projects/1")
    assert r.status_code == 200
    assert r.json()["name"] == "demo"
    assert "config" in r.json()
    assert client.get("/api/projects/999").status_code == 404


def test_create_project_rejects_non_git(client, tmp_path):
    notgit = tmp_path / "notgit"
    notgit.mkdir()
    r = client.post("/api/projects", json={"name": "x", "repo_path": str(notgit)})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Tasks: end-to-end stub flow
# ---------------------------------------------------------------------------

def test_task_lifecycle_stub_only(client, git_repo):
    _add_project(client, repo_path=git_repo)

    # run
    r = client.post(
        "/api/projects/1/tasks/run",
        json={"task": "hello", "stub_only": True},
    )
    assert r.status_code == 201, r.text
    run_out = r.json()
    task_n = run_out["task_number"]
    assert run_out["team"] == ["terminal_stub"]

    # wait for DONE.md
    deadline = time.time() + 15
    while time.time() < deadline:
        if (git_repo / ".johnstudio" / "worktrees" / f"task-{task_n:04d}-terminal-stub" / "DONE.md").exists():
            break
        time.sleep(0.2)

    # list tasks
    r = client.get("/api/projects/1/tasks")
    assert r.status_code == 200
    assert any(t["task_number"] == task_n for t in r.json())

    # status
    r = client.get(f"/api/projects/1/tasks/{task_n}")
    assert r.status_code == 200
    assert r.json()["runs"]

    # collect
    r = client.post(f"/api/projects/1/tasks/{task_n}/collect")
    assert r.status_code == 200
    summary = r.json()
    assert summary["runs"][0]["done_present"] is True

    # review
    r = client.post(f"/api/projects/1/tasks/{task_n}/review")
    assert r.status_code == 200
    rv = r.json()
    assert rv["recommended"] == "terminal_stub"

    # artifacts
    r = client.get(f"/api/projects/1/tasks/{task_n}/results")
    files = r.json()
    assert any(f["name"] == "terminal_stub_RESULT.md" for f in files)
    r = client.get(f"/api/projects/1/tasks/{task_n}/diffs")
    assert r.status_code == 200
    r = client.get(f"/api/projects/1/tasks/{task_n}/context-packs")
    assert r.status_code == 200
    r = client.get(f"/api/projects/1/tasks/{task_n}/review")
    assert r.status_code == 200
    assert r.json()["exists"] is True
    assert "Scores" in r.json()["content"]
    r = client.get(f"/api/projects/1/tasks/{task_n}/merge-plan")
    assert r.json()["exists"] is True
    r = client.get(f"/api/projects/1/tasks/{task_n}/safety-report")
    assert r.status_code == 200

    # merge — first without confirm should 409, then with confirm should succeed
    r = client.post(
        f"/api/projects/1/tasks/{task_n}/merge",
        json={"worker_name": "terminal_stub", "confirm": False},
    )
    assert r.status_code == 409
    r = client.post(
        f"/api/projects/1/tasks/{task_n}/merge",
        json={"worker_name": "terminal_stub", "confirm": True},
    )
    assert r.status_code == 200
    assert r.json()["merged"] is True


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

def test_skills_list_and_enable(client):
    rows = client.get("/api/skills").json()
    assert len(rows) >= 10
    target = rows[0]["skill_id"]
    r = client.post(f"/api/skills/{target}/disable")
    assert r.status_code == 200 and r.json()["enabled"] is False
    r = client.post(f"/api/skills/{target}/enable")
    assert r.status_code == 200 and r.json()["enabled"] is True
    r = client.get(f"/api/skills/{target}")
    assert r.status_code == 200
    assert r.json()["files"]["metadata_yaml"]


def test_skills_discover(client, git_repo):
    _add_project(client, repo_path=git_repo)
    r = client.post(
        "/api/projects/1/skills/discover",
        json={"task": "build a Next.js login page", "agent_role": "frontend_implementer"},
    )
    assert r.status_code == 200
    # frontend-react-specialist should at least be considered (score may be 0 without nextjs detected)
    assert isinstance(r.json(), list)


def test_skill_sources_lifecycle(client):
    r = client.post("/api/skills/source", json={"uri": str(Path.cwd())})
    assert r.status_code == 201
    r = client.get("/api/skills/sources")
    assert r.status_code == 200 and len(r.json()) >= 1


# ---------------------------------------------------------------------------
# Memory + graph
# ---------------------------------------------------------------------------

def test_memory_listing_and_file(client, git_repo):
    _add_project(client, repo_path=git_repo)
    r = client.get("/api/projects/1/memory/files")
    files = r.json()
    assert any(f["path"] == "00_index.md" for f in files)
    r = client.get("/api/projects/1/memory/file", params={"path": "00_index.md"})
    assert r.status_code == 200 and "Memory Index" in r.json()["content"]
    # traversal guard
    r = client.get("/api/projects/1/memory/file", params={"path": "../../../../etc/passwd"})
    assert r.status_code == 404


def test_memory_graph_entities(client, git_repo):
    _add_project(client, repo_path=git_repo)
    r = client.get("/api/projects/1/memory/entities")
    assert r.status_code == 200
    assert any(e["entity_type"] == "project" for e in r.json())


def test_memory_validate_repair(client, git_repo):
    _add_project(client, repo_path=git_repo)
    r = client.post("/api/projects/1/memory/validate")
    assert r.json()["ok"] is True
    # remove a directory and validate again
    (git_repo / ".johnstudio" / "memory" / "decisions").rmdir()
    r = client.post("/api/projects/1/memory/validate")
    assert r.json()["ok"] is False
    r = client.post("/api/projects/1/memory/repair")
    assert r.json()["repaired"] is True
    r = client.post("/api/projects/1/memory/validate")
    assert r.json()["ok"] is True


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------

def test_workers_list(client):
    rows = client.get("/api/workers").json()
    names = {w["name"] for w in rows}
    assert "terminal_stub" in names
    stub = next(w for w in rows if w["name"] == "terminal_stub")
    assert stub["is_available"] is True


def test_worker_test_endpoint_stub(client):
    r = client.post("/api/workers/terminal_stub/test")
    assert r.status_code == 200
    data = r.json()
    assert data["available"] is True
    assert data.get("tested") is True
    assert data.get("ok") is True


def test_worker_test_unknown_404(client):
    r = client.post("/api/workers/bogus/test")
    assert r.status_code == 404
