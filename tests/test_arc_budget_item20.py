"""Items 20 & 13 — cost-threshold emission + per-task cost breakdown.

- arc_budget emits COST_THRESHOLD_CROSSED only on a NEW band crossing.
- task_cost_breakdown returns per-run cost joined to worker name/role.
"""
from __future__ import annotations

import pytest

from johnstudio import arc_budget, db
from johnstudio.hooks import EventTypes, bus


def _seed_task(conn, *, task_number: int, cost: float) -> int:
    db.init_schema(conn)
    pid = conn.execute(
        "INSERT INTO projects (name, repo_path) VALUES (?, ?)",
        (f"proj-{task_number}", "/tmp/repo"),
    ).lastrowid
    cur = conn.execute(
        "INSERT INTO tasks (project_id, task_number, title, description, cost_usd) "
        "VALUES (?, ?, ?, ?, ?)",
        (pid, task_number, f"t{task_number}", "desc", cost),
    )
    conn.commit()
    return int(cur.lastrowid)


def _seed_run(conn, *, task_id: int, worker_name: str, role: str, cost: float) -> int:
    wid = conn.execute(
        "INSERT INTO workers (name, provider, role, command) VALUES (?, ?, ?, ?)",
        (worker_name, "claude", role, "claude"),
    ).lastrowid
    cur = conn.execute(
        "INSERT INTO runs (task_id, worker_id, status, cost_usd) VALUES (?, ?, ?, ?)",
        (task_id, wid, "done", cost),
    )
    conn.commit()
    return int(cur.lastrowid)


@pytest.fixture
def _bus_capture():
    """Subscribe to COST_THRESHOLD_CROSSED, yield the captured list, cleanup."""
    captured: list[dict] = []
    token = bus.subscribe(
        EventTypes.COST_THRESHOLD_CROSSED, lambda e, p: captured.append(p)
    )
    arc_budget.reset_threshold_state()
    try:
        yield captured
    finally:
        bus.unsubscribe(token)
        arc_budget.reset_threshold_state()


def test_threshold_emitted_once_per_band(jh_home, _bus_capture):
    conn = db.connect()
    # one iteration whose task costs $8 against a $10 cap = 80% band
    tid = _seed_task(conn, task_number=1, cost=8.0)
    iters = [{"iter": 1, "task_number": 1, "task_db_id": tid}]

    arc_budget.arc_cost_status("arc-x", iters, budget_usd=10.0, conn=conn)
    arc_budget.arc_cost_status("arc-x", iters, budget_usd=10.0, conn=conn)  # recompute

    assert len(_bus_capture) == 1, "must not re-emit the same band"
    evt = _bus_capture[0]
    assert evt["band"] == "80%"
    assert evt["arc"] == "arc-x"
    assert evt["total_cost_usd"] == 8.0
    assert evt["fraction"] == pytest.approx(0.8)
    conn.close()


def test_threshold_reemits_on_higher_band(jh_home, _bus_capture):
    conn = db.connect()
    tid = _seed_task(conn, task_number=2, cost=4.0)
    iters = [{"iter": 1, "task_number": 2, "task_db_id": tid}]
    arc_budget.arc_cost_status("arc-y", iters, budget_usd=10.0, conn=conn)  # 40% -> none
    assert _bus_capture == []

    # bump the task cost to 6 (60%) -> crosses 50% band
    conn.execute("UPDATE tasks SET cost_usd = 6.0 WHERE id = ?", (tid,))
    conn.commit()
    arc_budget.arc_cost_status("arc-y", iters, budget_usd=10.0, conn=conn)
    assert [e["band"] for e in _bus_capture] == ["50%"]

    # to 10 (100%)
    conn.execute("UPDATE tasks SET cost_usd = 10.0 WHERE id = ?", (tid,))
    conn.commit()
    arc_budget.arc_cost_status("arc-y", iters, budget_usd=10.0, conn=conn)
    assert [e["band"] for e in _bus_capture] == ["50%", "100%"]
    conn.close()


def test_threshold_usd_bands_when_no_cap(jh_home, _bus_capture):
    conn = db.connect()
    tid = _seed_task(conn, task_number=3, cost=6.0)
    iters = [{"iter": 1, "task_number": 3, "task_db_id": tid}]
    arc_budget.arc_cost_status("arc-z", iters, budget_usd=None, conn=conn)
    assert len(_bus_capture) == 1
    assert _bus_capture[0]["band"] == "$5"
    assert _bus_capture[0]["budget_usd"] is None
    conn.close()


def test_task_cost_breakdown(jh_home):
    conn = db.connect()
    tid = _seed_task(conn, task_number=10, cost=3.0)
    _seed_run(conn, task_id=tid, worker_name="claude-quant", role="quant", cost=2.0)
    _seed_run(conn, task_id=tid, worker_name="gemini-bt", role="backtester", cost=1.0)

    bd = arc_budget.task_cost_breakdown(tid, conn=conn)
    assert bd["task_db_id"] == tid
    assert bd["total_cost_usd"] == 3.0
    assert bd["runs_cost_usd"] == 3.0
    assert bd["tokens_available"] is False
    assert len(bd["workers"]) == 2
    by_name = {w["worker"]: w for w in bd["workers"]}
    assert by_name["claude-quant"]["cost_usd"] == 2.0
    assert by_name["claude-quant"]["role"] == "quant"
    assert by_name["gemini-bt"]["tokens"] is None
    conn.close()


def test_task_cost_breakdown_no_runs(jh_home):
    conn = db.connect()
    tid = _seed_task(conn, task_number=11, cost=0.0)
    bd = arc_budget.task_cost_breakdown(tid, conn=conn)
    assert bd["workers"] == []
    assert bd["runs_cost_usd"] == 0.0
    conn.close()
