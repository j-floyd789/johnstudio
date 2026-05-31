"""Iteration-arc primitive: run a sequence of team tasks until a stop
predicate is met, threading the prior iteration's artifact into the next
plan automatically.

This replaces the manual "write a python script per iteration" pattern.
A whole multi-iteration research arc (e.g. the DFW edge hunt) becomes
one `run_arc(...)` call.

State model
-----------
An *arc* is just a folder on disk:

    <repo>/.johnstudio/arcs/<arc-name>/
        ARC.yaml         — arc config (max_iter, predicate, plan template)
        iter-0001/       — symlink or recorded path to the team task folder
        iter-0002/
        ...
        STATE.json       — current iteration, status, last artifact path

The arc owns the predicate evaluation and decides when to terminate. Each
iteration is a normal team task — the orchestrator's existing
spawn/approve/advance/merge machinery handles the inside-iteration work.

Predicate
---------
A predicate is a Python `def predicate(artifact: dict) -> tuple[bool, str]`
that returns `(stop, reason)`. We pickle by file path (the predicate is a
.py file with a top-level `def predicate(...)`) so the arc survives
server restarts.

Cross-iteration memory
----------------------
Before spawning iteration N's planner, the arc writes
`PRIOR_ITERATIONS.md` into iteration N's task folder summarizing every
prior iteration's artifact (which candidate cleared, which didn't,
key numbers). The planner prompt builder picks this up automatically
via the same context-pack pipeline that injects project memory.

Self-modifying plans (Improvement #11)
--------------------------------------
When iteration N's predicate fails with a recognized failure signature
(e.g., "edge_found false" with a specific bottleneck), the arc spawns
the `plan-architect` role to revise the plan template before iteration
N+1 is approved. The revised template lands beside ARC.yaml and is used
for that single iteration only — the original template stays intact.

Auto-create (Improvement #10)
-----------------------------
`auto_create_arc(goal_text, project_name, ...)` skips the human-author
step entirely. It spawns the `plan-architect` role with the goal text,
the project's RUNBOOK + strategy inventory, and prior-arc STATE files;
the architect writes a TEAM_PLAN.md (used as plan template) and a
predicate.py. `create_arc` is then called with those paths.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import yaml

from . import adr, arc_budget, arc_webhook, config, db, project as project_mod, team, team_orchestrator as orch
from .hooks import EventTypes, bus


_log = logging.getLogger(__name__)


PLAN_ARCHITECT_ROLE = "plan-architect"
# Default wait time for the plan-architect to finish writing TEAM_PLAN.md
# and predicate.py during auto-create / self-modify flows.
PLAN_ARCHITECT_TIMEOUT_S = 30 * 60
PLAN_ARCHITECT_POLL_S = 10

# Role that records an ADR when an arc reaches a terminal state. Spawned
# best-effort and non-blocking — see `_spawn_adr_scribe`.
ADR_SCRIBE_ROLE = "adr-scribe"


# ---------------------------------------------------------------------------
# Config + state
# ---------------------------------------------------------------------------

@dataclass
class ArcConfig:
    """Persisted at <arc-folder>/ARC.yaml."""
    name: str
    project_name: str
    plan_template_path: str    # path to a .md TEAM_PLAN.md template (jinja-light: {{prior_summary}})
    predicate_path: str        # path to a .py file with `def predicate(artifact): -> (stop, reason)`
    artifact_glob: str         # relative path under the worktree where the artifact lands
    max_iterations: int = 10
    seed_text: str = ""        # one-line task description; templated into TASK.md
    base_iteration: int = 1    # number of the first iteration (for resuming)
    goal_text: str = ""        # north-star goal; persisted so the plan-architect can re-read it on self-modify
    webhook_url: str | None = None  # optional: POSTed when the arc reaches a terminal state
    budget_usd: float | None = None  # optional per-arc spend cap (USD); halts the arc when cumulative task cost >= cap

    @classmethod
    def from_yaml(cls, p: Path) -> "ArcConfig":
        d = yaml.safe_load(p.read_text(encoding="utf-8"))
        # Backwards compat: older arcs were saved without goal_text.
        d.setdefault("goal_text", "")
        # Tolerate any other unknown fields hand-edited into ARC.yaml so a
        # forward-compat addition doesn't break existing arcs.
        known = {f for f in cls.__dataclass_fields__}
        d = {k: v for k, v in d.items() if k in known}
        return cls(**d)

    def to_yaml(self, p: Path) -> None:
        p.write_text(yaml.safe_dump(self.__dict__, sort_keys=False), encoding="utf-8")


@dataclass
class ArcState:
    """Persisted at <arc-folder>/STATE.json."""
    name: str
    current_iter: int = 0
    status: str = "pending"   # pending|running|cleared|exhausted|over_budget|failed
    iterations: list[dict] = field(default_factory=list)  # {iter, task_db_id, task_number, artifact_path, evaluated_at, stop, reason}
    last_update: str = ""
    webhook_fired_at: str | None = None  # UTC iso8601 when the terminal-state webhook was fired (or skipped)
    webhook_ok: bool | None = None       # outcome of the webhook delivery; None if skipped
    webhook_detail: str | None = None    # short message recorded alongside the firing
    adr_scribe_spawned_at: str | None = None  # UTC iso8601 when the terminal-state adr-scribe was spawned (or skipped)

    @classmethod
    def from_json(cls, p: Path) -> "ArcState":
        if not p.exists():
            return cls(name=p.parent.name)
        d = json.loads(p.read_text(encoding="utf-8"))
        known = {f for f in cls.__dataclass_fields__}
        d = {k: v for k, v in d.items() if k in known}
        return cls(**d)

    def to_json(self, p: Path) -> None:
        p.write_text(json.dumps(self.__dict__, indent=2), encoding="utf-8")


def _arc_folder(repo: Path, arc_name: str) -> Path:
    return repo / ".johnstudio" / "arcs" / arc_name


# ---------------------------------------------------------------------------
# Predicate loading
# ---------------------------------------------------------------------------

def _load_predicate(p: Path) -> Callable[[dict], tuple[bool, str]]:
    spec = importlib.util.spec_from_file_location(f"arc_pred_{p.stem}", p)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load predicate from {p}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "predicate"):
        raise RuntimeError(f"{p}: missing top-level `def predicate(artifact)`")
    return mod.predicate


# ---------------------------------------------------------------------------
# Prior-iteration summary
# ---------------------------------------------------------------------------

def _build_prior_summary(state: ArcState, repo: Path) -> str:
    """Build the PRIOR_ITERATIONS.md content for the next iteration's planner.

    Reads every prior iteration's artifact and writes a compact summary.
    Format:
      ## iteration N (task-0007)
      - winner: <X> | edge_found: <bool>
      - notes: <one-line excerpt>
      - file: <path-to-artifact-json>
    """
    if not state.iterations:
        return ""
    lines = ["# Prior iterations in this arc\n",
             "This planner already saw the following iterations on this arc. ",
             "Build on what worked. Do NOT repeat what failed.\n"]
    for it in state.iterations:
        artifact_path = it.get("artifact_path")
        notes = ""
        winner = "?"
        edge = "?"
        if artifact_path and Path(artifact_path).exists():
            try:
                a = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
                winner = a.get("winner") or a.get("candidate_model") or "?"
                edge = str(a.get("edge_found", a.get("summary", {}).get("edge_found", "?")))
                notes = (a.get("notes") or a.get("summary", {}).get("notes") or "")[:240]
            except Exception:
                pass
        lines.append(f"## iteration {it['iter']} (task-{it['task_number']:04d})")
        lines.append(f"- result: winner={winner} · edge_found={edge}")
        if notes:
            lines.append(f"- notes: {notes}")
        lines.append(f"- artifact: `{artifact_path}`")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Plan templating
# ---------------------------------------------------------------------------

def _render_plan(template_path: Path, prior_summary: str, iter_num: int) -> str:
    """Simple `{{prior_iterations}}` and `{{iter_num}}` substitution. No jinja."""
    src = template_path.read_text(encoding="utf-8")
    return (src
            .replace("{{prior_iterations}}", prior_summary or "_(no prior iterations)_")
            .replace("{{iter_num}}", str(iter_num)))


# ---------------------------------------------------------------------------
# Failure-signature detection (Improvement #11)
# ---------------------------------------------------------------------------

# Known failure patterns. Each pattern is (substring, signature_kind).
# Matched case-insensitively against the predicate's `reason` string plus
# the artifact's `summary.notes` field. The first match wins.
_FAILURE_PATTERNS: list[tuple[str, str]] = [
    ("edge_found", "edge_not_found"),
    ("insufficient trades", "insufficient_trades"),
    ("non-positive pnl", "negative_pnl"),
    ("negative pnl", "negative_pnl"),
    ("sharpe", "sharpe_too_low"),
    ("model class", "model_class_wrong"),
    ("data source", "data_source_wrong"),
    ("calibration", "calibration_failure"),
    ("regime mismatch", "regime_mismatch"),
]


def _failure_signature(prev_iter: dict) -> dict | None:
    """Examine the previous iteration's reason and artifact for a known
    failure pattern. Return a structured dict the plan-architect can act
    on, or None if no pattern matched (fall back to default behavior).
    """
    if prev_iter.get("stop"):
        return None  # success — no revision needed
    reason = (prev_iter.get("reason") or "").lower()
    artifact_path = prev_iter.get("artifact_path")
    notes = ""
    if artifact_path and Path(artifact_path).exists():
        try:
            a = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
            notes = str(a.get("notes") or a.get("summary", {}).get("notes") or "").lower()
        except Exception:
            pass
    haystack = reason + " " + notes
    for needle, kind in _FAILURE_PATTERNS:
        if needle in haystack:
            return {
                "kind": kind,
                "matched_on": needle,
                "reason": prev_iter.get("reason"),
                "iter": prev_iter.get("iter"),
                "task_number": prev_iter.get("task_number"),
            }
    return None


# ---------------------------------------------------------------------------
# Plan-architect spawning (Improvements #10 + #11)
# ---------------------------------------------------------------------------

def _spawn_plan_architect(
    *,
    repo: Path,
    project_name: str,
    goal_text: str,
    arc_name: str,
    iter_num: int,
    out_folder: Path,
    prior_summary: str = "",
    failure_sig: dict | None = None,
    wait: bool = True,
    timeout_s: int = PLAN_ARCHITECT_TIMEOUT_S,
    poll_s: int = PLAN_ARCHITECT_POLL_S,
) -> dict:
    """Spawn the plan-architect role into `out_folder` with a custom prompt.

    Used by:
    - `auto_create_arc` (Improvement #10) — initial plan + predicate.
    - `_spawn_next_iteration` self-modify path (Improvement #11) — plan
      revision in response to a known failure pattern.

    Returns {plan_path, predicate_path, task_db_id, spawn_result}. If
    `wait=False`, the returned plan_path / predicate_path may not yet
    exist; the caller is responsible for polling.
    """
    catalog = team.load_role_catalog()
    if PLAN_ARCHITECT_ROLE not in catalog:
        raise RuntimeError(
            f"role catalog is missing {PLAN_ARCHITECT_ROLE!r}; "
            "ship seeds/roles/claude_vp/plan-architect.md first"
        )
    role = catalog[PLAN_ARCHITECT_ROLE]

    out_folder.mkdir(parents=True, exist_ok=True)
    for sub in ("prompts", "logs"):
        (out_folder / sub).mkdir(parents=True, exist_ok=True)

    # Allocate a scratch task row so worker_events and the live graph
    # surface the plan-architect run. Its task_number is allocated under
    # the host project just like any other task.
    proj = project_mod.get_project(project_name)
    if not proj:
        raise KeyError(f"project {project_name!r} not registered")
    pcfg = config.load_project_config(proj["repo_path"])
    conn = db.connect()
    try:
        db.init_schema(conn)
        cur = conn.execute(
            "SELECT COALESCE(MAX(task_number), 0) AS m FROM tasks WHERE project_id = ?",
            (proj["id"],),
        )
        task_number = int(cur.fetchone()["m"]) + 1
        scratch_title = f"plan-architect for arc {arc_name} iter {iter_num}"[:80]
        cur = conn.execute(
            """INSERT INTO tasks (project_id, task_number, title, description, status, base_branch)
               VALUES (?, ?, ?, ?, ?, ?) RETURNING id""",
            (proj["id"], task_number, scratch_title, goal_text or scratch_title,
             "planning", pcfg.base_branch),
        )
        task_db_id = int(cur.fetchone()["id"])
        conn.commit()
    finally:
        conn.close()

    # Build the prompt. We do NOT use `_build_specialist_prompt` because
    # plan-architect is not running as a team-task specialist — it's a
    # standalone arc helper.
    failure_md = ""
    if failure_sig:
        failure_md = (
            "## Failure signature from previous iteration\n"
            f"- kind: `{failure_sig.get('kind')}`\n"
            f"- matched on: `{failure_sig.get('matched_on')}`\n"
            f"- previous iteration: {failure_sig.get('iter')} "
            f"(task-{failure_sig.get('task_number'):04d})\n"
            f"- predicate reason: {failure_sig.get('reason')}\n\n"
            "Revise the plan to address THIS specific failure mode. "
            "Change ONE variable — do not redesign the arc.\n"
        )
    prior_md = prior_summary or "_(no prior iterations)_"
    prompt_md = (
        f"# Plan-architect invocation\n\n"
        f"You are the `plan-architect`. Read your role definition for the\n"
        f"output schema you must follow.\n\n"
        f"## Goal\n{goal_text or '_(no explicit goal_text provided)_'}\n\n"
        f"## Arc\n- name: `{arc_name}`\n- iteration: {iter_num}\n"
        f"- project: `{project_name}`\n- repo: `{repo}`\n\n"
        f"## Prior iterations\n{prior_md}\n\n"
        f"{failure_md}"
        f"## Required outputs (write into your CWD)\n"
        f"1. `TEAM_PLAN.md` — the orchestrator-parseable plan for this iteration.\n"
        f"2. `predicate.py` — top-level `def predicate(artifact: dict) -> tuple[bool, str]`.\n"
    )
    prompt_path = out_folder / "prompts" / f"plan_architect_iter{iter_num}.md"
    prompt_path.write_text(prompt_md, encoding="utf-8")
    log_path = out_folder / "logs" / f"plan_architect_iter{iter_num}.log"

    plan_path = out_folder / "TEAM_PLAN.md"
    predicate_path = out_folder / "predicate.py"

    spawn = orch.spawn_and_track(
        role=role, cwd=out_folder, prompt_md=prompt_md,
        prompt_path=prompt_path, log_path=log_path,
        task_db_id=task_db_id, worktree_path=None, branch_name=None,
        result_path=plan_path,
    )

    if wait:
        _wait_for_files(
            [plan_path, predicate_path],
            timeout_s=timeout_s, poll_s=poll_s,
            label=f"plan-architect iter {iter_num}",
        )

    return {
        "plan_path": plan_path,
        "predicate_path": predicate_path,
        "task_db_id": task_db_id,
        "task_number": task_number,
        "spawn_result": spawn,
    }


def _wait_for_files(paths: list[Path], *, timeout_s: int, poll_s: int, label: str) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if all(p.exists() and p.stat().st_size > 0 for p in paths):
            return
        time.sleep(poll_s)
    missing = [str(p) for p in paths if not p.exists() or p.stat().st_size == 0]
    raise TimeoutError(f"{label}: timed out waiting for {missing} after {timeout_s}s")


# ---------------------------------------------------------------------------
# Terminal-state hooks (webhook + ADR scribe)
# ---------------------------------------------------------------------------

def _fire_terminal_hooks(af: Path, *, cfg: "ArcConfig", state: "ArcState", repo: Path) -> None:
    """Run every side-effect owed when an arc reaches a terminal state.

    Today that's two things, both best-effort and independently guarded so one
    failing never blocks the other or the stepper:
    1. The existing terminal-state webhook.
    2. Spawning the `adr-scribe` to record an Architecture Decision Record.

    The bundled webhook subscriber in `arc_webhook.py` (registered at
    import via `bus.subscribe`) handles the HTTP POST when we emit
    ARC_TERMINAL below. We no longer call `fire_if_needed` directly
    here — that caused double-fire because the subscriber is sync.
    """
    _spawn_adr_scribe(repo, cfg, state, af)
    # Hook fan-out: pass the live cfg/state objects so subscribers don't
    # have to re-read STATE.json. Best-effort — bus.emit swallows
    # subscriber exceptions.
    last = state.iterations[-1] if state.iterations else {}
    bus.emit(EventTypes.ARC_TERMINAL, {
        "arc_name": cfg.name,
        "project_name": cfg.project_name,
        "status": state.status,
        "final_iter": last.get("iter", 0),
        "reason": last.get("reason", ""),
        "arc_folder": str(af),
        "cfg": cfg,
        "state": state,
    })


def _spawn_adr_scribe(repo: Path, cfg: "ArcConfig", state: "ArcState", af: Path) -> dict | None:
    """Spawn the adr-scribe role to record an ADR for a terminal arc.

    Non-blocking (`wait` is never awaited) and fully best-effort: a missing
    role, project, or DB error is logged and swallowed — recording an ADR must
    never break the arc stepper. Idempotent via `state.adr_scribe_spawned_at`,
    so repeated ticks on an already-terminal arc don't re-spawn.

    Seeds a draft ADR (`johnstudio/adr.py`) into the vault first so there is a
    record even if the scribe never runs (headless/cron contexts), then hands
    the scribe the path to flesh out.
    """
    if state.adr_scribe_spawned_at is not None:
        return None
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")

    catalog = team.load_role_catalog()
    if ADR_SCRIBE_ROLE not in catalog:
        _log.info("arc %s: adr-scribe role not in catalog; skipping ADR", cfg.name)
        state.adr_scribe_spawned_at = now
        return None
    role = catalog[ADR_SCRIBE_ROLE]

    last = state.iterations[-1] if state.iterations else {}
    reason = last.get("reason", "") or ""
    title = f"Arc {cfg.name}: {state.status}"

    # Seed a draft ADR so the decision is captured even if the scribe can't run.
    try:
        adr_path = adr.write_adr(
            repo, title,
            status="accepted",
            context=(
                f"Iteration arc `{cfg.name}` for project `{cfg.project_name}` "
                f"reached terminal status `{state.status}` after "
                f"{len(state.iterations)} iteration(s).\n\n"
                f"Goal: {cfg.goal_text or cfg.seed_text or '_(none)_'}\n\n"
                f"Predicate reason: {reason or '_(none)_'}"
            ),
            decision="_To be completed by adr-scribe._",
            consequences="_To be completed by adr-scribe._",
            tags=["arc", state.status],
        )
    except Exception as e:
        _log.warning("arc %s: failed to seed draft ADR: %s", cfg.name, e)
        state.adr_scribe_spawned_at = now
        return None

    try:
        proj = project_mod.get_project(cfg.project_name)
        if not proj:
            raise KeyError(f"project {cfg.project_name!r} not registered")
        pcfg = config.load_project_config(proj["repo_path"])
        conn = db.connect()
        try:
            db.init_schema(conn)
            cur = conn.execute(
                "SELECT COALESCE(MAX(task_number), 0) AS m FROM tasks WHERE project_id = ?",
                (proj["id"],),
            )
            task_number = int(cur.fetchone()["m"]) + 1
            scratch_title = f"adr-scribe for arc {cfg.name} ({state.status})"[:80]
            cur = conn.execute(
                """INSERT INTO tasks (project_id, task_number, title, description, status, base_branch)
                   VALUES (?, ?, ?, ?, ?, ?) RETURNING id""",
                (proj["id"], task_number, scratch_title, reason or scratch_title,
                 "running", pcfg.base_branch),
            )
            task_db_id = int(cur.fetchone()["id"])
            conn.commit()
        finally:
            conn.close()

        out_folder = af / "adr"
        for sub in ("prompts", "logs"):
            (out_folder / sub).mkdir(parents=True, exist_ok=True)

        prompt_md = (
            f"# adr-scribe invocation\n\n"
            f"An iteration arc has reached a terminal state. Record ONE ADR.\n\n"
            f"## Arc\n- name: `{cfg.name}`\n- project: `{cfg.project_name}`\n"
            f"- repo: `{repo}`\n- terminal status: `{state.status}`\n"
            f"- iterations: {len(state.iterations)}\n"
            f"- predicate reason: {reason or '_(none)_'}\n\n"
            f"## Goal\n{cfg.goal_text or cfg.seed_text or '_(none)_'}\n\n"
            f"## What to read\n"
            f"- `{af / 'STATE.json'}` — full per-iteration history.\n"
            f"- `{af / 'ARC.yaml'}` — arc config.\n"
            f"- the final iteration's artifact + any RESULT*.md it references.\n\n"
            f"## Output\n"
            f"A draft ADR already exists at:\n  `{adr_path}`\n"
            f"Overwrite it with the finished record (keep the `# ADR NNNN:` "
            f"heading and the Status/Context/Decision/Consequences shape). "
            f"Then write `DONE.md` with `status: COMPLETE`.\n"
        )
        prompt_path = out_folder / "prompts" / f"adr_scribe_{state.status}.md"
        prompt_path.write_text(prompt_md, encoding="utf-8")
        log_path = out_folder / "logs" / f"adr_scribe_{state.status}.log"

        spawn = orch.spawn_and_track(
            role=role, cwd=out_folder, prompt_md=prompt_md,
            prompt_path=prompt_path, log_path=log_path,
            task_db_id=task_db_id, worktree_path=None, branch_name=None,
            result_path=adr_path,
        )
        state.adr_scribe_spawned_at = now
        return {
            "adr_path": adr_path,
            "task_db_id": task_db_id,
            "task_number": task_number,
            "spawn_result": spawn,
        }
    except Exception as e:
        # The draft ADR is already on disk; the live scribe is a best-effort
        # enrichment. Never let its failure break the stepper.
        _log.warning("arc %s: adr-scribe spawn failed: %s", cfg.name, e)
        state.adr_scribe_spawned_at = now
        return {"adr_path": adr_path, "spawn_result": None, "error": str(e)}


# ---------------------------------------------------------------------------
# The main loop
# ---------------------------------------------------------------------------

def step_arc(repo: Path, arc_name: str, *, approve_func=None) -> dict:
    """Advance an arc by one tick.

    Behavior depends on current state:
    - If no current iteration: spawn iter-1.
    - If current iteration is still running: no-op, return status.
    - If current iteration's artifact has landed: evaluate predicate.
      - If stop: mark cleared, return.
      - If continue and current_iter < max: spawn next iteration.
      - Else: mark exhausted.

    `approve_func(task_db_id, task_number)` is the function that actually
    approves the team task — defaults to the orchestrator's
    `approve_plan_and_run`. Tests can swap in a mock.
    """
    af = _arc_folder(repo, arc_name)
    cfg = ArcConfig.from_yaml(af / "ARC.yaml")
    state = ArcState.from_json(af / "STATE.json")
    state.last_update = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    predicate = _load_predicate(Path(cfg.predicate_path))

    # Refresh the per-arc budget snapshot (observational; never raises).
    try:
        budget_status = arc_budget.arc_cost_status(
            cfg.name, state.iterations, cfg.budget_usd,
        )
        arc_budget.write_budget_files(af, budget_status)
    except Exception as e:  # budgeting must never break the stepper
        _log.warning("arc %s: budget snapshot failed: %s", cfg.name, e)
        budget_status = None

    # Status: no iterations yet — spawn iter-1.
    if not state.iterations:
        return _spawn_next_iteration(repo, cfg, state, approve_func=approve_func)

    last = state.iterations[-1]
    artifact_path = Path(last["artifact_path"])
    if not artifact_path.exists():
        # Fallback: the artifact may have been written by a `can_edit=false`
        # specialist (e.g., a synthesis architect) into the task folder
        # directly instead of into backend-developer-0's worktree.
        # Check there too before declaring the iteration still running.
        task_folder_fallback = (
            Path(last.get("task_folder", "")) / Path(artifact_path).relative_to(
                Path(artifact_path).parents[1]
            )
        ) if last.get("task_folder") else None
        if task_folder_fallback and task_folder_fallback.exists():
            artifact_path = task_folder_fallback
            last["artifact_path"] = str(artifact_path)  # remember for future ticks
        else:
            # Current iteration is still running.
            state.to_json(af / "STATE.json")
            return {"status": "waiting", "iter": last["iter"], "reason": "artifact not yet written"}

    # Evaluate predicate on the artifact.
    try:
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        stop, reason = predicate(artifact)
    except Exception as e:
        state.status = "failed"
        last["stop"] = False
        last["reason"] = f"predicate error: {e}"
        last["evaluated_at"] = state.last_update
        _fire_terminal_hooks(af, cfg=cfg, state=state, repo=repo)
        state.to_json(af / "STATE.json")
        return {"status": "failed", "iter": last["iter"], "reason": str(e)}

    last["stop"] = bool(stop)
    last["reason"] = reason
    last["evaluated_at"] = state.last_update

    # Per-iteration completion event — fires once the predicate has
    # been evaluated against the artifact, regardless of whether the
    # arc continues or hits a terminal state below.
    bus.emit(EventTypes.ARC_ITER_COMPLETE, {
        "arc_name": cfg.name,
        "project_name": cfg.project_name,
        "iter": last.get("iter"),
        "stop": bool(stop),
        "reason": reason,
        "artifact_path": str(artifact_path),
    })

    if stop:
        state.status = "cleared"
        _fire_terminal_hooks(af, cfg=cfg, state=state, repo=repo)
        state.to_json(af / "STATE.json")
        return {"status": "cleared", "iter": last["iter"], "reason": reason}

    if last["iter"] >= cfg.max_iterations:
        state.status = "exhausted"
        _fire_terminal_hooks(af, cfg=cfg, state=state, repo=repo)
        state.to_json(af / "STATE.json")
        return {"status": "exhausted", "iter": last["iter"], "reason": reason}

    # Per-arc budget gate: the in-flight iteration always finishes, but we
    # refuse to start another one once cumulative spend has hit the cap.
    if budget_status is not None and budget_status.over_budget:
        state.status = "over_budget"
        bmsg = (f"arc budget exhausted: spent ${budget_status.total_cost_usd:.4f} "
                f"of ${budget_status.budget_usd:.4f} cap")
        last["reason"] = bmsg
        _fire_terminal_hooks(af, cfg=cfg, state=state, repo=repo)
        state.to_json(af / "STATE.json")
        return {"status": "over_budget", "iter": last["iter"], "reason": bmsg}

    # Continue: spawn next iteration.
    return _spawn_next_iteration(repo, cfg, state, approve_func=approve_func)


def _spawn_next_iteration(
    repo: Path, cfg: ArcConfig, state: ArcState, *, approve_func=None,
    allow_self_modify: bool = True,
) -> dict:
    """Allocate a new team task, write its TEAM_PLAN.md, approve it.

    Self-modifying plans (Improvement #11):
    -----
    If the previous iteration failed with a recognized failure signature
    AND `allow_self_modify` is True, spawn the `plan-architect` role to
    write a one-shot revised template into `<arc-folder>/revisions/iter-N/`.
    That revised template is used for THIS iteration only — the original
    cfg.plan_template_path stays intact.

    `allow_self_modify` exists so tests can opt out of the architect spawn.
    """
    af = _arc_folder(repo, cfg.name)
    iter_num = (state.iterations[-1]["iter"] + 1) if state.iterations else cfg.base_iteration

    # Build prior-iterations summary for the next planner.
    prior_md = _build_prior_summary(state, repo)

    # Self-modify check (Improvement #11): if the prior iteration matched
    # a known failure pattern, ask plan-architect for a revised template.
    template_path = Path(cfg.plan_template_path)
    revision_info: dict | None = None
    if allow_self_modify and state.iterations:
        sig = _failure_signature(state.iterations[-1])
        if sig is not None:
            revision_folder = af / "revisions" / f"iter-{iter_num:04d}"
            try:
                arch_result = _spawn_plan_architect(
                    repo=repo, project_name=cfg.project_name,
                    goal_text=cfg.goal_text or cfg.seed_text,
                    arc_name=cfg.name, iter_num=iter_num,
                    out_folder=revision_folder,
                    prior_summary=prior_md,
                    failure_sig=sig,
                    wait=True,
                )
                template_path = arch_result["plan_path"]
                revision_info = {
                    "kind": sig["kind"],
                    "template_path": str(template_path),
                    "task_number": arch_result["task_number"],
                }
                _log.info(
                    "arc %s iter %d: self-modify via plan-architect (kind=%s)",
                    cfg.name, iter_num, sig["kind"],
                )
            except Exception as e:
                # If the architect spawn fails for any reason, fall back
                # to the default plan template — don't block the arc.
                _log.warning(
                    "arc %s iter %d: plan-architect revision failed (%s); "
                    "falling back to default template",
                    cfg.name, iter_num, e,
                )

    # Allocate task row + folder.
    proj = project_mod.get_project(cfg.project_name)
    if not proj:
        raise KeyError(f"project {cfg.project_name!r} not registered")
    pcfg = config.load_project_config(proj["repo_path"])

    conn = db.connect()
    db.init_schema(conn)
    cur = conn.execute(
        "SELECT COALESCE(MAX(task_number), 0) AS m FROM tasks WHERE project_id = ?",
        (proj["id"],),
    )
    task_number = int(cur.fetchone()["m"]) + 1
    task_text = (cfg.seed_text + f" (arc {cfg.name}, iteration {iter_num})")[:200]
    cur = conn.execute(
        """INSERT INTO tasks (project_id, task_number, title, description, status, base_branch)
           VALUES (?, ?, ?, ?, ?, ?) RETURNING id""",
        (proj["id"], task_number, task_text[:80], task_text, "planning", pcfg.base_branch),
    )
    task_db_id = int(cur.fetchone()["id"])
    conn.commit()
    conn.close()

    tf = Path(proj["repo_path"]) / ".johnstudio" / "tasks" / f"task-{task_number:04d}"
    for sub in ("prompts", "results", "diffs", "test_results", "logs", "team_notes"):
        (tf / sub).mkdir(parents=True, exist_ok=True)
    (tf / "TASK.md").write_text(f"# Task {task_number:04d}\n\n{task_text}\n", encoding="utf-8")

    # Cross-iteration memory: write the prior-iterations summary so the
    # planner sees it. Also write the rendered plan so we skip the
    # planner spawn entirely — this saves one big LLM call per iteration.
    if prior_md:
        (tf / "PRIOR_ITERATIONS.md").write_text(prior_md, encoding="utf-8")
    plan_md = _render_plan(template_path, prior_md, iter_num)
    (tf / "TEAM_PLAN.md").write_text(plan_md, encoding="utf-8")

    # Mark the team state as ready-to-approve.
    state_dict = {
        "task_db_id": task_db_id, "task_number": task_number,
        "project_name": cfg.project_name, "status": "planning",
        "started_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
        "plan_path": str(tf / "TEAM_PLAN.md"),
        "assignments": [], "arc_name": cfg.name, "arc_iteration": iter_num,
    }
    if revision_info:
        state_dict["plan_revised_by_architect"] = revision_info
    (tf / "TEAM_STATE.json").write_text(json.dumps(state_dict, indent=2), encoding="utf-8")

    # Approve and launch specialists.
    if approve_func is None:
        approve_func = orch.approve_plan_and_run
    approve_result = approve_func(task_db_id)

    # Record the iteration. The artifact path is derived from the cfg's
    # artifact_glob applied to the backend-developer's worktree (a common
    # convention; can override per-template if needed). The glob may
    # include `{iter}` which is substituted with the iteration number.
    artifact_rel = cfg.artifact_glob.replace("{iter}", str(iter_num))
    artifact_path = (
        Path(proj["repo_path"]) / ".johnstudio" / "worktrees" /
        f"task-{task_number:04d}-team-backend-developer-0" / artifact_rel
    )

    iter_record = {
        "iter": iter_num,
        "task_db_id": task_db_id,
        "task_number": task_number,
        "task_folder": str(tf),
        "artifact_path": str(artifact_path),
        "spawned_at": state.last_update,
        "approve_result": {"launched": len(approve_result.get("launched", []))},
    }
    if revision_info:
        iter_record["plan_revised_by_architect"] = revision_info
    state.iterations.append(iter_record)
    state.current_iter = iter_num
    state.status = "running"
    state.to_json(af / "STATE.json")
    return {
        "status": "spawned",
        "iter": iter_num,
        "task_number": task_number,
        "specialists_launched": len(approve_result.get("launched", [])),
        "plan_revised_by_architect": bool(revision_info),
    }


# ---------------------------------------------------------------------------
# Convenience: bootstrap a new arc
# ---------------------------------------------------------------------------

def create_arc(
    *,
    repo: Path,
    name: str,
    project_name: str,
    plan_template_path: str | Path,
    predicate_path: str | Path,
    artifact_glob: str,
    seed_text: str,
    max_iterations: int = 10,
    base_iteration: int = 1,
    goal_text: str = "",
    webhook_url: str | None = None,
    budget_usd: float | None = None,
) -> Path:
    af = _arc_folder(repo, name)
    af.mkdir(parents=True, exist_ok=True)
    cfg = ArcConfig(
        name=name, project_name=project_name,
        plan_template_path=str(plan_template_path),
        predicate_path=str(predicate_path),
        artifact_glob=artifact_glob,
        max_iterations=max_iterations,
        seed_text=seed_text,
        base_iteration=base_iteration,
        goal_text=goal_text,
        webhook_url=webhook_url,
        budget_usd=budget_usd,
    )
    cfg.to_yaml(af / "ARC.yaml")
    ArcState(name=name).to_json(af / "STATE.json")
    return af


# ---------------------------------------------------------------------------
# Auto-create: spawn plan-architect → write plan + predicate → create_arc
# (Improvement #10)
# ---------------------------------------------------------------------------

def auto_create_arc(
    *,
    repo: Path,
    name: str,
    project_name: str,
    goal_text: str,
    artifact_glob: str,
    seed_text: str = "",
    max_iterations: int = 10,
    base_iteration: int = 1,
    architect_timeout_s: int = PLAN_ARCHITECT_TIMEOUT_S,
) -> Path:
    """One-call arc bootstrap. Spawns the plan-architect role to write the
    initial TEAM_PLAN.md template and predicate.py, then registers the arc.

    The architect's outputs live at:
        <arc-folder>/architect_init/TEAM_PLAN.md
        <arc-folder>/architect_init/predicate.py

    Those paths are wired into ARC.yaml. The arc is not auto-stepped —
    the caller must call `step_arc` (or `run_arc_blocking`) afterwards.
    """
    af = _arc_folder(repo, name)
    af.mkdir(parents=True, exist_ok=True)
    init_folder = af / "architect_init"

    arch_result = _spawn_plan_architect(
        repo=repo, project_name=project_name,
        goal_text=goal_text,
        arc_name=name, iter_num=base_iteration,
        out_folder=init_folder,
        prior_summary="",
        failure_sig=None,
        wait=True,
        timeout_s=architect_timeout_s,
    )

    return create_arc(
        repo=repo, name=name, project_name=project_name,
        plan_template_path=arch_result["plan_path"],
        predicate_path=arch_result["predicate_path"],
        artifact_glob=artifact_glob,
        seed_text=seed_text or goal_text[:200],
        max_iterations=max_iterations,
        base_iteration=base_iteration,
        goal_text=goal_text,
    )


# ---------------------------------------------------------------------------
# Blocking driver: run until terminal
# ---------------------------------------------------------------------------

def run_arc_blocking(
    repo: Path, arc_name: str, *, poll_interval_s: int = 30,
) -> dict:
    """Drive an arc to terminal state in a single foreground call.

    Useful for scripts. The orchestrator's own ticker also advances
    state; this loop just keeps calling step_arc until the arc is
    cleared, exhausted, or failed.
    """
    af = _arc_folder(repo, arc_name)
    while True:
        result = step_arc(repo, arc_name)
        status = result.get("status")
        if status in ("cleared", "exhausted", "failed"):
            return result
        if status == "waiting":
            time.sleep(poll_interval_s)
            continue
        if status == "spawned":
            # Sleep a bit before checking — the team task needs time to do work.
            time.sleep(poll_interval_s)
            continue
        # Unknown status — bail.
        return result
