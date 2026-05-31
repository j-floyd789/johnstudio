"""SPARC phase gates: quality checkpoints between phased-team phases.

SPARC is the canonical five-step methodology this orchestrator uses to
decompose a focused build:

  1. Specification — what to build (constraints, acceptance criteria)
  2. Pseudocode    — the approach, in plain logic before real code
  3. Architecture  — module boundaries, data shapes, interfaces
  4. Refinement    — the implementation + tests, iterated to green
  5. Completion    — integration, docs, final verification

`dag.py` already models *which* phase runs after which (the DAG). What
it does not model is *whether a phase is actually done* — the
orchestrator there marks a phase complete the moment its specialists
exit, regardless of what they produced. A specialist can exit 0 having
written an empty DESIGN.md, and the next phase starts on sand.

A **gate** closes that hole. Each SPARC phase declares the artifacts it
must leave behind and cheap, deterministic checks over them (file
exists, non-empty, contains a required marker, minimum substance). A
phase only *passes its gate* when every check passes. The orchestrator
calls `evaluate_phase` before treating a phase as complete and refuses
to release downstream phases until the gate is green.

This module is pure: dataclasses, filesystem reads, and DAG glue built
on `dag.Phase`. It spawns nothing and touches no database, mirroring the
discipline of `dag.py` so the non-SPARC code path is unaffected.

Wiring into a plan
------------------
`sparc_phases()` returns the five `dag.Phase` objects with the linear
SPARC dependency chain already set. `default_gates()` returns the
matching gate set keyed by phase name. To run SPARC, drop the phases
into a `## Phases` block (or build a `PhasedPlan` directly) and, at each
phase boundary, call::

    report = evaluate_phase("specification", task_folder)
    if not report.passed:
        # hold the DAG; surface report.failures to the operator
        ...

Gates are intentionally shallow — they catch the *absence* of work, not
its correctness. Correctness is the reviewer's job; the gate just makes
sure there is something real for the reviewer to read.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import dag
from .dag import Phase

SPARC_PHASE_NAMES: tuple[str, ...] = (
    "specification",
    "pseudocode",
    "architecture",
    "refinement",
    "completion",
)
SPARC_DEFAULT_ARTIFACTS: dict[str, str] = {
    "specification": "SPECIFICATION.md",
    "pseudocode": "PSEUDOCODE.md",
    "architecture": "ARCHITECTURE.md",
    "refinement": "RESULT.md",
    "completion": "DONE.md",
}
GATE_KINDS = ("exists", "nonempty", "contains", "min_lines")


class GateError(ValueError):
    """Raised when a gate is misconfigured (bad kind, missing arg)."""

    pass


@dataclass
class Gate:
    name: str
    kind: str
    target: str
    arg: str | int | None = None
    description: str = ""

    def __post_init__(self):
        if self.kind not in GATE_KINDS:
            raise GateError(
                f"gate {self.name}: unknown kind {self.kind!r} "
                f"(must be one of {GATE_KINDS})"
            )
        if self.kind == "contains":
            if self.arg is None or not str(self.arg).strip():
                raise GateError(f"gate {self.name}: 'contains' needs a non-empty marker arg")
        if self.kind == "min_lines":
            try:
                int(self.arg)
            except (TypeError, ValueError):
                raise GateError(f"gate {self.name}: 'min_lines' needs an integer arg")


@dataclass
class GateResult:
    name: str
    target: str
    passed: bool
    detail: str


@dataclass
class PhaseGateReport:
    phase: str
    results: list[GateResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True only if every gate passed. An empty gate set passes
        (a phase with no declared artifacts is trusted)."""
        return all(r.passed for r in self.results)

    @property
    def failures(self) -> list[GateResult]:
        return [r for r in self.results if not r.passed]

    def to_dict(self) -> dict:
        return {
            "phase": self.phase,
            "passed": self.passed,
            "results": [
                {
                    "name": r.name,
                    "target": r.target,
                    "passed": r.passed,
                    "detail": r.detail,
                }
                for r in self.results
            ],
        }


def _non_blank_lines(text: str) -> int:
    return sum(1 for ln in text.splitlines() if ln.strip())


def evaluate_gate(gate: Gate, task_folder: Path) -> GateResult:
    """Run one gate against the artifacts in `task_folder`.

    Never raises on a missing/unreadable artifact — that is a *failed*
    gate, not an error. Misconfiguration is caught at `Gate` construction.
    """
    path = task_folder / gate.target
    exists = path.is_file()
    if gate.kind == "exists":
        return GateResult(
            gate.name,
            gate.target,
            exists,
            "artifact present" if exists else "artifact missing",
        )
    if not exists:
        return GateResult(gate.name, gate.target, False, "artifact missing")

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return GateResult(gate.name, gate.target, False, f"unreadable artifact: {e}")

    if gate.kind == "nonempty":
        ok = bool(text.strip())
        return GateResult(
            gate.name, gate.target, ok, "has content" if ok else "artifact is empty"
        )
    if gate.kind == "contains":
        needle = str(gate.arg)
        ok = needle.lower() in text.lower()
        return GateResult(
            gate.name,
            gate.target,
            ok,
            f"found {needle!r}" if ok else f"missing required marker {needle!r}",
        )
    if gate.kind == "min_lines":
        need = int(gate.arg)
        have = _non_blank_lines(text)
        ok = have >= need
        return GateResult(
            gate.name, gate.target, ok, f"{have} non-blank lines (need {need})"
        )
    raise GateError(f"gate {gate.name!r}: unhandled kind {gate.kind!r}")


