"""Tests for the per-task blackboard."""
from __future__ import annotations

import time

import pytest

from johnstudio import blackboard as bb_mod
from johnstudio import db


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    """Point JohnStudio's DB at a fresh per-test home."""
    monkeypatch.setenv("JOHNSTUDIO_HOME", str(tmp_path / "jh"))
    conn = db.connect()
    db.init_schema(conn)
    conn.close()
    return tmp_path


def test_post_then_get_roundtrip(isolated_home):
    bb = bb_mod.Blackboard(project_id=2, task_number=46)
    bb.post(
        key="rejected_candidates",
        value=["cand_4"],
        ttl_seconds=1800,
        agent="code-reviewer",
    )
    entry = bb.get(key="rejected_candidates")
    assert entry is not None
    assert entry.value == ["cand_4"]
    assert entry.agent == "code-reviewer"
    assert entry.posted_at
    assert entry.expires_at > entry.posted_at


def test_get_returns_none_for_missing_key(isolated_home):
    bb = bb_mod.Blackboard(project_id=2, task_number=46)
    assert bb.get(key="nope") is None


def test_append_accumulates_list(isolated_home):
    bb = bb_mod.Blackboard(project_id=2, task_number=46)
    bb.post(
        key="rejected_candidates", value=["cand_4"],
        ttl_seconds=1800, agent="code-reviewer",
    )
    bb.append(
        key="rejected_candidates", value="cand_5",
        ttl_seconds=1800, agent="code-reviewer",
    )
    bb.append(
        key="rejected_candidates", value="cand_6",
        ttl_seconds=1800, agent="code-reviewer",
    )
    entry = bb.get(key="rejected_candidates")
    assert entry is not None
    assert entry.value == ["cand_4", "cand_5", "cand_6"]


def test_append_on_missing_key_starts_list(isolated_home):
    bb = bb_mod.Blackboard(project_id=2, task_number=46)
    bb.append(
        key="claimed", value="cand_7", ttl_seconds=1800, agent="impl",
    )
    entry = bb.get(key="claimed")
    assert entry is not None
    assert entry.value == ["cand_7"]


def test_ttl_expires_entry(isolated_home):
    bb = bb_mod.Blackboard(project_id=2, task_number=46)
    # 1-second TTL — sleep past it.
    bb.post(key="ephemeral", value=42, ttl_seconds=1, agent="x")
    assert bb.get(key="ephemeral") is not None
    time.sleep(1.2)
    assert bb.get(key="ephemeral") is None
    # And it stays out of list()/snapshot() too.
    assert bb.list() == []
    assert bb.snapshot() == {}


def test_snapshot_returns_live_only(isolated_home):
    bb = bb_mod.Blackboard(project_id=2, task_number=46)
    bb.post(key="alive", value="ok", ttl_seconds=600, agent="r")
    bb.post(key="dying", value="bye", ttl_seconds=1, agent="r")
    bb.post(key="also_alive", value=[1, 2, 3], ttl_seconds=600, agent="r")
    time.sleep(1.2)
    snap = bb.snapshot()
    assert snap == {"alive": "ok", "also_alive": [1, 2, 3]}


def test_list_returns_live_entries(isolated_home):
    bb = bb_mod.Blackboard(project_id=2, task_number=46)
    bb.post(key="a", value=1, ttl_seconds=600, agent="x")
    bb.post(key="b", value=2, ttl_seconds=600, agent="y")
    entries = bb.list()
    assert len(entries) == 2
    by_key = {e["key"]: e for e in entries}
    assert by_key["a"]["value"] == 1
    assert by_key["a"]["agent"] == "x"
    assert by_key["b"]["value"] == 2


def test_unique_key_upsert_replaces_value(isolated_home):
    bb = bb_mod.Blackboard(project_id=2, task_number=46)
    bb.post(key="winner", value="cand_1", ttl_seconds=600, agent="a")
    bb.post(key="winner", value="cand_2", ttl_seconds=600, agent="b")
    entry = bb.get(key="winner")
    assert entry is not None
    assert entry.value == "cand_2"
    assert entry.agent == "b"
    # And the table has exactly one row for that key.
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM blackboard "
            "WHERE project_id=? AND task_number=? AND key=?",
            (2, 46, "winner"),
        ).fetchone()
    finally:
        conn.close()
    assert row["c"] == 1


def test_scope_isolation_between_tasks(isolated_home):
    a = bb_mod.Blackboard(project_id=2, task_number=46)
    b = bb_mod.Blackboard(project_id=2, task_number=47)
    a.post(key="k", value="from-46", ttl_seconds=600, agent="r")
    assert a.get(key="k").value == "from-46"
    assert b.get(key="k") is None
    assert b.snapshot() == {}


def test_gc_deletes_expired_rows(isolated_home):
    bb = bb_mod.Blackboard(project_id=2, task_number=46)
    bb.post(key="live", value=1, ttl_seconds=600, agent="r")
    bb.post(key="dead", value=2, ttl_seconds=1, agent="r")
    time.sleep(1.2)
    deleted = bb_mod.Blackboard.gc()
    assert deleted == 1
    # Live row is still there.
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT key FROM blackboard WHERE project_id=? AND task_number=?",
            (2, 46),
        ).fetchall()
    finally:
        conn.close()
    assert [r["key"] for r in rows] == ["live"]


def test_post_rejects_nonpositive_ttl(isolated_home):
    bb = bb_mod.Blackboard(project_id=2, task_number=46)
    with pytest.raises(ValueError):
        bb.post(key="x", value=1, ttl_seconds=0, agent="r")
