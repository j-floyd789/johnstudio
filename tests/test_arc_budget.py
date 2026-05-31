"""Tests for Cluster B — per-arc budget tracker.

Covers:
- `arc_budget.arc_cost_status` summing per-iteration task cost from the DB.
- `over_budget` semantics (cost >= cap), no-cap observational mode, and
  graceful handling of iterations whose task row is absent.
- `arc_budget.render_budget_md` / `write_budget_files` output.
- `iteration_arc.step_arc` halting an arc with status `over_budget`
  instead of spawning the next iteration once the cap is hit.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from johnstudio import arc_budget, db, iteration_arc


def _seed_task(conn, *, task_number: int, cost: float) -> int:
    """Insert a project + tasks row and return the task id. Supplies the
    NOT NULL columns; `cost_usd` is what the budget tracker reads."""
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


def test_cost_status_sums_iterations(jh_home):
    conn = db.connect()
    t1 = _seed_task(conn, task_number=1, cost=1.25)
    t2 = _seed_task(conn, task_number=2, cost=0.75)
    iters = [
        {"iter": 1, "task_db_id": t1, "task_number": 1},
        {"iter": 2, "task_db_id": t2, "task_number": 2},
    ]
    st = arc_budget.arc_cost_status("demo", iters, budget_usd=5.0, conn=conn)
    assert st.total_cost_usd == 2.0
    assert st.budget_usd == 5.0
    assert st.over_budget is False
    assert st.remaining_usd == 3.0
    assert st.fraction_used == pytest.approx(0.4)
    assert [c.cost_usd for c in st.iterations] == [1.25, 0.75]
    conn.close()


def test_over_budget_when_cost_meets_cap(jh_home):
    conn = db.connect()
    t1 = _seed_task(conn, task_number=1, cost=4.0)
    t2 = _seed_task(conn, task_number=2, cost=1.0)
    iters = [
        {"iter": 1, "task_db_id": t1, "task_number": 1},
        {"iter": 2, "task_db_id": t2, "task_number": 2},
    ]
    st = arc_budget.arc_cost_status("demo", iters, budget_usd=5.0, conn=conn)
    assert st.total_cost_usd == 5.0
    assert st.over_budget is True          # cost >= cap
    assert st.remaining_usd == 0.0
    conn.close()


def test_no_cap_is_observational(jh_home):
    conn = db.connect()
    t1 = _seed_task(conn, task_number=1, cost=99.0)
    iters = [{"iter": 1, "task_db_id": t1, "task_number": 1}]
    st = arc_budget.arc_cost_status("demo", iters, budget_usd=None, conn=conn)
    assert st.total_cost_usd == 99.0
    assert st.over_budget is False
    assert st.budget_usd is None
    assert st.remaining_usd is None
    assert st.fraction_used is None
    conn.close()


def test_missing_task_row_contributes_zero(jh_home):
    conn = db.connect()
    db.init_schema(conn)
    iters = [{"iter": 1, "task_db_id": 999999, "task_number": 1},
             {"iter": 2, "task_db_id": None, "task_number": 2}]
    st = arc_budget.arc_cost_status("demo", iters, budget_usd=1.0, conn=conn)
    assert st.total_cost_usd == 0.0
    assert st.over_budget is False
    conn.close()


def test_render_and_write_budget_files(tmp_path, jh_home):
    conn = db.connect()
    t1 = _seed_task(conn, task_number=7, cost=2.5)
    iters = [{"iter": 1, "task_db_id": t1, "task_number": 7}]
    st = arc_budget.arc_cost_status("demo", iters, budget_usd=5.0, conn=conn)
    conn.close()

    md = arc_budget.render_budget_md(st)
    assert "Arc budget" in md
    assert "0007" in md

    arc_budget.write_budget_files(tmp_path, st)
    data = json.loads((tmp_path / "BUDGET.json").read_text())
    assert data["total_cost_usd"] == 2.5
    assert (tmp_path / "BUDGET.md").exists()


def test_arc_config_roundtrips_budget(tmp_path):
    cfg = iteration_arc.ArcConfig(
        name="a", project_name="p",
        plan_template_path="t.md", predicate_path="pr.py",
        artifact_glob="art.json", budget_usd=12.5,
    )
    p = tmp_path / "ARC.yaml"
    cfg.to_yaml(p)
    back = iteration_arc.ArcConfig.from_yaml(p)
    assert back.budget_usd == 12.5


def test_step_arc_halts_when_over_budget(tmp_path, jh_home, monkeypatch):
    """A completed iteration whose cumulative cost has hit the cap must
    stop the arc with status `over_budget` rather than spawning iter-2."""
    repo = tmp_path / "repo"
    af = repo / ".johnstudio" / "arcs" / "demo"
    af.mkdir(parents=True)

    # predicate that always says "keep going" (stop=False) so only the
    # budget gate can terminate the arc.
    pred = af / "predicate.py"
    pred.write_text(
        "def predicate(artifact):\n    return (False, 'keep going')\n",
        encoding="utf-8",
    )
    template = af / "plan.md"
    template.write_text("plan {{iter_num}}", encoding="utf-8")

    conn = db.connect()
    tid = _seed_task(conn, task_number=1, cost=10.0)
    conn.close()

    cfg = iteration_arc.ArcConfig(
        name="demo", project_name="p",
        plan_template_path=str(template), predicate_path=str(pred),
        artifact_glob="art.json", max_iterations=10, budget_usd=5.0,
    )
    cfg.to_yaml(af / "ARC.yaml")

    # One completed iteration with a landed artifact.
    artifact = af / "art.json"
    artifact.write_text(json.dumps({"edge_found": False}), encoding="utf-8")
    state = iteration_arc.ArcState(
        name="demo", current_iter=1,
        iterations=[{
            "iter": 1, "task_db_id": tid, "task_number": 1,
            "artifact_path": str(artifact),
        }],
    )
    state.to_json(af / "STATE.json")

    def _should_not_spawn(*a, **k):  # pragma: no cover
        raise AssertionError("arc must not spawn another iteration when over budget")

    monkeypatch.setattr(iteration_arc, "_spawn_next_iteration", _should_not_spawn)

    res = iteration_arc.step_arc(repo, "demo", approve_func=lambda *a, **k: {})
    assert res["status"] == "over_budget"
    reloaded = iteration_arc.ArcState.from_json(af / "STATE.json")
    assert reloaded.status == "over_budget"
    # Budget snapshot files were written.
    assert (af / "BUDGET.json").exists()
    assert (af / "BUDGET.md").exists()
