"""Coverage for the loopback bearer-token gate (johnstudio/auth.py)."""
from __future__ import annotations

import os
import stat

import pytest
from fastapi.testclient import TestClient

from johnstudio import auth, init as init_mod
from johnstudio.server import create_app


@pytest.fixture
def authed_client(jh_home):
    init_mod.run_init()
    app = create_app(require_auth=True)
    return TestClient(app)


def test_health_is_public(authed_client):
    r = authed_client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_doctor_requires_token(authed_client):
    r = authed_client.get("/api/doctor")
    assert r.status_code == 401


def test_doctor_accepts_correct_token(authed_client):
    token = auth.get_or_create_token()
    r = authed_client.get(
        "/api/doctor",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200


def test_doctor_rejects_bad_token(authed_client):
    r = authed_client.get(
        "/api/doctor",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert r.status_code == 401


def test_token_file_is_600(jh_home):
    # Force generation
    auth.get_or_create_token()
    p = auth.token_path()
    assert p.exists()
    mode = stat.S_IMODE(p.stat().st_mode)
    # On Windows file mode bits are not enforced; skip there.
    if os.name == "posix":
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_request_id_header_is_set(authed_client):
    r = authed_client.get("/api/health")
    assert "x-request-id" in r.headers
    assert len(r.headers["x-request-id"]) >= 8
