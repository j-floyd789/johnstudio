"""Multi-phase team tasks: DAG specs for phased TEAM_PLAN.md.

A *phased plan* extends the existing TEAM_PLAN.md format with a
sequence of named phases. Each phase has its own set of assignments
(grouped by VP, same shape as the regular `## Team` block) and a list
of dependencies on prior phases. The orchestrator launches a phase
only after every phase it depends on has completed.

This lets one team task model work that can't be expressed as a flat
fan-out:

  - design phase writes DESIGN.md and a schema
  - implementation phase reads DESIGN.md, writes code
  - verification phase reads code, runs tests + review

Without phases the planner is forced to put every specialist in one
parallel batch — which means the implementer starts with no design and
the reviewer starts with no code.

Plan format
-----------
Add a `## Phases` section to TEAM_PLAN.md containing a fenced YAML
block of the form::

    ## Phases
    ```yaml
    - name: design
      depends_on: []
      assignments:
        claude_vp:
          - role: architect
            brief: "Sketch the schema and API surface."
            output: DESIGN.md
    - name: implement
      depends_on: [design]
      assignments:
        claude_vp:
          - role: backend-developer
            brief: "Wire the endpoints from DESIGN.md."
            output: src/api/rooms.py
    - name: verify
      depends_on: [implement]
      assignments:
        codex_vp:
          - role: test-automator
            brief: "Unit + integration tests."
            output: tests/test_rooms.py
    ```

When `## Phases` is present the regular `## Team` block is optional
and ignored — every assignment must live inside a phase. A plan with
no `## Phases` is unchanged and `parse_phased_plan` returns a single
implicit phase named ``"default"`` wrapping the original assignments,
so callers can treat the two shapes uniformly.

This module is pure parsing + DAG math. It does not spawn specialists
or touch the database — `team_orchestrator` is the integration point
and can opt into phased execution by reading the same `## Phases`
block.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import team
from .team import Assignment, PlanError, Role, TeamPlan, VPs


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

DEFAULT_PHASE = "default"


@dataclass
class Phase:
    """One named phase in a phased plan."""
    name: str
    depends_on: list[str] = field(default_factory=list)
    assignments: list[Assignment] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "depends_on": list(self.depends_on),
            "assignments": [a.to_dict() for a in self.assignments],
        }


@dataclass
class PhasedPlan:
    """A team plan decomposed into phases.

    Carries the same surface as `TeamPlan` so downstream code that only
    needs `summary` / `acceptance_criteria` can swap the two freely.
    `assignments` is the flat union of every phase's assignments, in
    topological order.
    """
    summary: str
    phases: list[Phase] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    source_path: Path | None = None

    @property
    def assignments(self) -> list[Assignment]:
        out: list[Assignment] = []
        for ph in self.phases:
            out.extend(ph.assignments)
        return out

    def phase(self, name: str) -> Phase:
        for ph in self.phases:
            if ph.name == name:
                return ph
        raise KeyError(f"no phase named {name!r}")

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "phases": [ph.to_dict() for ph in self.phases],
            "acceptance_criteria": list(self.acceptance_criteria),
            "source_path": str(self.source_path) if self.source_path else None,
        }


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_PHASES_SECTION_NAMES = ("phases", "dag", "phase plan")


def _extract_phases_yaml(md: str) -> str | None:
    """Return the YAML body inside `## Phases` (or aliases), or None."""
    sections = team._split_sections(md)  # noqa: SLF001 — same module family
    for name in _PHASES_SECTION_NAMES:
        body = sections.get(name)
        if body:
            m = team._YAML_FENCE_RE.search(body)  # noqa: SLF001
            if m:
                return m.group(1)
    return None


def _build_assignment(
    raw: dict, vp: str, *, catalog: dict[str, Role] | None, where: str,
) -> Assignment:
    for k in ("role", "brief", "output"):
        if k not in raw:
            raise PlanError(f"{where}: missing '{k}'")
    a = Assignment(
        role=str(raw["role"]).strip(),
        vp=vp,  # type: ignore[arg-type]
        brief=str(raw["brief"]).strip(),
        output=str(raw["output"]).strip(),
    )
    if catalog is not None:
        if a.role not in catalog:
            raise PlanError(
                f"{where}: role {a.role!r} is not in the catalog. "
                f"Known: {sorted(catalog)}"
            )
        if catalog[a.role].vp != a.vp:
            raise PlanError(
                f"{where}: role {a.role!r} lives in catalog vp "
                f"{catalog[a.role].vp!r}, not {a.vp!r}"
            )
    return a


def parse_phased_plan(
    md: str,
    *,
    catalog: dict[str, Role] | None = None,
    source_path: Path | None = None,
) -> PhasedPlan:
    """Parse TEAM_PLAN.md with optional `## Phases` block.

    If `## Phases` is present:
    - Every assignment lives under a phase.
    - Phase names are unique; `depends_on` must reference earlier-named
      phases (validated via cycle check below).
    - The flat `## Team` block (if any) is ignored — phased plans own
      the assignment list.

    If `## Phases` is absent:
    - Delegate to `team.parse_team_plan` and wrap the result in a
      single phase named ``"default"`` with no dependencies. This lets
      every caller treat a `PhasedPlan` as the canonical shape.

    Validation enforced here:
    - Phase output paths globally unique (a duplicate would race when
      two phases finish out of order).
    - DAG must be acyclic.
    - Every `depends_on` entry must name a real phase.
    """
    phases_yaml = _extract_phases_yaml(md)
    if phases_yaml is None:
        flat = team.parse_team_plan(md, catalog=catalog, source_path=source_path)
        return PhasedPlan(
            summary=flat.summary,
            phases=[Phase(
                name=DEFAULT_PHASE, depends_on=[], assignments=list(flat.assignments),
            )],
            acceptance_criteria=list(flat.acceptance_criteria),
            source_path=source_path,
        )

    # Summary still required.
    sections = team._split_sections(md)  # noqa: SLF001
    summary = sections.get("summary", "").strip()
    if not summary:
        raise PlanError("plan is missing a '## Summary' section")

    try:
        raw = yaml.safe_load(phases_yaml) or []
    except yaml.YAMLError as e:
        raise PlanError(f"phases YAML failed to parse: {e}")
    if not isinstance(raw, list):
        raise PlanError("phases YAML must be a list of phase mappings")

    phases: list[Phase] = []
    seen_names: set[str] = set()
    seen_outputs: set[str] = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise PlanError(f"phases[{i}]: expected a mapping, got {type(entry).__name__}")
        name = str(entry.get("name") or "").strip()
        if not name:
            raise PlanError(f"phases[{i}]: missing 'name'")
        if name in seen_names:
            raise PlanError(f"phases[{i}]: duplicate phase name {name!r}")
        seen_names.add(name)

        deps_raw = entry.get("depends_on") or []
        if not isinstance(deps_raw, list):
            raise PlanError(f"phases[{name}]: 'depends_on' must be a list")
        depends_on = [str(d).strip() for d in deps_raw if str(d).strip()]

        assignments_raw = entry.get("assignments") or {}
        if not isinstance(assignments_raw, dict):
            raise PlanError(
                f"phases[{name}]: 'assignments' must be a mapping {{vp: [...]}}"
            )
        phase_assignments: list[Assignment] = []
        for vp, entries in assignments_raw.items():
            if vp not in VPs:
                raise PlanError(
                    f"phases[{name}]: unknown vp {vp!r} (must be one of {VPs})"
                )
            if not isinstance(entries, list):
                raise PlanError(
                    f"phases[{name}].{vp}: expected a list of assignments"
                )
            for j, raw_a in enumerate(entries):
                if not isinstance(raw_a, dict):
                    raise PlanError(
                        f"phases[{name}].{vp}[{j}]: expected a mapping"
                    )
                a = _build_assignment(
                    raw_a, vp,
                    catalog=catalog,
                    where=f"phases[{name}].{vp}[{j}]",
                )
                if a.output in seen_outputs:
                    raise PlanError(
                        f"phases[{name}].{vp}[{j}]: output {a.output!r} "
                        "collides with an earlier assignment"
                    )
                seen_outputs.add(a.output)
                phase_assignments.append(a)
        phases.append(Phase(
            name=name, depends_on=depends_on, assignments=phase_assignments,
        ))

    if not phases:
        raise PlanError("phases YAML has no phases")

    # Validate the DAG: deps reference real phases, no cycles.
    validate_dag(phases)

    # Acceptance criteria (same shape as flat plans).
    ac_md = sections.get("acceptance criteria", "")
    acceptance = [
        line.lstrip("-* ").strip()
        for line in ac_md.splitlines()
        if line.strip().startswith(("-", "*"))
    ]

    return PhasedPlan(
        summary=summary,
        phases=phases,
        acceptance_criteria=acceptance,
        source_path=source_path,
    )


# ---------------------------------------------------------------------------
# DAG math
# ---------------------------------------------------------------------------

class DagError(ValueError):
    """Raised on cycles or dangling references."""


def validate_dag(phases: list[Phase]) -> None:
    """Raise DagError if any `depends_on` names a missing phase, or if
    the phase graph has a cycle.

    Self-edges (a phase listing itself in depends_on) count as cycles.
    """
    names = {ph.name for ph in phases}
    for ph in phases:
        for d in ph.depends_on:
            if d not in names:
                raise DagError(
                    f"phase {ph.name!r} depends on unknown phase {d!r}"
                )
            if d == ph.name:
                raise DagError(f"phase {ph.name!r} depends on itself")

    # Kahn's algorithm. If we can't drain every node, there's a cycle.
    in_deg = {ph.name: len(ph.depends_on) for ph in phases}
    out_edges: dict[str, list[str]] = {ph.name: [] for ph in phases}
    for ph in phases:
        for d in ph.depends_on:
            out_edges[d].append(ph.name)

    ready = [n for n, deg in in_deg.items() if deg == 0]
    drained: list[str] = []
    while ready:
        n = ready.pop(0)
        drained.append(n)
        for m in out_edges[n]:
            in_deg[m] -= 1
            if in_deg[m] == 0:
                ready.append(m)
    if len(drained) != len(phases):
        stuck = sorted(n for n, deg in in_deg.items() if deg > 0)
        raise DagError(f"phase DAG has a cycle involving {stuck}")


def topological_order(phases: list[Phase]) -> list[str]:
    """Return phase names in a valid execution order.

    Ties are broken by the order phases appear in the source list, so
    the output is deterministic for the same input. Raises DagError on
    a cycle.
    """
    validate_dag(phases)
    in_deg = {ph.name: len(ph.depends_on) for ph in phases}
    name_order = {ph.name: i for i, ph in enumerate(phases)}
    out_edges: dict[str, list[str]] = {ph.name: [] for ph in phases}
    for ph in phases:
        for d in ph.depends_on:
            out_edges[d].append(ph.name)

    ready = sorted([n for n, deg in in_deg.items() if deg == 0],
                   key=lambda n: name_order[n])
    order: list[str] = []
    while ready:
        n = ready.pop(0)
        order.append(n)
        new_ready: list[str] = []
        for m in out_edges[n]:
            in_deg[m] -= 1
            if in_deg[m] == 0:
                new_ready.append(m)
        # Merge while keeping source-order tiebreak.
        ready = sorted(ready + new_ready, key=lambda n: name_order[n])
    return order


def ready_phases(completed: set[str], phases: list[Phase]) -> list[Phase]:
    """Return phases whose dependencies are all in `completed` AND that
    are not themselves completed. Order matches the source list.

    A phase with no dependencies is always ready until it completes.
    Use this to drive the orchestrator's spawn loop: after each phase
    finishes, call `ready_phases(done, plan.phases)` and launch any
    phase returned that hasn't been launched yet.
    """
    done = set(completed)
    out: list[Phase] = []
    for ph in phases:
        if ph.name in done:
            continue
        if all(d in done for d in ph.depends_on):
            out.append(ph)
    return out


# ---------------------------------------------------------------------------
# Execution state
# ---------------------------------------------------------------------------

@dataclass
class PhaseState:
    """Persisted view of one phase's progress.

    Lives inside the task folder's `PHASES_STATE.json` alongside the
    existing TEAM_STATE.json. Keeping it separate means the
    non-phased code path is unaffected — old tasks just don't have
    this file.
    """
    name: str
    status: str = "pending"   # pending | running | complete | failed
    launched_at: str | None = None
    completed_at: str | None = None
    assignments: list[dict] = field(default_factory=list)
    # Failure detail when status == "failed"; an operator-facing string.
    reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "launched_at": self.launched_at,
            "completed_at": self.completed_at,
            "assignments": list(self.assignments),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PhaseState":
        return cls(
            name=str(d["name"]),
            status=str(d.get("status") or "pending"),
            launched_at=d.get("launched_at"),
            completed_at=d.get("completed_at"),
            assignments=list(d.get("assignments") or []),
            reason=d.get("reason"),
        )


@dataclass
class PhasedExecutionState:
    """Container for per-phase state plus the plan summary."""
    phases: list[PhaseState] = field(default_factory=list)

    def by_name(self) -> dict[str, PhaseState]:
        return {ph.name: ph for ph in self.phases}

    def completed_names(self) -> set[str]:
        return {ph.name for ph in self.phases if ph.status == "complete"}

    def is_terminal(self) -> bool:
        """True if every phase is in a terminal state (complete or failed)."""
        if not self.phases:
            return False
        return all(ph.status in ("complete", "failed") for ph in self.phases)

    def to_dict(self) -> dict:
        return {"phases": [ph.to_dict() for ph in self.phases]}

    @classmethod
    def from_dict(cls, d: dict) -> "PhasedExecutionState":
        return cls(phases=[PhaseState.from_dict(p) for p in (d.get("phases") or [])])

    @classmethod
    def initial(cls, plan: PhasedPlan) -> "PhasedExecutionState":
        """Build a fresh state with one PhaseState per plan phase."""
        return cls(phases=[PhaseState(name=ph.name) for ph in plan.phases])


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

PHASES_STATE_FILENAME = "PHASES_STATE.json"


def write_state(task_folder: Path, state: PhasedExecutionState) -> Path:
    """Persist state to `<task_folder>/PHASES_STATE.json`. Returns the path."""
    import json
    p = task_folder / PHASES_STATE_FILENAME
    p.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
    return p


def read_state(task_folder: Path) -> PhasedExecutionState | None:
    """Read PHASES_STATE.json if present; return None if absent."""
    import json
    p = task_folder / PHASES_STATE_FILENAME
    if not p.exists():
        return None
    return PhasedExecutionState.from_dict(json.loads(p.read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# Convenience: compatibility with `TeamPlan` consumers
# ---------------------------------------------------------------------------

def to_team_plan(plan: PhasedPlan) -> TeamPlan:
    """Flatten a PhasedPlan into a TeamPlan in topological order.

    Useful for code paths that expect the legacy shape and don't need
    phase awareness — they get the full assignment list in an order
    that respects dependencies. New code should consume PhasedPlan
    directly so phase boundaries aren't lost.
    """
    order = topological_order(plan.phases)
    by_name = {ph.name: ph for ph in plan.phases}
    assignments: list[Assignment] = []
    for name in order:
        assignments.extend(by_name[name].assignments)
    return TeamPlan(
        summary=plan.summary,
        assignments=assignments,
        cross_review=[],
        acceptance_criteria=list(plan.acceptance_criteria),
        source_path=plan.source_path,
    )