def evaluate_gates(gates: list[Gate], task_folder: Path, *, phase: str) -> PhaseGateReport:
    """Evaluate every gate and bundle the results into a report."""
    results = [evaluate_gate(g, task_folder) for g in gates]
    return PhaseGateReport(phase=phase, results=results)


def default_gates() -> dict[str, list[Gate]]:
    """The standard gate set, keyed by SPARC phase name.

    Each phase must leave its canonical artifact, and that artifact must
    carry real substance — not just touch-an-empty-file. The markers and
    line minimums are deliberately low: the gate proves *something* was
    produced, leaving correctness to review.
    """
    a = SPARC_DEFAULT_ARTIFACTS
    return {
        "specification": [
            Gate("spec-exists", "exists", a["specification"], description="specification artifact was written"),
            Gate("spec-substance", "min_lines", a["specification"], 5, description="specification has real content"),
        ],
        "pseudocode": [
            Gate("pseudo-exists", "exists", a["pseudocode"], description="pseudocode artifact was written"),
            Gate("pseudo-substance", "min_lines", a["pseudocode"], 5, description="pseudocode has real content"),
        ],
        "architecture": [
            Gate("arch-exists", "exists", a["architecture"], description="architecture artifact was written"),
            Gate("arch-substance", "min_lines", a["architecture"], 5, description="architecture has real content"),
        ],
        "refinement": [
            Gate("result-exists", "exists", a["refinement"], description="RESULT.md was written"),
            Gate("result-tests", "contains", a["refinement"], "Tests run", description="RESULT.md reports a test run"),
        ],
        "completion": [
            Gate("done-exists", "exists", a["completion"], description="DONE.md was written"),
            Gate("done-complete", "contains", a["completion"], "status: COMPLETE", description="DONE.md signals completion"),
        ],
    }


def sparc_phases(assignments_by_phase: dict[str, list] | None = None) -> list[Phase]:
    """Return the five SPARC phases as `dag.Phase` objects.

    The dependency chain is linear in `SPARC_PHASE_NAMES` order, so the
    orchestrator runs Specification → … → Completion strictly in sequence.

    `assignments_by_phase` optionally maps a phase name to its list of
    `dag.Assignment`s (same objects `dag.parse_phased_plan` produces). A
    phase with no entry gets an empty assignment list — useful when the
    caller only wants the gate-bearing skeleton and fills assignments in
    later.
    """
    by_phase = assignments_by_phase or {}
    phases: list[Phase] = []
    prev: str | None = None
    for name in SPARC_PHASE_NAMES:
        phases.append(
            Phase(
                name=name,
                depends_on=[prev] if prev else [],
                assignments=list(by_phase.get(name, [])),
            )
        )
        prev = name
    return phases


def evaluate_phase(
    phase: str, task_folder: Path, *, gates: dict[str, list[Gate]] | None = None
) -> PhaseGateReport:
    """Evaluate the gates for one SPARC phase against `task_folder`.

    `gates` defaults to `default_gates()`. A phase name with no declared
    gates yields an empty (passing) report — the orchestrator trusts a
    phase that opted out of gating rather than blocking it.
    """
    table = gates if gates is not None else default_gates()
    phase_gates = table.get(phase, [])
    return evaluate_gates(phase_gates, task_folder, phase=phase)


def gate_blocks_release(
    completed: set[str],
    task_folder: Path,
    phases: list[Phase] | None = None,
    *,
    gates: dict[str, list[Gate]] | None = None,
) -> list[PhaseGateReport]:
    """Among phases the DAG marked `completed`, return the reports for any
    whose gate FAILS.

    This is the orchestrator's safety check: `dag.ready_phases` answers
    "which phases *may* run next" purely from the dependency graph, but a
    phase whose specialists exited without leaving valid artifacts must
    not be allowed to release its dependents. Feed the DAG's notion of
    completed phases here; an empty return means every completed phase
    truly passed its gate and the DAG may advance. A non-empty return
    lists exactly the phases the operator must fix before downstream work
    starts.
    """
    table = gates if gates is not None else default_gates()
    blocking: list[PhaseGateReport] = []
    for name in completed:
        report = evaluate_phase(name, task_folder, gates=table)
        if not report.passed:
            blocking.append(report)
    return blocking


def passed_phases(
    candidate: set[str] | list[str],
    task_folder: Path,
    *,
    gates: dict[str, list[Gate]] | None = None,
) -> set[str]:
    """Filter `candidate` phase names down to those whose gate passes.

    Compose with `dag.ready_phases`: a phase is genuinely complete only
    when the DAG says its deps are done *and* its own gate is green. Use
    the intersection of this and the DAG's view to drive the spawn loop.
    """
    table = gates if gates is not None else default_gates()
    return {
        name
        for name in candidate
        if evaluate_phase(name, task_folder, gates=table).passed
    }
