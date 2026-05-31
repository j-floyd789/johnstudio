"""Per-arc budget tracker (Cluster B).

An iteration arc can optionally carry a spend cap (`ArcConfig.budget_usd`).
This module sums the notional cost of every iteration's underlying team
task (`tasks.cost_usd`, populated by the worker-event cost tailer) and
reports whether the arc has hit its cap.

The gate is "soft": the in-flight iteration always finishes, but
`iteration_arc.step_arc` refuses to spawn a *new* iteration once
`over_budget` is True. With no cap set the tracker is purely
observational (`over_budget` is always False; remaining/fraction are None).

Public surface:
  - `arc_cost_status(arc_name, iterations, budget_usd, *, conn=None)`
  - `render_budget_md(status)` -> str
  - `write_budget_files(arc_folder, status)` -> writes BUDGET.json + BUDGET.md
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import db

# ---------------------------------------------------------------------------
# Item 20 — COST_THRESHOLD_CROSSED emission.
#
# When an arc's rolling spend crosses a budget band we emit a one-shot
# event so consumers (notifier, UI cost meter) can react without polling.
# Two modes:
#   * cap set     -> fractional bands of the cap (50% / 80% / 100%).
#   * no cap       -> fixed USD bands (observational pressure signal).
# We only emit on a NEW (higher) band than the last one seen for that arc,
# so steady-state recompute calls don't spam the bus.
# ---------------------------------------------------------------------------

# Fractional bands (of budget_usd) when a cap is set.
_FRACTION_BANDS: tuple[float, ...] = (0.5, 0.8, 1.0)
# Fixed USD bands when no cap is set — purely observational.
_USD_BANDS: tuple[float, ...] = (1.0, 5.0, 10.0, 25.0, 50.0, 100.0)

# arc_name -> highest band label already emitted. Guarded by a lock so the
# tailer threads that drive recompute don't double-emit on a race.
_last_band_by_arc: dict[str, str] = {}
_band_lock = threading.Lock()


@dataclass
class IterationCost:
    """The cost contribution of a single arc iteration."""
    iter: int
    task_number: int
    task_db_id: int | None
    cost_usd: float


@dataclass
class ArcBudgetStatus:
    arc_name: str
    total_cost_usd: float
    budget_usd: float | None
    over_budget: bool
    remaining_usd: float | None
    fraction_used: float | None
    iterations: list[IterationCost] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "arc_name": self.arc_name,
            "total_cost_usd": self.total_cost_usd,
            "budget_usd": self.budget_usd,
            "over_budget": self.over_budget,
            "remaining_usd": self.remaining_usd,
            "fraction_used": self.fraction_used,
            "iterations": [
                {
                    "iter": c.iter,
                    "task_number": c.task_number,
                    "task_db_id": c.task_db_id,
                    "cost_usd": c.cost_usd,
                }
                for c in self.iterations
            ],
        }


def _task_cost(conn, task_db_id: int | None) -> float:
    """Read `tasks.cost_usd` for one task. Missing row / null id => 0.0."""
    if task_db_id is None:
        return 0.0
    row = conn.execute(
        "SELECT cost_usd FROM tasks WHERE id = ?", (task_db_id,)
    ).fetchone()
    if row is None or row["cost_usd"] is None:
        return 0.0
    return float(row["cost_usd"])


def _current_band(
    total: float, budget_usd: float | None
) -> tuple[str | None, float | None]:
    """Return ``(band_label, fraction)`` for the highest band ``total`` crosses.

    With a cap, bands are fractions of the cap (e.g. ``"80%"``) and
    ``fraction`` is ``total/budget``. Without a cap, bands are fixed USD
    thresholds (e.g. ``"$25"``) and ``fraction`` is ``None``. Returns
    ``(None, ...)`` when no band has been reached yet.
    """
    if budget_usd is not None and budget_usd > 0:
        frac = total / budget_usd
        crossed = [b for b in _FRACTION_BANDS if frac >= b]
        if not crossed:
            return None, frac
        hi = max(crossed)
        return f"{int(round(hi * 100))}%", frac
    # No cap (or zero cap) — fixed USD bands, observational.
    crossed = [b for b in _USD_BANDS if total >= b]
    if not crossed:
        return None, None
    hi = max(crossed)
    label = f"${hi:g}"
    return label, None


def _maybe_emit_threshold(
    arc_name: str,
    total: float,
    budget_usd: float | None,
    fraction: float | None,
) -> None:
    """Emit COST_THRESHOLD_CROSSED iff ``arc_name`` reached a NEW higher band.

    Best-effort: any import/emit failure is swallowed so budget accounting
    never depends on the event bus being importable.
    """
    band, band_fraction = _current_band(total, budget_usd)
    if band is None:
        return
    with _band_lock:
        prev = _last_band_by_arc.get(arc_name)
        if prev == band:
            return
        _last_band_by_arc[arc_name] = band
    try:
        from .hooks import EventTypes, bus

        bus.emit(
            EventTypes.COST_THRESHOLD_CROSSED,
            {
                "arc": arc_name,
                "band": band,
                "fraction": fraction if fraction is not None else band_fraction,
                "total_cost_usd": total,
                "budget_usd": budget_usd,
            },
        )
    except Exception:
        pass


def reset_threshold_state(arc_name: str | None = None) -> None:
    """Clear remembered band(s) so the next crossing re-emits. Test helper.

    Pass an ``arc_name`` to reset one arc, or ``None`` to reset all.
    """
    with _band_lock:
        if arc_name is None:
            _last_band_by_arc.clear()
        else:
            _last_band_by_arc.pop(arc_name, None)


def arc_cost_status(
    arc_name: str,
    iterations: list[dict],
    budget_usd: float | None = None,
    *,
    conn=None,
) -> ArcBudgetStatus:
    """Sum per-iteration task cost and evaluate the cap.

    `iterations` is the list of iteration dicts from `ArcState.iterations`;
    each has at least `iter`, `task_number`, and (optionally) `task_db_id`.
    An iteration whose task row is absent contributes 0.0.

    `over_budget` semantics: True iff a cap is set AND total >= cap.
    """
    own_conn = conn is None
    if own_conn:
        conn = db.connect()
        db.init_schema(conn)
    try:
        costs: list[IterationCost] = []
        for it in iterations:
            tid = it.get("task_db_id")
            cost = _task_cost(conn, tid)
            costs.append(IterationCost(
                iter=int(it.get("iter", 0)),
                task_number=int(it.get("task_number", 0)),
                task_db_id=tid,
                cost_usd=cost,
            ))
    finally:
        if own_conn:
            conn.close()

    total = round(sum(c.cost_usd for c in costs), 6)

    if budget_usd is None:
        _maybe_emit_threshold(arc_name, total, None, None)
        return ArcBudgetStatus(
            arc_name=arc_name,
            total_cost_usd=total,
            budget_usd=None,
            over_budget=False,
            remaining_usd=None,
            fraction_used=None,
            iterations=costs,
        )

    budget_usd = float(budget_usd)
    over = total >= budget_usd
    remaining = max(0.0, round(budget_usd - total, 6))
    fraction = (total / budget_usd) if budget_usd > 0 else None
    _maybe_emit_threshold(arc_name, total, budget_usd, fraction)
    return ArcBudgetStatus(
        arc_name=arc_name,
        total_cost_usd=total,
        budget_usd=budget_usd,
        over_budget=over,
        remaining_usd=remaining,
        fraction_used=fraction,
        iterations=costs,
    )


def render_budget_md(status: ArcBudgetStatus) -> str:
    """Human-readable BUDGET.md body for an arc-budget snapshot."""
    lines: list[str] = [f"# Arc budget — {status.arc_name}", ""]
    if status.budget_usd is None:
        lines.append(
            f"- Spent: **${status.total_cost_usd:.4f}** "
            "(observational — no cap set)"
        )
    else:
        pct = (status.fraction_used * 100.0) if status.fraction_used is not None else 0.0
        flag = " — **OVER BUDGET**" if status.over_budget else ""
        lines.append(
            f"- Spent: **${status.total_cost_usd:.4f}** / "
            f"${status.budget_usd:.4f} cap ({pct:.1f}%){flag}"
        )
        if status.remaining_usd is not None:
            lines.append(f"- Remaining: ${status.remaining_usd:.4f}")
    lines.append(f"- Updated: {datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S')}Z")
    lines.append("")
    lines.append("## Per-iteration cost")
    lines.append("")
    lines.append("| iter | task | cost (USD) |")
    lines.append("| ---: | :--- | ---------: |")
    for c in status.iterations:
        lines.append(f"| {c.iter} | task-{c.task_number:04d} | ${c.cost_usd:.4f} |")
    lines.append("")
    return "\n".join(lines)


def write_budget_files(arc_folder: Path, status: ArcBudgetStatus) -> None:
    """Write BUDGET.json + BUDGET.md into the arc folder."""
    arc_folder = Path(arc_folder)
    arc_folder.mkdir(parents=True, exist_ok=True)
    (arc_folder / "BUDGET.json").write_text(
        json.dumps(status.to_dict(), indent=2), encoding="utf-8"
    )
    (arc_folder / "BUDGET.md").write_text(
        render_budget_md(status), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Item 13 (backend) — per-task cost/token breakdown for the UI cost meter.
# ---------------------------------------------------------------------------


def task_cost_breakdown(task_db_id: int, *, conn=None) -> dict:
    """Return a per-run / per-worker cost (and token) breakdown for one task.

    Reads the per-run ``runs.cost_usd`` (joined to ``workers.name`` /
    ``workers.role``) plus the cached task total ``tasks.cost_usd``. The
    current schema has NO per-run token columns, so token counts are
    reported as ``None`` and ``tokens_available`` is ``False`` — a future
    schema that adds e.g. ``runs.input_tokens`` / ``runs.output_tokens``
    can be surfaced here without changing the shape.

    Returned shape (back-end for a UI cost meter + a future API route)::

        {
          "task_db_id": 42,
          "total_cost_usd": 3.21,        # tasks.cost_usd (cached rollup)
          "runs_cost_usd": 3.20,         # sum of runs.cost_usd
          "tokens_available": False,
          "workers": [
            {"run_id": 7, "worker_id": 3, "worker": "claude-quant",
             "role": "quant-researcher", "cost_usd": 1.10,
             "status": "done", "tokens": None},
            ...
          ],
        }

    Workers with multiple runs appear once per run (keyed by ``run_id``);
    callers wanting a per-worker rollup can group on ``worker_id``.
    """
    own_conn = conn is None
    if own_conn:
        conn = db.connect()
        db.init_schema(conn)
    try:
        rows = conn.execute(
            "SELECT r.id AS run_id, r.worker_id AS worker_id, "
            "       r.status AS status, "
            "       COALESCE(r.cost_usd, 0.0) AS cost_usd, "
            "       w.name AS worker, w.role AS role "
            "FROM runs r LEFT JOIN workers w ON w.id = r.worker_id "
            "WHERE r.task_id = ? ORDER BY r.id ASC",
            (int(task_db_id),),
        ).fetchall()
        task_row = conn.execute(
            "SELECT cost_usd FROM tasks WHERE id = ?", (int(task_db_id),)
        ).fetchone()
    finally:
        if own_conn:
            conn.close()

    workers: list[dict] = []
    runs_total = 0.0
    for r in rows:
        cost = float(r["cost_usd"] or 0.0)
        runs_total += cost
        workers.append(
            {
                "run_id": int(r["run_id"]),
                "worker_id": int(r["worker_id"]) if r["worker_id"] is not None else None,
                "worker": r["worker"],
                "role": r["role"],
                "status": r["status"],
                "cost_usd": round(cost, 6),
                "tokens": None,  # no token columns in the current schema
            }
        )

    task_total = (
        float(task_row["cost_usd"])
        if task_row is not None and task_row["cost_usd"] is not None
        else round(runs_total, 6)
    )

    return {
        "task_db_id": int(task_db_id),
        "total_cost_usd": round(task_total, 6),
        "runs_cost_usd": round(runs_total, 6),
        "tokens_available": False,
        "workers": workers,
    }
