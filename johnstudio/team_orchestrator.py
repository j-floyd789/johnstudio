"""Team-mode orchestrator: planner → specialists.

Glue between `team.py` (catalog + plan parser) and the existing worker
infrastructure (workers.make_worker, git_worktree, worker_events).

Flow (RFC 0001):

1. `begin_team_task` inserts a task row and spawns the lead-planner
   (Gemini VP). The planner gets a custom context pack that includes the
   role catalog summaries + the user's task + project memory.
2. The planner writes `TEAM_PLAN.md` into the task folder. `current_state`
   reflects this and the chain pauses at a human-approval gate (mirrors
   the chain-mode RFC approval gate).
3. `approve_plan_and_run` parses the plan, builds a per-assignment
   context pack, spawns each specialist in parallel under its VP, and
   starts a worker_events tailer for each.
4. The graph view (already SSE-driven) renders the new tasks/workers
   live.

We deliberately stay file-artifact-driven (TEAM_PLAN.md, TEAM_STATE.json
in the task folder) rather than adding new DB tables — same shape as
chain mode, easier to inspect.
"""
from __future__ import annotations

import json
import logging
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config, context_builder, db, memory, project as project_mod, rag_memory, team
from . import chain as chain_mod
from . import git_worktree as gw
from . import spawner, worker_events, workers
from .models import WorkerConfig


_log = logging.getLogger(__name__)

PLANNER_ROLE = "lead-planner"
CRITIC_ROLE = "product-manager"   # used for plan-critique pass
DEBUGGER_ROLE = "debugger"        # used for tests-as-signal failure analysis
MAX_REVISE_ROUNDS = 2             # mirrors chain.DEFAULT_MAX_REVISE_ROUNDS
# Cap concurrent workers PER PROVIDER. Spawning many same-provider workers at
# once (e.g. 3 claude_vp roles) hammers that provider's shared rate limit (the
# Claude sub's five_hour cap), which throttles a worker into spuriously failing.
# We launch up to this many per provider, then wait for a slot before the next.
MAX_CONCURRENT_PER_PROVIDER = 2
# Order to try the planner across providers when one is unavailable (e.g. its
# quota is exhausted). One exhausted provider must not stall the whole loop.
PLANNER_FALLBACK_PROVIDERS = ["gemini", "claude", "codex"]

# Per-task advance lock: the 5s ticker, manual POST /advance, and a slow tick
# overlapping the next can all call advance_team_task concurrently and each pass
# the file-state guards before the other commits → double-spawned planners /
# reviewers / debuggers. Serialize per task.
_TASK_LOCKS: dict[int, "threading.Lock"] = {}
_TASK_LOCKS_GUARD = threading.Lock()


def _task_lock(task_db_id: int) -> "threading.Lock":
    with _TASK_LOCKS_GUARD:
        lk = _TASK_LOCKS.get(task_db_id)
        if lk is None:
            lk = _TASK_LOCKS[task_db_id] = threading.Lock()
        return lk


def _planner_quota_stalled(tf) -> bool:
    """True if the planner's log shows a HARD provider-quota error and no plan
    was produced — i.e. the worker is alive but stuck retrying the 429 forever."""
    if (tf / "TEAM_PLAN.md").exists():
        return False
    markers = ("quota will reset", "terminalquotaerror", "quota_exhausted",
               "exhausted your capacity", "insufficient_quota")
    # Check only the NEWEST planner log (the currently-active attempt) so a prior
    # provider's quota error doesn't make the new provider look stalled.
    logs = sorted(
        (tf / "logs").glob("planning_lead-planner*.log"),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    if not logs:
        return False
    try:
        txt = logs[0].read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False
    return any(m in txt for m in markers)


def _refire_planner_if_needed(task_db_id, proj_name, repo, task_number, tf) -> bool:
    """If a team task is still planning, has no TEAM_PLAN.md, and ALL its planner
    runs have terminated (e.g. the planner's provider quota is exhausted), re-fire
    the planner on the next provider in PLANNER_FALLBACK_PROVIDERS. Idempotent:
    only acts when no planner is alive, and only tries each provider once.
    Returns True if it re-fired a planner.
    """
    if (tf / "TEAM_PLAN.md").exists():
        return False
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT r.id, r.pid, r.status FROM runs r JOIN workers w ON w.id=r.worker_id "
            "WHERE r.task_id=? AND w.name=? ORDER BY r.id",
            (task_db_id, PLANNER_ROLE),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return False
    alive = [r for r in rows
             if r["status"] in ("launched", "running", "retrying")
             and r["pid"] and _pid_alive(int(r["pid"]))]
    if alive:
        # A worker that hit a HARD quota stays ALIVE forever retrying the 429
        # internally (gemini-cli backs off up to 16h) and produces nothing — so
        # "alive" is not "working". If the planner is quota-stalled, kill it so we
        # can fall back; a genuinely-working planner we leave alone.
        if not _planner_quota_stalled(tf):
            return False
        for r in alive:
            _kill_pid(r["pid"])
            conn = db.connect()
            try:
                conn.execute(
                    "UPDATE runs SET status='stopped' WHERE id=? AND status IN ('launched','running','retrying')",
                    (r["id"],),
                )
                conn.commit()
            finally:
                conn.close()

    catalog = team.load_role_catalog()
    if PLANNER_ROLE not in catalog:
        return False
    planner_role = catalog[PLANNER_ROLE]
    state = _read_team_state(tf)
    tried = state.get("planner_providers_tried") or [planner_role.provider]
    next_provider = next((p for p in PLANNER_FALLBACK_PROVIDERS if p not in tried), None)
    if next_provider is None:
        # Every provider has been tried — give up cleanly with a clear status.
        state["status"] = "planning_failed"
        state["plan_error"] = "all planner providers unavailable (quota exhausted?)"
        _write_team_state(tf, state)
        return False

    # Re-spawn the planner on the fallback provider (drop the role's model so the
    # new provider uses its own default rather than e.g. gemini-2.5-pro).
    alt_role = replace(planner_role, provider=next_provider, model="")
    prompt_md = _build_planner_prompt(
        role=planner_role, catalog=catalog,
        task_text=_task_description(task_db_id), task_number=task_number,
        project_name=proj_name, repo=repo,
    )
    prompt_path = tf / "prompts" / f"planning_{PLANNER_ROLE}_{next_provider}.md"
    prompt_path.write_text(prompt_md, encoding="utf-8")
    try:
        spawn_and_track(
            # Per-provider log so a prior provider's quota error doesn't make the
            # new provider look stalled (the stall check reads the newest log).
            role=alt_role, cwd=tf, prompt_md=prompt_md,
            prompt_path=prompt_path,
            log_path=tf / "logs" / f"planning_{PLANNER_ROLE}_{next_provider}.log",
            task_db_id=task_db_id, worktree_path=None, branch_name=None,
            result_path=tf / "TEAM_PLAN.md",
        )
    except Exception:
        _log.exception("planner fallback to %s failed for task %s", next_provider, task_db_id)
        return False
    state["planner_providers_tried"] = tried + [next_provider]
    _write_team_state(tf, state)
    _log.warning("planner re-fired on fallback provider %r for task %s", next_provider, task_db_id)
    return True


# ---------------------------------------------------------------------------
# Top-level lifecycle
# ---------------------------------------------------------------------------

def begin_team_task(
    *, project_name: str, task_text: str, budget_usd: float | None = None,
) -> dict:
    """Insert the task row, scaffold the task folder, spawn the planner.

    `budget_usd`, if set, is a hard cap on the rolling cost across every
    worker the team spawns. The cost-tracker rolls per-turn USD into
    `tasks.cost_usd`; `check_budget` reports when the cap is hit; the
    next attempted spawn refuses.

    Returns {task_db_id, task_number, planner_pid, planner_log_jsonl}.
    """
    proj = project_mod.get_project(project_name)
    if not proj:
        raise KeyError(f"project not registered: {project_name}")
    pcfg = config.load_project_config(proj["repo_path"])
    repo = Path(proj["repo_path"])

    # 1. Allocate task row + task folder.
    conn = db.connect()
    db.init_schema(conn)
    cur = conn.execute(
        "SELECT COALESCE(MAX(task_number), 0) AS m FROM tasks WHERE project_id = ?",
        (proj["id"],),
    )
    task_number = int(cur.fetchone()["m"]) + 1
    cur = conn.execute(
        """INSERT INTO tasks (project_id, task_number, title, description, status, base_branch, budget_usd)
           VALUES (?,?,?,?,?,?, ?) RETURNING id""",
        (proj["id"], task_number, task_text[:80], task_text, "planning", pcfg.base_branch, budget_usd),
    )
    task_db_id = int(cur.fetchone()["id"])
    conn.commit()
    conn.close()

    tf = _task_folder(repo, task_number)
    for sub in ("prompts", "results", "diffs", "test_results", "logs", "team_notes"):
        (tf / sub).mkdir(parents=True, exist_ok=True)
    (tf / "TASK.md").write_text(
        f"# Task {task_number:04d}\n\n{task_text}\n",
        encoding="utf-8",
    )

    # 2. Persist team state (status='planning').
    _write_team_state(tf, {
        "task_db_id": task_db_id,
        "task_number": task_number,
        "project_name": project_name,
        "status": "planning",
        "started_at": _now(),
        "plan_path": str(tf / "TEAM_PLAN.md"),
        "assignments": [],
    })

    # 3. Spawn the planner.
    catalog = team.load_role_catalog()
    if PLANNER_ROLE not in catalog:
        raise RuntimeError(
            f"role catalog is missing {PLANNER_ROLE!r}; cannot run team mode"
        )
    planner_role = catalog[PLANNER_ROLE]
    prompt_md = _build_planner_prompt(
        role=planner_role, catalog=catalog,
        task_text=task_text, task_number=task_number,
        project_name=project_name, repo=repo,
    )
    prompt_path = tf / "prompts" / f"planning_{PLANNER_ROLE}.md"
    prompt_path.write_text(prompt_md, encoding="utf-8")
    log_path = tf / "logs" / f"planning_{PLANNER_ROLE}.log"

    spawn = spawn_and_track(
        role=planner_role, cwd=tf, prompt_md=prompt_md,
        prompt_path=prompt_path, log_path=log_path,
        task_db_id=task_db_id, worktree_path=None, branch_name=None,
        result_path=tf / "TEAM_PLAN.md",
    )

    return {
        "task_db_id": task_db_id,
        "task_number": task_number,
        "project_name": project_name,
        "status": "planning",
        "planner": planner_role.name,
        "planner_pid": spawn.pid,
        "planner_run_id": spawn.run_id,
        "task_folder": str(tf),
    }


def get_team_state(task_db_id: int) -> dict:
    """Read the team state from disk: status, plan (if available), assignments."""
    proj_info = _project_for_task(task_db_id)
    if not proj_info:
        raise KeyError(f"task {task_db_id} not found")
    proj_name, repo, task_number = proj_info
    tf = _task_folder(repo, task_number)
    state = _read_team_state(tf)
    state.setdefault("task_db_id", task_db_id)
    state.setdefault("task_number", task_number)
    state.setdefault("project_name", proj_name)

    plan_path = tf / "TEAM_PLAN.md"
    state["plan_exists"] = plan_path.exists()
    state["plan_path"] = str(plan_path)
    if plan_path.exists():
        try:
            catalog = team.load_role_catalog()
            plan = team.parse_team_plan(plan_path.read_text(encoding="utf-8"), catalog=catalog)
            state["plan"] = plan.to_dict()
            state["plan_valid"] = True
        except team.PlanError as e:
            state["plan_valid"] = False
            state["plan_error"] = str(e)
    return state


def validate_plan_for_task(task_db_id: int) -> dict:
    """Parse + validate TEAM_PLAN.md the moment it's read, BEFORE any
    long-running spawn state is entered.

    Returns {"ok": True, "plan": <TeamPlan>} on success or
    {"ok": False, "error": "...", "stage": "..."} on any validation
    failure. Never raises for a *plan* problem (only for a missing task /
    missing plan file, which the caller maps to HTTP 409/404).

    This is the early gate for Item 2: validating role↔VP pairs up front
    means an invalid plan is caught while the task is still in 'planning'
    (or whatever non-running state it's in), so we never wedge the SQL
    row at 'running' and force every retry to return already_running.
    """
    proj_info = _project_for_task(task_db_id)
    if not proj_info:
        raise KeyError(f"task {task_db_id} not found")
    _proj_name, repo, task_number = proj_info
    tf = _task_folder(repo, task_number)

    plan_path = tf / "TEAM_PLAN.md"
    if not plan_path.exists():
        raise RuntimeError(f"planner has not written {plan_path} yet")

    catalog = team.load_role_catalog()
    try:
        # parse_team_plan validates: every role exists in the catalog AND
        # each assignment's vp matches the role's catalog vp (the role↔VP
        # pairing check) AND no output collisions.
        plan = team.parse_team_plan(
            plan_path.read_text(encoding="utf-8"), catalog=catalog,
            source_path=plan_path,
        )
    except team.PlanError as e:
        return {"ok": False, "error": str(e), "stage": "parse"}

    # Augment with deterministic standing rules (idempotent; only adds
    # roles not already in the plan). Re-validate the role↔VP pairing of
    # everything the rules added too, so a misconfigured standing rule
    # can't slip an invalid pair past the gate.
    plan = team.apply_standing_rules(
        plan, task_text=_task_description(task_db_id), catalog=catalog,
    )
    for a in plan.assignments:
        role = catalog.get(a.role)
        if role is None:
            return {"ok": False, "stage": "role-catalog",
                    "error": f"role {a.role!r} (vp {a.vp!r}) is not in the role catalog"}
        if role.vp != a.vp:
            return {"ok": False, "stage": "role-vp",
                    "error": (f"role {a.role!r} is assigned under vp {a.vp!r} but the "
                              f"catalog places it under {role.vp!r} (provider "
                              f"{role.provider!r}) — fix the role↔VP pairing")}
    return {"ok": True, "plan": plan}


def _mark_needs_replan(task_db_id: int, tf: Path, error: str, stage: str) -> dict:
    """Move the task out of any spawn/planning limbo into 'needs_replan'
    and surface the validation error.

    Crucially this does NOT leave the row at 'running': the planner can be
    re-issued (see `replan_team_task`) without every approve returning
    `already_running: true`.
    """
    _set_task_status(task_db_id, "needs_replan")
    state = _read_team_state(tf)
    state["status"] = "needs_replan"
    state["plan_valid"] = False
    state["plan_error"] = error
    state["plan_error_stage"] = stage
    state["needs_replan_at"] = _now()
    _write_team_state(tf, state)
    try:
        from .hooks import bus, EventTypes
        bus.emit(EventTypes.TASK_TRANSITIONED, {
            "task_id": task_db_id, "status": "needs_replan",
            "reason": "plan_invalid", "error": error, "stage": stage,
        })
    except Exception:
        _log.exception("failed to emit needs_replan hook for task %s", task_db_id)
    return {"plan_invalid": True, "error": error, "stage": stage, "status": "needs_replan"}


def approve_plan_and_run(task_db_id: int) -> dict:
    """Validate TEAM_PLAN.md, then kick off the specialist spawn in the
    BACKGROUND and return immediately (HTTP 202).

    Two distinct guards:

    1. **Validity gate (Item 2).** The plan is parsed + role↔VP-validated
       *before* the SQL gate flips the row to 'running'. An invalid plan
       sends the task to 'needs_replan' (not 'running'), so the wedge
       that made every retry return `already_running` can't happen, and
       the planner can be re-issued.

    2. **Idempotency / no-double-spawn gate (Item 3).** A single atomic
       `UPDATE … status='running' WHERE status='planning'` lets exactly
       one caller win; the spawn runs on a daemon thread, so two
       overlapping approves can never both spawn the team. Losers get
       `already_running`. The endpoint returns 202 with the task id while
       the spawn proceeds and emits per-specialist SSE progress.
    """
    proj_info = _project_for_task(task_db_id)
    if not proj_info:
        raise KeyError(f"task {task_db_id} not found")
    proj_name, repo, task_number = proj_info
    tf = _task_folder(repo, task_number)

    # Pre-spawn budget guard: refuse the whole approve flow if the task
    # has already exceeded its budget during planning (e.g. an overpriced
    # planner-critic cycle).
    bs = check_budget(task_db_id)
    if bs.get("over_budget"):
        return {"refused": True, "reason": "budget_exceeded", "cost": bs}

    # --- Item 2: validate BEFORE entering the long-running spawn state. ---
    # This runs while the row is still 'planning'. If the plan is bad we
    # transition to 'needs_replan' and never touch 'running', so the task
    # is not wedged and the planner can be re-issued.
    validation = validate_plan_for_task(task_db_id)
    if not validation.get("ok"):
        return _mark_needs_replan(
            task_db_id, tf,
            error=validation.get("error", "invalid plan"),
            stage=validation.get("stage", "validate"),
        )
    plan = validation["plan"]

    # --- Item 3: atomic single-winner gate, then spawn in background. ---
    conn = db.connect()
    try:
        cur = conn.execute(
            "UPDATE tasks SET status = 'running' WHERE id = ? AND status = 'planning'",
            (task_db_id,),
        )
        won_gate = cur.rowcount == 1
        conn.commit()
    finally:
        conn.close()

    state = _read_team_state(tf)
    if not won_gate:
        # Another approve already flipped the row → it owns the spawn.
        # Never spawn a second team.
        return {"already_running": True, "status": state.get("status", "running"),
                "state": state}

    # Record the approved plan + 'spawning' substate so the UI shows
    # progress immediately, before the first specialist lands.
    state["status"] = "running"
    state["spawn_state"] = "spawning"
    state["approved_at"] = _now()
    state["plan"] = plan.to_dict()
    state["assignments"] = []
    state["expected_specialists"] = len(plan.assignments)
    _write_team_state(tf, state)

    try:
        from .hooks import bus, EventTypes
        bus.emit(EventTypes.PLAN_APPROVED, {
            "task_id": task_db_id, "task_number": task_number,
            "project_name": proj_name, "specialists": len(plan.assignments),
        })
    except Exception:
        _log.exception("failed to emit plan.approved hook for task %s", task_db_id)

    # Launch the spawn on a daemon thread and return 202 immediately. The
    # SQL gate above guarantees only this caller reaches here, so the
    # background spawn can never be double-started.
    t = threading.Thread(
        target=_spawn_team_background,
        kwargs=dict(
            task_db_id=task_db_id, repo=repo, tf=tf,
            task_number=task_number, proj_name=proj_name, plan=plan,
        ),
        name=f"team-spawn-{task_db_id}", daemon=True,
    )
    t.start()

    return {
        "accepted": True,
        "task_db_id": task_db_id,
        "task_number": task_number,
        "status": "running",
        "spawn_state": "spawning",
        "expected_specialists": len(plan.assignments),
    }


def _spawn_team_background(
    *, task_db_id: int, repo: Path, tf: Path, task_number: int,
    proj_name: str, plan: "team.TeamPlan",
) -> None:
    """Spawn every named specialist. Runs on a daemon thread off the
    approve hot path (Item 3). Emits one WORKER_SPAWNED hook per
    specialist so the SSE UI can follow progress, and rolls the SQL gate
    back to 'planning' if the loop fails partway so the user can retry
    without hitting `already_running`.
    """
    from .hooks import bus, EventTypes
    pcfg = config.load_project_config(str(repo))
    # Re-entry after a partial spawn: a prior attempt failed mid-loop and rolled
    # back to 'planning' (spawn_state='failed'), leaving some specialists already
    # running. Carry those forward and skip their indices so a re-approve doesn't
    # double-spawn into the same worktree/branch. Normal first spawns start empty.
    _prior = _read_team_state(tf)
    if _prior.get("spawn_state") == "failed":
        launched = [a for a in _prior.get("assignments", []) if isinstance(a, dict) and "i" in a]
        already_idx = {a["i"] for a in launched}
    else:
        launched = []
        already_idx = set()
    try:
        for i, assignment in enumerate(plan.assignments):
            if i in already_idx:
                continue  # already launched on a prior (partial) attempt
            catalog = team.load_role_catalog()
            role = catalog[assignment.role]
            # Build the per-specialist worktree if they're an editor.
            if role.can_edit:
                wt_path = repo / ".johnstudio" / "worktrees" / (
                    f"task-{task_number:04d}-team-{assignment.role}-{i}"
                )
                branch = f"ai/task-{task_number:04d}/team/{assignment.role}-{i}"
                if not wt_path.exists():
                    gw.add_worktree(repo, wt_path, branch, base=pcfg.base_branch)
                cwd = wt_path
            else:
                wt_path = None
                branch = None
                cwd = tf

            prompt_md = _build_specialist_prompt(
                role=role, assignment=assignment,
                plan_summary=plan.summary,
                plan=plan.to_dict(),
                task_text=_task_description(task_db_id),
                task_number=task_number,
                project_name=proj_name,
                repo=repo, worktree=wt_path,
                worker_index=i + 1,
            )
            # Provider-aware throttle: wait until fewer than N workers of this
            # provider are still alive before launching, so concurrent
            # same-provider workers can't trip that provider's shared rate limit.
            _waited = 0
            while _waited < 600:
                alive = sum(
                    1 for a in launched
                    if a.get("provider") == role.provider
                    and a.get("pid") and _pid_alive(int(a["pid"]))
                )
                if alive < MAX_CONCURRENT_PER_PROVIDER:
                    break
                time.sleep(3)
                _waited += 3

            spawn = spawn_and_track(
                role=role, cwd=cwd, prompt_md=prompt_md,
                prompt_path=tf / "prompts" / f"team_{assignment.role}_{i}.md",
                log_path=tf / "logs" / f"team_{assignment.role}_{i}.log",
                task_db_id=task_db_id, worktree_path=wt_path, branch_name=branch,
                result_path=(wt_path or tf) / "RESULT.md",
            )
            launched.append({
                "i": i, "run_id": spawn.run_id, "role": role.name, "vp": role.vp,
                "provider": role.provider,
                "brief": assignment.brief, "output": assignment.output,
                "worktree": str(wt_path) if wt_path else None,
                "pid": spawn.pid,
            })
            # Per-specialist SSE progress. The spawn already inserted a
            # `runs` row (which the stream picks up as a node); this hook
            # gives the UI an explicit "specialist N of M spawned" beat.
            try:
                bus.emit(EventTypes.WORKER_SPAWNED, {
                    "task_id": task_db_id, "task_number": task_number,
                    "run_id": spawn.run_id, "role": role.name, "vp": role.vp,
                    "index": i, "total": len(plan.assignments),
                    "pid": spawn.pid,
                })
            except Exception:
                _log.exception("failed to emit worker.spawned hook (task %s, role %s)",
                               task_db_id, role.name)
            # Persist incremental progress so a UI poll/reconnect sees the
            # team filling in even before the whole loop finishes.
            st = _read_team_state(tf)
            st["assignments"] = list(launched)
            st["spawn_state"] = "spawning"
            _write_team_state(tf, st)
    except Exception:
        # Roll the SQL gate back so the user can retry. We do not try to
        # kill the already-launched spawns — they're harmless background
        # processes and the next approve will pick up their results.
        _log.exception("team spawn failed for task %s; rolling status back to planning",
                       task_db_id)
        rb = db.connect()
        try:
            rb.execute(
                "UPDATE tasks SET status = 'planning' WHERE id = ? AND status = 'running'",
                (task_db_id,),
            )
            rb.commit()
        finally:
            rb.close()
        st = _read_team_state(tf)
        st["status"] = "planning"
        st["spawn_state"] = "failed"
        st["assignments"] = list(launched)
        st["spawn_error_at"] = _now()
        _write_team_state(tf, st)
        return

    # Finalize: every specialist launched. Row is already 'running' from
    # the gate; just record the completed assignment set + clear the
    # 'spawning' substate.
    state = _read_team_state(tf)
    state["status"] = "running"
    state["spawn_state"] = "spawned"
    state["assignments"] = launched
    state["approved_at"] = state.get("approved_at") or _now()
    state["plan"] = plan.to_dict()
    _write_team_state(tf, state)


def replan_team_task(task_db_id: int) -> dict:
    """Re-issue the lead planner after a failed plan validation.

    The validity gate (Item 2) parks an invalid plan at 'needs_replan'
    instead of wedging it at 'running'. This re-spawns the planner with a
    fresh prompt that includes the prior validation error, archives the
    bad TEAM_PLAN.md, and resets the task to 'planning' so the normal
    approve flow can run again.

    Allowed from 'needs_replan' or 'planning' (re-roll a not-yet-approved
    plan). Refused once specialists are running.
    """
    proj_info = _project_for_task(task_db_id)
    if not proj_info:
        raise KeyError(f"task {task_db_id} not found")
    proj_name, repo, task_number = proj_info
    tf = _task_folder(repo, task_number)

    # Only re-plan from a pre-spawn state. Atomic gate so two clicks don't
    # both re-spawn the planner.
    conn = db.connect()
    try:
        cur = conn.execute(
            "UPDATE tasks SET status = 'planning' "
            "WHERE id = ? AND status IN ('needs_replan', 'planning')",
            (task_db_id,),
        )
        won = cur.rowcount == 1
        conn.commit()
    finally:
        conn.close()
    if not won:
        return {"refused": True, "reason": "task is not in a re-plannable state"}

    state = _read_team_state(tf)
    prior_error = state.get("plan_error")

    # Archive the rejected plan so the next parse doesn't re-read it.
    plan_path = tf / "TEAM_PLAN.md"
    if plan_path.exists():
        try:
            plan_path.rename(tf / f"TEAM_PLAN.rejected.{_now().replace(':', '')}.md")
        except OSError:
            pass

    catalog = team.load_role_catalog()
    if PLANNER_ROLE not in catalog:
        raise RuntimeError(f"role catalog is missing {PLANNER_ROLE!r}; cannot replan")
    planner_role = catalog[PLANNER_ROLE]
    prompt_md = _build_planner_prompt(
        role=planner_role, catalog=catalog,
        task_text=_task_description(task_db_id), task_number=task_number,
        project_name=proj_name, repo=repo,
    )
    if prior_error:
        prompt_md += (
            "\n\n---\n\n## Your previous plan was REJECTED by validation\n\n"
            f"Fix this and re-emit a valid TEAM_PLAN.md:\n\n> {prior_error}\n\n"
            "Common cause: a role assigned under the wrong VP. Every role must "
            "be listed under the VP the catalog above places it in."
        )
    prompt_path = tf / "prompts" / f"replanning_{PLANNER_ROLE}.md"
    prompt_path.write_text(prompt_md, encoding="utf-8")
    log_path = tf / "logs" / f"replanning_{PLANNER_ROLE}.log"

    spawn = spawn_and_track(
        role=planner_role, cwd=tf, prompt_md=prompt_md,
        prompt_path=prompt_path, log_path=log_path,
        task_db_id=task_db_id, worktree_path=None, branch_name=None,
        result_path=tf / "TEAM_PLAN.md",
    )

    state["status"] = "planning"
    state.pop("plan_valid", None)
    state.pop("plan_error", None)
    state.pop("plan_error_stage", None)
    state["replanned_at"] = _now()
    _write_team_state(tf, state)

    return {
        "replanned": True, "status": "planning",
        "planner_pid": spawn.pid, "planner_run_id": spawn.run_id,
        "prior_error": prior_error,
    }


# ---------------------------------------------------------------------------
# Phase 2: cross-VP review + merge-plan consolidation
# ---------------------------------------------------------------------------

def advance_team_task(task_db_id: int) -> dict:
    """Serialized wrapper around the state-machine tick (per-task lock)."""
    with _task_lock(task_db_id):
        return _advance_team_task_impl(task_db_id)


def _advance_team_task_impl(task_db_id: int) -> dict:
    """Idempotent state-machine tick.

    Each transition is gated on a SQL `UPDATE … WHERE status=?`
    `rowcount == 1` check so concurrent callers (UI polling + manual
    advance + retries) can't both run the same transition. The expensive
    work (spawning reviewers, generating MERGE_PLAN.md) only runs for
    the caller that wins the race.

    Transitions:
    - running → reviewing  (or → pending_merge if no cross_review)
    - reviewing → pending_merge
    - pending_merge → no-op (human gate)
    """
    proj_info = _project_for_task(task_db_id)
    if not proj_info:
        raise KeyError(f"task {task_db_id} not found")
    proj_name, repo, task_number = proj_info
    tf = _task_folder(repo, task_number)
    state = _read_team_state(tf)
    status = state.get("status")

    if status == "planning":
        # Planner provider fallback: if the planner died (e.g. its provider's
        # quota is exhausted) without producing a plan, re-fire it on the next
        # available provider so one exhausted provider doesn't stall the loop.
        refired = _refire_planner_if_needed(task_db_id, proj_name, repo, task_number, tf)
        return {"status": "planning", "planner_refired": refired}

    if status == "running":
        # Don't transition while the background spawn thread is still launching
        # the team (the per-provider throttle staggers launches, so an early
        # finisher must not make _all_specialists_done True over a PARTIAL set
        # and trigger review before the rest are even spawned).
        if state.get("spawn_state") not in (None, "spawned"):
            return {"status": status, "waiting": "spawning"}
        expected = state.get("expected_specialists")
        if expected is not None and len(state.get("assignments") or []) < expected:
            return {"status": status, "waiting": "spawning"}
        if not _all_specialists_done(state, repo):
            return {"status": status, "waiting": "specialists"}

        # Tests-as-signal: before transitioning to cross-VP review, run
        # the project's test_commands inside every editor's worktree.
        # If any fail (and budget + round cap allow), auto-spawn the
        # debugger + a revision pass and stay in `running` until the
        # next advance tick finds the work done.
        test_round = int(state.get("test_round") or 0)
        bs0 = check_budget(task_db_id)
        if test_round < MAX_REVISE_ROUNDS and not bs0.get("over_budget"):
            tres = _maybe_spawn_test_recovery(
                state=state, tf=tf, repo=repo, task_db_id=task_db_id,
                task_number=task_number, project_name=proj_name,
                round=test_round + 1,
            )
            if tres is not None:
                state["test_round"] = test_round + 1
                state.setdefault("test_loops", []).append(tres)
                _write_team_state(tf, state)
                return {"status": "running", "tests_failed": True,
                        "recovery_round": test_round + 1, "spawned": tres}

        plan = state.get("plan") or {}
        cross = plan.get("cross_review") or []
        next_status = "reviewing" if cross else "pending_merge"
        if not _try_transition(task_db_id, "running", next_status):
            # Lost the race to another caller; just report current state.
            return {"status": _read_team_state(tf).get("status", next_status), "raced": True}
        if not cross:
            merge_plan_path = _generate_merge_plan(state, tf, repo, task_number, proj_name)
            state["status"] = "pending_merge"
            state["merge_plan_path"] = str(merge_plan_path)
            state["pending_merge_at"] = _now()
            _write_team_state(tf, state)
            return {"status": "pending_merge", "skipped_reviewing": True, "merge_plan_path": str(merge_plan_path)}
        reviewers = _spawn_cross_reviewers(state, tf, repo, task_db_id, task_number, proj_name, cross)
        state["status"] = "reviewing"
        state["cross_reviewers"] = reviewers
        state["reviewing_started_at"] = _now()
        _write_team_state(tf, state)
        return {"status": "reviewing", "launched_reviewers": reviewers}

    if status == "reviewing":
        if not _all_reviewers_done(state, repo, task_number):
            return {"status": status, "waiting": "reviewers"}

        # Auto-revise on `needs-changes` verdict from any cross-VP
        # reviewer — capped at MAX_REVISE_ROUNDS. Without this the team
        # detects "the reviewer hated it" and writes a merge plan anyway,
        # which is exactly the open-loop failure mode the deep review
        # called out.
        revise_round = int(state.get("revise_round") or 0)
        bs = check_budget(task_db_id)
        if not bs.get("over_budget") and revise_round < MAX_REVISE_ROUNDS:
            needs = _check_needs_revision(state, repo, task_number)
            if needs:
                # Don't transition; spawn revisions and stay in reviewing.
                catalog = team.load_role_catalog()
                spawned = []
                for n in needs:
                    role = catalog.get(n["assignment"]["role"])
                    if not role:
                        continue
                    out = _spawn_revision(
                        role=role, original_assignment=n["assignment"],
                        review_text=n["review_text"], repo=repo, tf=tf,
                        task_db_id=task_db_id, task_number=task_number,
                        project_name=proj_name, round=revise_round + 1,
                    )
                    if out.get("launched"):
                        spawned.append(out)
                if spawned:
                    state["revise_round"] = revise_round + 1
                    # Clear cross-reviewers' DONE markers so the next
                    # advance call waits for the re-review post-revision.
                    for c in state.get("cross_reviewers") or []:
                        out_path = c.get("output", "")
                        if out_path:
                            for p in [Path(out_path), tf / "team_notes" / "DONE.md"]:
                                if p.exists():
                                    try: p.unlink()
                                    except OSError: pass
                    state["last_revisions"] = spawned
                    _write_team_state(tf, state)
                    return {"status": "revising", "spawned": spawned, "round": revise_round + 1}

        if not _try_transition(task_db_id, "reviewing", "pending_merge"):
            return {"status": _read_team_state(tf).get("status", "pending_merge"), "raced": True}
        merge_plan_path = _generate_merge_plan(state, tf, repo, task_number, proj_name)
        state["status"] = "pending_merge"
        state["merge_plan_path"] = str(merge_plan_path)
        state["pending_merge_at"] = _now()
        _write_team_state(tf, state)
        return {"status": "pending_merge", "merge_plan_path": str(merge_plan_path)}

    return {"status": status, "no_op": True}


def _try_transition(task_db_id: int, from_status: str, to_status: str) -> bool:
    """SQL-rowcount gate. Returns True iff this caller flipped the row."""
    conn = db.connect()
    try:
        cur = conn.execute(
            "UPDATE tasks SET status = ? WHERE id = ? AND status = ?",
            (to_status, task_db_id, from_status),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Cross-VP review spawning
# ---------------------------------------------------------------------------

def _spawn_cross_reviewers(
    state: dict, tf: Path, repo: Path, task_db_id: int, task_number: int,
    proj_name: str, cross_review_entries: list,
) -> list[dict]:
    """Spawn one reviewer per entry in the plan's cross_review block.

    Reviewer ROLE is parsed from "<role> (<vp>)" strings produced by the
    planner. We resolve the role from the catalog (must be read-only —
    can_edit must be False) and build a focused context pack that lists
    the files this reviewer is expected to read.
    """
    catalog = team.load_role_catalog()
    launched: list[dict] = []

    for i, entry in enumerate(cross_review_entries):
        reviewer_field = str(entry.get("reviewer", "")).strip()
        reads = [str(r).strip() for r in (entry.get("reads") or [])]
        role_name = _strip_vp_paren(reviewer_field)
        if role_name not in catalog:
            # Plan named a reviewer that doesn't exist in the catalog.
            # Skip but record so the user sees it.
            launched.append({
                "i": i, "reviewer": reviewer_field, "skipped": True,
                "reason": f"role {role_name!r} not in catalog",
            })
            continue
        role = catalog[role_name]
        if role.can_edit:
            launched.append({
                "i": i, "reviewer": reviewer_field, "skipped": True,
                "reason": f"role {role.name!r} is can_edit=True (not a reviewer)",
            })
            continue

        # Resolve `reads` to actual absolute paths. The plan uses paths
        # relative to either the task folder or one of the specialist
        # worktrees; we search both.
        resolved = [_resolve_artifact_path(p, tf, state) for p in reads]
        resolved = [r for r in resolved if r is not None]

        prompt_md = _build_cross_reviewer_prompt(
            role=role, reads=reads, resolved_reads=resolved,
            task_text=_task_description(task_db_id),
            task_number=task_number, project_name=proj_name,
        )
        cwd = tf / "team_notes"
        cwd.mkdir(parents=True, exist_ok=True)
        spawn = spawn_and_track(
            role=role, cwd=cwd, prompt_md=prompt_md,
            prompt_path=tf / "prompts" / f"cross_review_{role.name}_{i}.md",
            log_path=tf / "logs" / f"cross_review_{role.name}_{i}.log",
            task_db_id=task_db_id, worktree_path=None, branch_name=None,
            result_path=cwd / f"CROSS_REVIEW_{role.name}_{i}.md",
        )
        launched.append({
            "i": i, "reviewer": reviewer_field, "role": role.name, "vp": role.vp,
            "reads": reads, "run_id": spawn.run_id,
            "output": str(cwd / f"CROSS_REVIEW_{role.name}_{i}.md"),
            "pid": spawn.pid,
        })

    return launched


def _build_cross_reviewer_prompt(
    *, role: team.Role, reads: list[str], resolved_reads: list[Path],
    task_text: str, task_number: int, project_name: str,
) -> str:
    parts: list[str] = []
    parts.append(role.system_prompt.rstrip())
    parts.append("\n---\n")
    parts.append(f"# Cross-VP review — Task {task_number:04d} — project `{project_name}`\n")
    parts.append("## User request (verbatim)\n")
    parts.append(task_text + "\n")
    parts.append("## Your scope as a cross-VP reviewer\n")
    parts.append(
        "You were named in the lead planner's cross-VP review block. You read the "
        "artifacts another VP's team produced and report findings from your own "
        "perspective. You do NOT edit code. You DO write a single review file."
    )
    parts.append("\n## Files to read\n")
    if not resolved_reads:
        parts.append("_(planner named files but none were found on disk; flag this in your review)_")
    else:
        for orig, p in zip(reads, resolved_reads):
            if p and p.exists():
                body = p.read_text(encoding="utf-8", errors="replace")
                parts.append(f"### `{orig}` (resolved: `{p}`)\n")
                parts.append("```\n" + body[:8000] + "\n```\n")
            else:
                parts.append(f"### `{orig}` — not found on disk\n")
    parts.append("\n## Output contract\n")
    parts.append(
        f"Write your review to the current working directory as the file named "
        f"`CROSS_REVIEW_{role.name}_<i>.md` — but in practice just write to "
        f"`CROSS_REVIEW.md`. The orchestrator finds it by name pattern.\n\n"
        f"Structure: a single `## Verdict: approve | needs-changes | reject` line, "
        f"then **Strengths**, **Required changes**, **Notes**. Then write `DONE.md` "
        f"with `status: COMPLETE`."
    )
    return "\n".join(parts)


def _strip_vp_paren(s: str) -> str:
    """Turn 'code-reviewer (claude_vp)' into 'code-reviewer'."""
    i = s.find("(")
    return s[:i].strip() if i != -1 else s.strip()


def _resolve_artifact_path(rel: str, tf: Path, state: dict) -> Path | None:
    """Find a planner-named path on disk, bounded to known-safe roots.

    The planner produces TEAM_PLAN.md from an LLM, so its `reads:` paths
    are user-influenceable. We refuse:
    - Absolute paths (the LLM should never need them).
    - Any path that resolves outside the task folder, the team_notes
      subfolder, or one of the registered specialist worktrees — even if
      it would have hit a real file. `..`-based escapes don't work.
    """
    # Hard reject anything that's already absolute. The planner has no
    # business referencing /etc, /home/X, ~/.ssh, etc.
    if Path(rel).is_absolute():
        return None
    candidate_roots: list[Path] = [tf, tf / "team_notes"]
    for a in state.get("assignments") or []:
        wt = a.get("worktree")
        if wt:
            candidate_roots.append(Path(wt))
    for root in candidate_roots:
        try:
            root_resolved = root.resolve()
        except OSError:
            continue
        cand = (root / rel)
        try:
            cand_resolved = cand.resolve()
        except OSError:
            continue
        # `is_relative_to` is the safe check that defeats `..`.
        try:
            cand_resolved.relative_to(root_resolved)
        except ValueError:
            continue
        if cand_resolved.exists():
            return cand_resolved
    return None


# ---------------------------------------------------------------------------
# Specialist + reviewer done-detection
# ---------------------------------------------------------------------------

def _all_specialists_done(state: dict, repo: Path) -> bool:
    """A specialist is "done" iff its DONE.md exists AND its promised
    output file exists at one of the known locations. This catches the
    common silent-failure mode where a specialist exits with stop_reason
    'end_turn' without ever writing the artifact it was briefed to
    produce — previously we'd treat that as success and advance the
    task; now the orchestrator keeps waiting (and the watchdog will
    eventually mark the wedged run as idle, freeing the task for retry)."""
    assignments = state.get("assignments") or []
    if not assignments:
        return False
    tf = _task_folder(repo, state.get("task_number", 0))
    for a in assignments:
        wt = a.get("worktree")
        if wt:
            if not (Path(wt) / "DONE.md").exists():
                return False
            # Output verification: the briefed output file must exist.
            if not _output_file_exists(a, Path(wt), tf):
                return False
        else:
            # Read-only specialists write into the task folder (or team_notes/).
            # Their DONE.md lands in the cwd we set when spawning them.
            if not (tf / "DONE.md").exists() and not _readonly_artifact_landed(a, tf):
                return False
            # Output verification: same as above.
            if not _output_file_exists(a, None, tf):
                return False
    return True


def _output_file_exists(assignment: dict, wt_path: Path | None, tf: Path) -> bool:
    """Check that the briefed output file exists at one of the known
    locations. Returns True if the assignment didn't promise an output
    (some specialists are signal-only)."""
    out_rel = assignment.get("output") or ""
    if not out_rel:
        return True
    candidates: list[Path] = []
    if wt_path:
        candidates.append(wt_path / out_rel)
    candidates.append(tf / out_rel)
    # Some artifacts land directly under the task folder's `artifacts/`
    # or `reviews/` — the brief may have used a flat path.
    candidates.append(tf / "artifacts" / Path(out_rel).name)
    candidates.append(tf / "reviews" / Path(out_rel).name)
    return any(p.exists() for p in candidates)


def _readonly_artifact_landed(assignment: dict, tf: Path) -> bool:
    """A read-only specialist may not write DONE.md if it wrote its expected
    output file. Accept the named output file as the completion signal."""
    out_name = assignment.get("output") or ""
    if not out_name:
        return False
    cand = _resolve_artifact_path(out_name, tf, {"assignments": []})
    return cand is not None and cand.exists()


def _all_reviewers_done(state: dict, repo: Path, task_number: int) -> bool:
    cross = state.get("cross_reviewers") or []
    if not cross:
        return True
    tf = _task_folder(repo, task_number)
    notes_dir = tf / "team_notes"
    for c in cross:
        if c.get("skipped"):
            continue
        # Cross reviewer either wrote CROSS_REVIEW.md or CROSS_REVIEW_<role>_<i>.md
        # in team_notes.
        out_path = Path(c.get("output", ""))
        if out_path.exists():
            continue
        # Fallback: any CROSS_REVIEW*.md in team_notes works.
        if any(notes_dir.glob("CROSS_REVIEW*.md")):
            continue
        return False
    return True


# ---------------------------------------------------------------------------
# Merge-plan consolidation
# ---------------------------------------------------------------------------

def _generate_merge_plan(
    state: dict, tf: Path, repo: Path, task_number: int, proj_name: str,
) -> Path:
    """Collect every worktree the editor specialists produced, compute the
    cross-cutting file footprint, and write a MERGE_PLAN.md the human can
    use as the merge contract.
    """
    assignments = state.get("assignments") or []
    branches: list[dict] = []
    files_to_branches: dict[str, list[str]] = {}

    for a in assignments:
        wt = a.get("worktree")
        if not wt:
            continue
        wt_path = Path(wt)
        if not wt_path.exists():
            continue
        branch_name = _branch_for_worktree(repo, wt_path)
        files, diff_stat = _diff_summary(wt_path, base="main")
        branches.append({
            "role": a.get("role"),
            "worktree": str(wt_path),
            "branch": branch_name,
            "files": files,
            "stat": diff_stat,
            "result_md": (wt_path / "RESULT.md").read_text(encoding="utf-8", errors="replace")[:4000]
                          if (wt_path / "RESULT.md").exists() else "",
        })
        for f in files:
            files_to_branches.setdefault(f, []).append(branch_name or a.get("role", "?"))

    conflicts = {f: bs for f, bs in files_to_branches.items() if len(bs) > 1}

    # Reviewer findings — concatenate any CROSS_REVIEW*.md
    reviews_md = []
    for p in sorted((tf / "team_notes").glob("CROSS_REVIEW*.md")):
        reviews_md.append((p.name, p.read_text(encoding="utf-8", errors="replace")[:4000]))

    out_path = tf / "MERGE_PLAN.md"
    lines: list[str] = []
    lines.append(f"# MERGE_PLAN.md — Task {task_number:04d}")
    lines.append("")
    lines.append(f"_Project: `{proj_name}` · generated {_now()}_")
    lines.append("")
    lines.append("## Summary")
    lines.append(state.get("plan", {}).get("summary", "") or "_(no plan summary)_")
    lines.append("")

    lines.append("## Branches")
    if not branches:
        lines.append("_(no editor specialists ran; nothing to merge)_")
    else:
        for b in branches:
            lines.append(f"### `{b['role']}` → `{b['branch']}`")
            lines.append(f"- Worktree: `{b['worktree']}`")
            lines.append(f"- Files changed ({len(b['files'])}): {', '.join(b['files']) or '_(none)_'}")
            if b["stat"]:
                lines.append(f"- Stat: `{b['stat']}`")
            if b["result_md"]:
                lines.append("- RESULT.md preview:")
                lines.append("  > " + b["result_md"][:500].replace("\n", "\n  > "))
            lines.append("")

    lines.append("## Expected conflicts")
    arbiter_info: dict | None = None
    if conflicts:
        for f, bs in sorted(conflicts.items()):
            lines.append(f"- `{f}` — touched by: {', '.join(sorted(set(bs)))}")
        # Auto-spawn the architect role as arbiter. The architect's
        # CONFLICT_RESOLUTION.md will appear in the task folder; the
        # human reviews both before merging.
        arbiter_info = _spawn_conflict_arbiter(
            tf=tf, repo=repo, task_db_id=state.get("task_db_id") or 0,
            task_number=task_number, project_name=proj_name,
            conflicts=conflicts, branches=branches,
        )
        if arbiter_info:
            lines.append("")
            lines.append(f"_Architect arbiter spawned (run {arbiter_info['run_id']}); "
                         f"see `{arbiter_info['output_path']}` when DONE._")
    else:
        lines.append("_(no overlapping file edits across branches)_")
    lines.append("")

    lines.append("## Cross-VP review findings")
    if not reviews_md:
        lines.append("_(no cross-VP reviews ran)_")
    else:
        for name, body in reviews_md:
            lines.append(f"### {name}")
            lines.append("```markdown")
            lines.append(body)
            lines.append("```")
            lines.append("")

    lines.append("## Suggested merge sequence")
    if branches:
        lines.append("```bash")
        lines.append(f"git checkout main")
        for b in branches:
            br = b["branch"] or "?"
            lines.append(f"git merge --no-ff {br}")
        lines.append("```")
    lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")

    # Best-effort: write the per-task retro and append per-role lessons.
    # This is what closes the institutional-knowledge loop — the next
    # task's specialists read their own lessons via _memory_injection_for.
    _write_team_retro(
        repo=repo, task_number=task_number, task_text=state.get("plan", {}).get("summary", "") or "",
        plan=state.get("plan", {}),
        assignments=state.get("assignments", []) or [],
        cross_reviewers=state.get("cross_reviewers", []) or [],
    )

    return out_path


def _branch_for_worktree(repo: Path, wt: Path) -> str | None:
    """git -C <wt> rev-parse --abbrev-ref HEAD"""
    import subprocess
    try:
        cp = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        if cp.returncode == 0:
            return cp.stdout.strip()
    except Exception:
        pass
    return None


def _diff_summary(wt: Path, *, base: str = "main") -> tuple[list[str], str]:
    """Return (changed_files, single-line shortstat) for `wt` vs `base`."""
    import subprocess
    files: list[str] = []
    stat = ""
    try:
        cp = subprocess.run(
            ["git", "-C", str(wt), "diff", "--name-only", f"{base}...HEAD"],
            capture_output=True, text=True, timeout=15,
        )
        if cp.returncode == 0:
            files = [f for f in cp.stdout.splitlines() if f.strip()]
        cp = subprocess.run(
            ["git", "-C", str(wt), "diff", "--shortstat", f"{base}...HEAD"],
            capture_output=True, text=True, timeout=15,
        )
        if cp.returncode == 0:
            stat = cp.stdout.strip()
    except Exception:
        pass
    return files, stat


def _set_task_status(task_db_id: int, status: str) -> None:
    conn = db.connect()
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_db_id))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def _build_planner_prompt(
    *, role: team.Role, catalog: dict[str, team.Role],
    task_text: str, task_number: int, project_name: str, repo: Path,
) -> str:
    """The planner's prompt: its system prompt + role catalog + task + project."""
    sections: list[str] = []
    sections.append(role.system_prompt.rstrip())
    sections.append("")
    sections.append("---")
    sections.append("")
    sections.append(f"# Task {task_number:04d} — project `{project_name}`")
    sections.append("")
    sections.append("## User request")
    sections.append(task_text)
    sections.append("")

    sections.append("## Role catalog you may assign from")
    sections.append("")
    sections.append("Only assign roles from this list. Each line is `name (vp): description`.")
    sections.append("")
    grouped = team.roles_by_vp(catalog)
    for vp, roles in grouped.items():
        if vp == "gemini_vp":
            # Don't let the planner assign itself.
            roles = [r for r in roles if r.name != PLANNER_ROLE]
        sections.append(f"### {vp}")
        for r in roles:
            edit = "(editor)" if r.can_edit else "(read-only)"
            sections.append(f"- **{r.name}** {edit} — {r.description}")
        sections.append("")

    # Optional project memory.
    mem_dir = repo / ".johnstudio" / "memory"
    if mem_dir.exists():
        sections.append("## Project memory excerpts")
        for fname in ("project_brief.md", "current_state.md", "architecture.md"):
            p = mem_dir / fname
            if p.exists():
                body = p.read_text(encoding="utf-8", errors="replace").strip()
                if body:
                    sections.append(f"### {fname}")
                    sections.append(body[:2000])
                    sections.append("")

    sections.append("## Output contract")
    sections.append("")
    sections.append("Write your TEAM_PLAN.md to the current working directory as the file")
    sections.append("named `TEAM_PLAN.md`. Then write `DONE.md` containing the single line")
    sections.append("`status: COMPLETE`. Do not write any other files. Do not commit.")
    sections.append("")
    sections.append("Use the exact YAML structure from your system prompt above. The")
    sections.append("orchestrator parses it; deviations will fail validation.")
    return "\n".join(sections)


def _build_specialist_prompt(
    *, role: team.Role, assignment: team.Assignment,
    plan_summary: str, plan: dict, task_text: str, task_number: int,
    project_name: str, repo: Path, worktree: Path | None,
    worker_index: int | None = None,
) -> str:
    """Compose a specialist's full prompt.

    Layers (in order):
    1. Role's system-prompt body (from the markdown).
    2. The full context pack from `context_builder.build_context_pack`
       — skill router output, project memory, safety rules, scope, rule
       precedence, output contract. Previously bypassed in team mode,
       which gave specialists a worse prompt than parallel-mode workers.
    3. Plan-level context — summary + acceptance criteria. The plan
       parses acceptance_criteria; specialists are now told them.
    4. Role-specific memory injection — relevant decisions + the role's
       own agent_lessons file. Activates the memory vault that the
       review found to be write-only.
    5. The specialist's assignment-specific brief + output contract.
    """
    sections: list[str] = []

    # 1. Role system prompt (the markdown body authored in
    # seeds/roles/<vp>/<role>.md).
    sections.append(role.system_prompt.rstrip())
    sections.append("\n---\n")

    # 2. Full context pack — skills, project memory, safety, scope.
    pcfg = config.load_project_config(str(repo))
    wcfg = _worker_cfg_for_role(role)
    try:
        _pack, ctx_md = context_builder.build_context_pack(
            project_cfg=pcfg, project_name=project_name,
            worker_name=role.name, worker_cfg=wcfg,
            task_id=task_number, task_title=assignment.brief[:80],
            task_description=task_text,
            worktree_path=worktree,
            worker_index=worker_index,
        )
        sections.append(ctx_md)
        sections.append("\n---\n")
    except Exception as e:
        # context_builder relies on optional configs; never block a
        # specialist on its absence — just note it.
        sections.append(f"\n_(context-pack synthesis failed: {e})_\n")

    # 3. Plan-level context (summary + acceptance criteria).
    sections.append("## Plan summary (from the lead planner)\n")
    sections.append(plan_summary)
    sections.append("")
    ac = plan.get("acceptance_criteria") or []
    if ac:
        sections.append("## Acceptance criteria you must satisfy\n")
        sections.append(
            "Your output is being measured against these. Self-check "
            "each one before you write `DONE.md`.\n"
        )
        for i, c in enumerate(ac, start=1):
            sections.append(f"{i}. {c}")
        sections.append("")

    # 4. Memory injection.
    mem_md = _memory_injection_for(repo, role)
    if mem_md:
        sections.append(mem_md)

    # 4b. RAG retrieval over the whole vault. Unlike (4)'s fixed lessons +
    # recent-decisions slice, this surfaces the chunks most relevant to THIS
    # task (past runs, bugs, ADRs, handoffs) ranked by BM25. Best-effort: a
    # missing/empty vault returns "" and the section is simply omitted.
    try:
        retrieval_query = f"{assignment.brief}\n{task_text}"
        rag_md = rag_memory.query(repo, retrieval_query)
        if rag_md:
            sections.append(rag_md)
    except Exception as e:
        sections.append(f"\n_(memory retrieval failed: {e})_\n")

    # 5. The assignment.
    sections.append("## Your specific assignment\n")
    sections.append(f"**Role:** {role.name}  ")
    sections.append(f"**VP:** {role.vp}  ")
    sections.append(f"**Brief:** {assignment.brief}  ")
    sections.append(f"**Expected output:** {assignment.output}")
    sections.append("")
    if worktree is not None:
        sections.append(f"**Worktree (you may edit files inside this dir only):** `{worktree}`")
    else:
        sections.append("**Read-only:** you have no worktree. Write your artifact to the current working directory.")
    sections.append("")
    sections.append("## Autonomy contract — READ THIS FIRST\n")
    sections.append(
        "You are running **autonomous, non-interactive**. There is **no human "
        "to answer questions**. The lead planner already approved your brief.\n\n"
        "**Forbidden:** `AskUserQuestion`, `Workflow`, `EnterPlanMode`/`ExitPlanMode`, "
        "`ScheduleWakeup`, `CronCreate`. Calling any of these stalls the run "
        "indefinitely and your task fails.\n\n"
        "**Required posture:** Make every judgment call yourself. If something "
        "is ambiguous, **pick the most defensible default and document the "
        "choice in your output** — do not stop to ask. If you would normally "
        "say 'let me know your call on X', instead pick X yourself and note "
        "the rationale.\n\n"
        "**You must ship the artifact in this single run.** Do not propose a "
        "plan and wait. Do not write 'I'll start with X once you confirm'. "
        "Just do X.\n"
    )
    if role.can_spawn_subagents:
        sections.append(
            "**Subagent permission:** you ARE allowed to use the `Task` tool to "
            "spawn Claude Code's built-in subagents (Explore, Plan, "
            "implementer, verifier, general-purpose). Use them for genuinely "
            "decomposable subwork — large refactors, broad codebase searches, "
            "independent parallel investigations. Don't use them for trivial "
            "operations a single Bash/Read call would handle. Subagents add "
            "real latency and token cost — pick them when the parallelism or "
            "context-isolation actually helps.\n"
        )
    sections.append("## Output contract\n")
    if role.can_edit:
        sections.append(
            "1. Implement the assignment inside your worktree.\n"
            "2. Verify each acceptance criterion above is satisfied by your changes.\n"
            "3. `git add -A && git commit -m \"<short message>\"` on your branch.\n"
            "4. Write `RESULT.md` in your worktree summarizing what you did, "
            "including one bullet per acceptance criterion noting how you satisfied it.\n"
            "5. Write `DONE.md` with `status: COMPLETE`."
        )
    else:
        sections.append(
            f"1. Write your artifact at `{assignment.output}` (relative to the cwd if no absolute path).\n"
            "2. Self-check against each acceptance criterion above; cite which ones your output addresses.\n"
            "3. Write `DONE.md` with `status: COMPLETE`.\n"
            "Do not modify any other files. Do not commit."
        )
    return "\n".join(sections)


def _memory_injection_for(repo: Path, role: team.Role) -> str:
    """Pull the role's prior lessons + any relevant decisions for the
    current task. Cheap; bounded by character cap so the prompt stays
    reasonable.
    """
    parts: list[str] = []
    mem_root = memory.memory_root(repo)
    if not mem_root.exists():
        return ""

    # Per-role accumulated lessons. The team_orchestrator writes one per
    # task at retro time (_write_team_retro).
    lessons = mem_root / "agent_lessons" / f"{role.name}.md"
    if lessons.exists():
        body = lessons.read_text(encoding="utf-8", errors="replace")
        if body.strip():
            parts.append("## Your accumulated lessons (across prior tasks)\n")
            parts.append(body[:3000])
            parts.append("")

    # Recent decisions — most recent 5, capped at ~500 chars each.
    decisions_dir = mem_root / "decisions"
    if decisions_dir.exists():
        recent = sorted(
            decisions_dir.glob("*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:5]
        if recent:
            parts.append("## Recent project decisions to respect\n")
            for d in recent:
                body = d.read_text(encoding="utf-8", errors="replace").strip()
                parts.append(f"### {d.name}")
                parts.append(body[:600])
                parts.append("")
    return "\n".join(parts)


def _spawn_conflict_arbiter(
    *, tf: Path, repo: Path, task_db_id: int, task_number: int,
    project_name: str, conflicts: dict[str, list[str]],
    branches: list[dict],
) -> dict | None:
    """When two or more editor specialists touched the same file, spawn
    the architect role as a tie-breaker. The architect reads both diffs
    + RESULT.md from the conflicting workers and writes
    `CONFLICT_RESOLUTION.md` with a `Winner: <role>` line per conflict.

    Returns the launched info, or None if the architect role isn't
    available or there are no conflicts.
    """
    if not conflicts:
        return None
    catalog = team.load_role_catalog()
    if "architect" not in catalog:
        return None
    role = catalog["architect"]

    parts = [role.system_prompt.rstrip(), "\n---\n"]
    parts.append(f"# Conflict resolution — Task {task_number:04d} — project `{project_name}`\n")
    parts.append("Two or more editor specialists touched the same file(s). "
                 "You decide which version ships per conflicting file. Don't merge — pick.\n")
    parts.append("\n## Conflicting files (path → branches that touched it)\n")
    for f, bs in sorted(conflicts.items()):
        parts.append(f"- `{f}` ← {', '.join(sorted(set(bs)))}")
    parts.append("\n## Branch contents per conflict\n")
    branch_by_role: dict[str, dict] = {(b.get("role") or "?"): b for b in branches}
    for f in sorted(conflicts.keys()):
        parts.append(f"\n### `{f}`\n")
        seen_branches = set(conflicts[f])
        for role_name in seen_branches:
            b = branch_by_role.get(role_name)
            if not b:
                continue
            wt = Path(b["worktree"])
            file_in_wt = wt / f
            body = ""
            if file_in_wt.exists():
                try:
                    body = file_in_wt.read_text(encoding="utf-8", errors="replace")[:3000]
                except OSError:
                    pass
            parts.append(f"#### from `{role_name}` (branch `{b.get('branch')}`)")
            parts.append("```\n" + body + "\n```\n")
    parts.append("\n## Output contract\n")
    parts.append(
        "Write `CONFLICT_RESOLUTION.md` in the current directory with one "
        "`## Conflict: <path>` section per conflicting file. Each section "
        "ends with a single `Winner: <role>` line on its own line "
        "(parseable). Add a short `Rationale` per pick. Then write `DONE.md` "
        "with `status: COMPLETE`."
    )
    prompt_md = "\n".join(parts)

    out_path = tf / "CONFLICT_RESOLUTION.md"
    spawn = spawn_and_track(
        role=role, cwd=tf, prompt_md=prompt_md,
        prompt_path=tf / "prompts" / "conflict_arbiter_architect.md",
        log_path=tf / "logs" / "conflict_arbiter_architect.log",
        task_db_id=task_db_id, worktree_path=None, branch_name=None,
        result_path=out_path,
    )
    return {"role": role.name, "run_id": spawn.run_id,
            "output_path": str(out_path), "conflicts": list(conflicts.keys())}


def _write_team_retro(
    *, repo: Path, task_number: int, task_text: str, plan: dict,
    assignments: list[dict], cross_reviewers: list[dict],
) -> None:
    """Persist a per-task retro into memory.

    Writes:
    - memory/runs/task-NNNN.md — full snapshot.
    - memory/agent_lessons/<role>.md — one durable bullet per role,
      appended. This is what makes the next task's planner+specialists
      "feel smarter" — they read their own past lessons.
    """
    try:
        # Run-level retro
        body_parts = [
            f"# Task {task_number:04d} — retro",
            "",
            f"## Goal",
            task_text,
            "",
            f"## Plan",
            f"- {len(assignments)} specialists",
            f"- {len(cross_reviewers)} cross-VP reviewers",
            "",
            "## Outcomes",
        ]
        for a in assignments:
            body_parts.append(f"- **{a.get('role')}** ({a.get('vp')}) → {a.get('output')}")
        memory.write_run_summary(repo, task_number, "\n".join(body_parts))

        # Per-role lessons.
        for a in assignments:
            role = a.get("role")
            if not role:
                continue
            wt = a.get("worktree")
            result_summary = ""
            if wt:
                rp = Path(wt) / "RESULT.md"
                if rp.exists():
                    # Take the first paragraph as the "lesson" seed.
                    first = rp.read_text(encoding="utf-8", errors="replace").strip().split("\n\n", 1)[0]
                    result_summary = first[:280]
            lesson = (
                f"Task {task_number:04d}: brief={a.get('brief','')[:120]!r} "
                f"→ {result_summary or 'completed'}"
            )
            memory.append_lesson(repo, role, lesson)
    except Exception:
        # Retros are best-effort; never block merge on memory writes.
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _task_folder(repo: Path, task_number: int) -> Path:
    return repo / ".johnstudio" / "tasks" / f"task-{task_number:04d}"


def _team_state_path(tf: Path) -> Path:
    return tf / "TEAM_STATE.json"


def _read_team_state(tf: Path) -> dict:
    p = _team_state_path(tf)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_team_state(tf: Path, state: dict) -> None:
    # Atomic write: the ticker, the background spawn thread, and replan all write
    # this file concurrently. A bare write_text can be read half-written (→ the
    # reader gets {} and the task silently loses its state). temp + os.replace
    # makes every read see either the old or the new file, never a torn one.
    import os
    p = _team_state_path(tf)
    tmp = p.with_name(f".{p.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def _effort_for_model(model: str | None) -> str | None:
    """Right-size reasoning effort to the model tier: deep models think hard,
    fast models stay fast. Speed without sacrificing the work that needs depth."""
    m = (model or "").lower()
    if "opus" in m: return "high"
    if "sonnet" in m: return "medium"
    if "haiku" in m: return "low"
    return None


def _worker_cfg_for_role(role: team.Role) -> WorkerConfig:
    """Build a WorkerConfig from a Role markdown.

    Plumbs the role's declared `model:` and `tools:` straight into the
    WorkerConfig so the adapter can honor them. `Task` and `Agent` are
    blocked at catalog-load time, so an `allowed_tools` list here can be
    passed to the CLI without further filtering.
    """
    return WorkerConfig(
        provider=role.provider, command=role.provider, role=role.name,
        can_edit=role.can_edit, worktree=role.can_edit,
        max_runtime_minutes=45, always_available=False,
        model=role.model or None,
        effort=_effort_for_model(role.model),
        allowed_tools=list(role.tools) if role.tools else None,
        can_spawn_subagents=role.can_spawn_subagents,
    )


# ---------------------------------------------------------------------------
# spawn_and_track — the one launch seam used by every team-mode spawn site.
# ---------------------------------------------------------------------------
#
# Four different team-mode call sites all need the same shape: write a
# prompt, optionally create a worktree, launch the worker, insert a run
# row with the PID, start an event-stream tailer keyed on the run id,
# stagger to avoid the concurrent-IPC contention the parallel-mode
# orchestrator learned about. Before this seam each site re-derived all
# of that, with subtle bugs (e.g. cross-reviewers were inserting runs
# without PIDs until Commit B fixed the obvious sites individually).
#
# We deliberately scope this to team mode for now. orchestrator.run and
# chain.run_phase have their own duplication of the same shape but
# migrating them is a higher-risk refactor and is left for a separate
# focused pass. Once those migrate too, this becomes johnstudio.spawner.

def spawn_and_track(
    *,
    role: team.Role,
    cwd: Path,
    prompt_md: str,
    prompt_path: Path,
    log_path: Path,
    task_db_id: int,
    worktree_path: Path | None,
    branch_name: str | None,
    result_path: Path,
    phase_id: int | None = None,
) -> spawner.SpawnResult:
    """Team-mode-flavored convenience around `spawner.spawn` that builds
    the WorkerConfig from a Role markdown. Equivalent to constructing a
    SpawnRequest manually; preserved so existing callers don't churn."""
    wcfg = _worker_cfg_for_role(role)
    return spawner.spawn(spawner.SpawnRequest(
        worker_name=role.name, worker_cfg=wcfg,
        cwd=cwd, prompt_md=prompt_md, prompt_path=prompt_path, log_path=log_path,
        task_db_id=task_db_id,
        worktree_path=worktree_path, branch_name=branch_name,
        result_path=result_path,
        tmux_session=None, phase_id=phase_id,
    ))


def _project_for_task(task_db_id: int) -> tuple[str, Path, int] | None:
    conn = db.connect()
    try:
        row = conn.execute(
            """SELECT p.name AS project_name, p.repo_path, t.task_number
               FROM tasks t JOIN projects p ON p.id = t.project_id
               WHERE t.id = ?""",
            (task_db_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return row["project_name"], Path(row["repo_path"]), int(row["task_number"])


def _task_description(task_db_id: int) -> str:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT description FROM tasks WHERE id = ?", (task_db_id,)
        ).fetchone()
    finally:
        conn.close()
    return row["description"] if row else ""


def _insert_run_row(
    *, task_db_id: int, worker_name: str, provider: str, can_edit: bool,
    worktree_path: str | None, branch: str | None, prompt_path: str, result_path: str,
    pid: int | None = None,
) -> int:
    """Create a row in `runs` so the live graph picks the specialist up.

    Workers table is upserted to give the run a worker_id (mirrors the
    pattern used by orchestrator.run). `pid` is stored so a backend
    restart can find and kill orphaned worker subprocesses.
    """
    conn = db.connect()
    db.init_schema(conn)
    cur = conn.execute(
        """INSERT INTO workers (name, provider, role, command, can_edit, worktree_enabled)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(name) DO UPDATE SET
               provider = excluded.provider, role = excluded.role,
               command = excluded.command, can_edit = excluded.can_edit,
               worktree_enabled = excluded.worktree_enabled
           RETURNING id""",
        (worker_name, provider, worker_name, provider, 1 if can_edit else 0, 1 if can_edit else 0),
    )
    worker_id = int(cur.fetchone()["id"])
    cur = conn.execute(
        """INSERT INTO runs (task_id, worker_id, status, tmux_session, tmux_pane,
            worktree_path, branch_name, prompt_path, result_path, started_at, pid)
           VALUES (?,?,?,?,?,?,?,?,?, ?, ?) RETURNING id""",
        (
            task_db_id, worker_id, "launched", None, None,
            worktree_path, branch, prompt_path, result_path, _now(), pid,
        ),
    )
    rid = int(cur.fetchone()["id"])
    conn.commit()
    conn.close()
    return rid


def check_budget(task_db_id: int) -> dict:
    """Lift `worker_events.task_cost_status` so external callers (API,
    orchestrator pre-spawn check) don't have to import the events module.
    """
    return worker_events.task_cost_status(task_db_id)


# ---------------------------------------------------------------------------
# Item 10 — mid-flight cancellation
# ---------------------------------------------------------------------------

# Statuses a run can be in while it still has a live (or recently-live)
# specialist we should stop. Anything terminal (completed/stopped/killed/
# failed) is left untouched so cancel is idempotent.
_LIVE_RUN_STATUSES = ("launched", "running", "retrying")


def _kill_pid(pid: int | None) -> bool:
    """Best-effort SIGTERM (then SIGKILL) a worker PID. Returns True iff a
    signal was delivered to a live process."""
    if not pid:
        return False
    import os
    import signal
    if not _pid_alive(int(pid)):
        return False
    try:
        os.kill(int(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    # Give it a moment to exit, then SIGKILL if it ignored SIGTERM.
    for _ in range(10):
        if not _pid_alive(int(pid)):
            return True
        time.sleep(0.1)
    try:
        os.kill(int(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    return True


def _kill_tmux(session: str | None, pane: str | None) -> None:
    """Best-effort kill of a worker's tmux pane (or session). No-op when
    tmux isn't present or the target is already gone."""
    if not session:
        return
    try:
        from . import tmux_controller
        if not tmux_controller.is_available():
            return
        if pane:
            from . import utils
            target = f"{session}:.{pane}" if pane.startswith("%") else f"{session}:{pane}"
            utils.run(["tmux", "kill-pane", "-t", target])
        else:
            tmux_controller.kill_session(session)
    except Exception:
        pass


def cancel_team_task(task_db_id: int) -> dict:
    """Cancel a team task mid-flight: kill every live specialist subprocess
    (by PID and/or tmux pane), mark their runs 'stopped', mark the task
    'cancelled', and emit WORKER_KILLED per specialist.

    Idempotent + safe on an already-finished task: we only act on runs in a
    live status (launched/running/retrying). A second call (or a call on a
    task whose workers all already exited) finds zero live runs and returns
    {"cancelled": [...], "already_done": True/...} without touching anything
    terminal. Killing an already-dead PID / gone pane is a no-op.

    Returns {task_db_id, task_status, cancelled:[{run_id, worker, pid,
    killed}], count}.
    """
    proj_info = _project_for_task(task_db_id)
    if not proj_info:
        raise KeyError(f"task {task_db_id} not found")

    from .hooks import EventTypes, bus

    placeholders = ",".join("?" for _ in _LIVE_RUN_STATUSES)
    conn = db.connect()
    try:
        rows = conn.execute(
            f"""SELECT r.id, r.pid, r.tmux_session, r.tmux_pane, w.name AS worker
                FROM runs r JOIN workers w ON w.id = r.worker_id
                WHERE r.task_id = ? AND r.status IN ({placeholders})""",
            (task_db_id, *_LIVE_RUN_STATUSES),
        ).fetchall()
    finally:
        conn.close()

    cancelled: list[dict] = []
    for r in rows:
        run_id = int(r["id"])
        pid = r["pid"]
        killed = _kill_pid(pid)
        _kill_tmux(r["tmux_session"], r["tmux_pane"])
        # Mark stopped — guarded on live status so a concurrent natural exit
        # that already wrote a terminal status wins and we don't clobber it.
        conn = db.connect()
        try:
            conn.execute(
                f"""UPDATE runs SET status='stopped', finished_at=?
                    WHERE id=? AND status IN ({placeholders})""",
                (_now(), run_id, *_LIVE_RUN_STATUSES),
            )
            conn.commit()
        finally:
            conn.close()
        try:
            bus.emit(EventTypes.WORKER_KILLED, {
                "run_id": run_id,
                "task_id": task_db_id,
                "worker": r["worker"],
                "pid": int(pid) if pid else None,
                "reason": "task_cancelled",
            })
        except Exception:
            pass
        cancelled.append({
            "run_id": run_id, "worker": r["worker"],
            "pid": int(pid) if pid else None, "killed": killed,
        })

    # Mark the task cancelled (idempotent — fine to re-set).
    _set_task_status(task_db_id, "cancelled")
    # Reflect it in the on-disk team state too, mirroring other transitions.
    try:
        _proj_name, repo, task_number = proj_info
        tf = _task_folder(repo, task_number)
        state = _read_team_state(tf)
        state["status"] = "cancelled"
        state["cancelled_at"] = _now()
        _write_team_state(tf, state)
    except Exception:
        pass

    try:
        bus.emit(EventTypes.TASK_TRANSITIONED, {
            "task_id": task_db_id, "to": "cancelled",
            "cancelled_runs": len(cancelled),
        })
    except Exception:
        pass

    return {
        "task_db_id": task_db_id,
        "task_status": "cancelled",
        "cancelled": cancelled,
        "count": len(cancelled),
    }


# ---------------------------------------------------------------------------
# Autonomous loops (RFC review tier 3 — what makes it "feel like a team")
# ---------------------------------------------------------------------------

def run_plan_critic(task_db_id: int) -> dict:
    """Spawn the product-manager role to critique the lead planner's
    TEAM_PLAN.md. Writes `PLAN_CRITIQUE.md` to the task folder. If the
    critique's verdict is `revise`, we'll re-spawn the planner once with
    the critique inlined.

    Returns {launched, critique_path}. Idempotent: re-calling when a
    critique exists is a no-op.
    """
    proj_info = _project_for_task(task_db_id)
    if not proj_info:
        raise KeyError(f"task {task_db_id} not found")
    proj_name, repo, task_number = proj_info
    tf = _task_folder(repo, task_number)
    plan_path = tf / "TEAM_PLAN.md"
    if not plan_path.exists():
        return {"refused": True, "reason": "no plan yet"}
    crit_path = tf / "PLAN_CRITIQUE.md"
    if crit_path.exists():
        return {"already_done": True, "critique_path": str(crit_path)}

    catalog = team.load_role_catalog()
    if CRITIC_ROLE not in catalog:
        return {"refused": True, "reason": f"no {CRITIC_ROLE} role in catalog"}
    role = catalog[CRITIC_ROLE]

    prompt = _build_plan_critic_prompt(
        role=role, plan_md=plan_path.read_text(encoding="utf-8"),
        task_text=_task_description(task_db_id), task_number=task_number,
    )
    spawn = spawn_and_track(
        role=role, cwd=tf, prompt_md=prompt,
        prompt_path=tf / "prompts" / f"plan_critic_{CRITIC_ROLE}.md",
        log_path=tf / "logs" / f"plan_critic_{CRITIC_ROLE}.log",
        task_db_id=task_db_id, worktree_path=None, branch_name=None,
        result_path=crit_path,
    )
    return {"launched": True, "run_id": spawn.run_id, "critique_path": str(crit_path)}


def _build_plan_critic_prompt(*, role: team.Role, plan_md: str, task_text: str, task_number: int) -> str:
    return f"""{role.system_prompt.rstrip()}

---

# Plan critique — Task {task_number:04d}

You are reviewing a TEAM_PLAN.md produced by the lead planner. Decide
whether the plan is good enough to dispatch to specialists, or whether
the planner should revise.

## User request
{task_text}

## Plan to review
```markdown
{plan_md[:8000]}
```

## Output contract

Write `PLAN_CRITIQUE.md` in the current directory with:

- A first line: `## Verdict: approve` OR `## Verdict: revise`
- A **Strengths** section (2-4 bullets)
- A **Required changes** section (only if verdict=revise — numbered list)
- A **Coverage gaps** section — anything the plan misses (e.g. no tests,
  no docs, no security review on a sensitive change)

Then write `DONE.md` with `status: COMPLETE`. Do not write any other
files. Do not commit.
"""


def _spawn_revision(
    *, role: team.Role, original_assignment: dict, review_text: str,
    repo: Path, tf: Path, task_db_id: int, task_number: int,
    project_name: str, round: int,
) -> dict:
    """Spawn a revision pass for an editor specialist with the review
    feedback inlined. Uses the same worktree the original specialist
    edited.
    """
    catalog = team.load_role_catalog()
    if role.name not in catalog:
        return {"skipped": True, "reason": f"role {role.name!r} not in catalog"}
    wt = original_assignment.get("worktree")
    if not wt:
        return {"skipped": True, "reason": "no worktree to revise in"}
    wt_path = Path(wt)

    # Clear DONE.md so we can detect the new revision's completion.
    done = wt_path / "DONE.md"
    if done.exists():
        try: done.unlink()
        except OSError: pass

    prompt_md = f"""{role.system_prompt.rstrip()}

---

# Revision round {round} — Task {task_number:04d}

You are revising your earlier implementation in response to reviewer
feedback. Your original brief was:

> {original_assignment.get('brief', '')}

## Reviewer feedback (address every Required change)

{review_text[:8000]}

## Your scope

- Worktree (edit files inside only): `{wt_path}`
- Make the smallest change that addresses every numbered Required change
  above. Don't refactor unrelated code.

## Output contract

1. Apply the changes in your worktree.
2. `git add -A && git commit -m "revise round {round}: address feedback"`.
3. Rewrite `RESULT.md` to reflect the revised state.
4. Write `DONE.md` with `status: COMPLETE`.
"""
    spawn = spawn_and_track(
        role=role, cwd=wt_path, prompt_md=prompt_md,
        prompt_path=tf / "prompts" / f"revise_round{round}_{role.name}.md",
        log_path=tf / "logs" / f"revise_round{round}_{role.name}.log",
        task_db_id=task_db_id, worktree_path=wt_path,
        branch_name=_branch_for_worktree(repo, wt_path),
        result_path=wt_path / "RESULT.md",
    )
    return {"launched": True, "run_id": spawn.run_id, "role": role.name, "round": round}


def _check_needs_revision(state: dict, repo: Path, task_number: int) -> list[dict]:
    """Scan every CROSS_REVIEW*.md for `## Verdict: needs-changes`. For
    each, return {role_to_revise, review_text}. The implementer named
    in the original plan is the one we re-spawn (we don't try to be
    clever about which specific role to revise — the reviewer's feedback
    is meant for the editor whose work is being reviewed).
    """
    tf = _task_folder(repo, task_number)
    notes_dir = tf / "team_notes"
    findings: list[dict] = []
    if not notes_dir.exists():
        return findings
    # Find the editor specialists (can_edit) — they're the revision targets.
    catalog = team.load_role_catalog()
    editor_assignments = [
        a for a in (state.get("assignments") or [])
        if a.get("role") in catalog and catalog[a["role"]].can_edit
    ]
    if not editor_assignments:
        return findings
    for p in sorted(notes_dir.glob("CROSS_REVIEW*.md")):
        text = p.read_text(encoding="utf-8", errors="replace")
        verdict = chain_mod.parse_verdict(text)
        if verdict != chain_mod.Verdict.NEEDS_CHANGES:
            continue
        # Route the feedback to every editor assignment (simple v0 — full
        # mapping by file overlap is a follow-up).
        for a in editor_assignments:
            findings.append({"assignment": a, "review_text": text, "source": p.name})
    return findings


def _maybe_spawn_test_recovery(
    *, state: dict, tf: Path, repo: Path, task_db_id: int,
    task_number: int, project_name: str, round: int,
) -> dict | None:
    """If tests have already been run this round, no-op. Otherwise run
    them; if any failed, spawn one debugger + one revision pass per
    failing worktree and return a record of what we did.

    Returns None if tests passed or weren't run; otherwise the dict
    describing the spawned debug+revise pair.
    """
    # Avoid re-spawning on every advance tick — only act if there's no
    # active debug/revision still in progress.
    if _has_pending_recovery(state, tf):
        return None
    test_report = run_test_signal(state, repo, task_number)
    if test_report.get("skipped"):
        return None  # no test command configured → nothing to do
    results = test_report.get("results") or []
    failing = [r for r in results if not r.get("passed")]
    if not failing:
        return None

    catalog = team.load_role_catalog()
    if DEBUGGER_ROLE not in catalog:
        return None  # can't recover without a debugger role
    debugger = catalog[DEBUGGER_ROLE]
    spawned: list[dict] = []
    for fr in failing:
        wt = Path(fr["worktree"])
        # 1. Spawn the debugger to write DEBUG_REPORT.md.
        debug_path = wt / "DEBUG_REPORT.md"
        try:
            if (wt / "DONE.md").exists():
                (wt / "DONE.md").unlink()
        except OSError:
            pass
        debug_prompt = _build_debug_prompt(
            role=debugger, failing_commands=fr.get("commands", []),
            task_number=task_number, project_name=project_name, worktree=wt,
        )
        d_spawn = spawn_and_track(
            role=debugger, cwd=wt, prompt_md=debug_prompt,
            prompt_path=tf / "prompts" / f"debug_round{round}_{fr.get('role')}.md",
            log_path=tf / "logs" / f"debug_round{round}_{fr.get('role')}.log",
            task_db_id=task_db_id, worktree_path=None, branch_name=None,
            result_path=debug_path,
        )
        # 2. Spawn a revision of the failing implementer that consumes
        # the debug report. The revision worker runs concurrently with
        # the debugger; if DEBUG_REPORT.md doesn't exist yet it's
        # mentioned in the prompt as "may not be written yet — read it
        # if it exists."
        impl_role_name = fr.get("role")
        impl_role = catalog.get(impl_role_name) if impl_role_name else None
        if impl_role is None or not impl_role.can_edit:
            spawned.append({"debugger": d_spawn.run_id, "revision": None,
                            "reason": "no editor role to revise"})
            continue
        rev_prompt = _build_test_revision_prompt(
            role=impl_role, failing_commands=fr.get("commands", []),
            worktree=wt, debug_report_path=debug_path,
            round=round, task_number=task_number,
        )
        r_spawn = spawn_and_track(
            role=impl_role, cwd=wt, prompt_md=rev_prompt,
            prompt_path=tf / "prompts" / f"test_revise_round{round}_{impl_role.name}.md",
            log_path=tf / "logs" / f"test_revise_round{round}_{impl_role.name}.log",
            task_db_id=task_db_id, worktree_path=wt,
            branch_name=_branch_for_worktree(repo, wt),
            result_path=wt / "RESULT.md",
        )
        spawned.append({"role": impl_role_name, "debugger_run": d_spawn.run_id,
                        "revision_run": r_spawn.run_id,
                        "first_failing_cmd": fr.get("commands", [{}])[0].get("command")})
    return {"round": round, "spawned": spawned}


def _has_pending_recovery(state: dict, tf: Path) -> bool:
    """Don't re-spawn while a previous recovery round's revision is
    still working (DONE.md absent in any of the active worktrees that
    were re-spawned)."""
    loops = state.get("test_loops") or []
    if not loops:
        return False
    last = loops[-1]
    for s in last.get("spawned") or []:
        rid = s.get("revision_run")
        if rid is None:
            continue
        # Look up the worktree for that run.
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT worktree_path FROM runs WHERE id = ?", (rid,),
            ).fetchone()
        finally:
            conn.close()
        if not row or not row["worktree_path"]:
            continue
        if not (Path(row["worktree_path"]) / "DONE.md").exists():
            return True
    return False


def _build_debug_prompt(*, role: team.Role, failing_commands: list[dict],
                       task_number: int, project_name: str, worktree: Path) -> str:
    tails = []
    for c in failing_commands[:4]:
        if c.get("exit_code") == 0:
            continue
        tails.append(
            f"### `$ {c.get('command')}` (exit {c.get('exit_code')})\n"
            f"```\n{(c.get('stdout_tail') or '')[-1500:]}\n"
            f"{(c.get('stderr_tail') or '')[-1500:]}\n```"
        )
    failure_md = "\n\n".join(tails) or "_(no detail captured)_"
    return f"""{role.system_prompt.rstrip()}

---

# Test failure investigation — Task {task_number:04d} — project `{project_name}`

The implementer finished, but the project's tests do not pass inside
the worktree. Investigate root cause; do NOT fix.

## Worktree under investigation
`{worktree}`

## Failing test commands

{failure_md}

## Output contract

Write `DEBUG_REPORT.md` in the current directory with:

- **Symptom** — what's observed.
- **Root cause** — file:line, what's wrong.
- **Proposed fix** — small code sketch or description.
- **Confidence** — high / medium / low.

Then write `DONE.md` with `status: COMPLETE`. Do not write any other files.
"""


def _build_test_revision_prompt(*, role: team.Role, failing_commands: list[dict],
                                worktree: Path, debug_report_path: Path,
                                round: int, task_number: int) -> str:
    tails = []
    for c in failing_commands[:2]:
        if c.get("exit_code") == 0:
            continue
        tails.append(
            f"`$ {c.get('command')}` → exit {c.get('exit_code')}\n```\n"
            f"{(c.get('stderr_tail') or c.get('stdout_tail') or '')[-2000:]}\n```"
        )
    failure_md = "\n\n".join(tails) or "_(no detail)_"
    return f"""{role.system_prompt.rstrip()}

---

# Fix-the-tests revision round {round} — Task {task_number:04d}

Your previous implementation made the project's tests fail. A debugger
is concurrently writing `DEBUG_REPORT.md` to `{debug_report_path}` —
read it if it exists before deciding the fix; otherwise rely on the
failure tails below.

## Failure tails

{failure_md}

## Scope

- Worktree: `{worktree}` (edit files inside only).
- The fix should be minimal — address the failing test, no refactors.

## Output contract

1. Apply the fix.
2. Re-run the failing test command locally to confirm it now passes.
3. `git add -A && git commit -m "fix tests round {round}"`.
4. Rewrite `RESULT.md` to reflect the fix.
5. Write `DONE.md` with `status: COMPLETE`.
"""


def run_test_signal(state: dict, repo: Path, task_number: int) -> dict:
    """Run the project's configured test commands inside every editor
    specialist's worktree. Returns a per-worktree {passed, output}.

    Called from advance_team_task before declaring specialists done.
    On failure we DO transition to reviewing — but we mark
    `tests_failing: [...]` in state so the autonomous-loop layer can
    decide whether to spawn a debugger + revision.
    """
    proj = project_mod.get_project(state.get("project_name"))
    if not proj:
        return {"skipped": True, "reason": "no project"}
    pcfg = config.load_project_config(proj["repo_path"])
    if not pcfg.test_commands:
        return {"skipped": True, "reason": "no test_commands configured"}

    per_wt: list[dict] = []
    for a in state.get("assignments") or []:
        wt = a.get("worktree")
        if not wt:
            continue
        wt_path = Path(wt)
        if not wt_path.exists():
            continue
        out_for_wt: list[dict] = []
        for cmd in pcfg.test_commands:
            try:
                cp = subprocess.run(
                    shlex.split(cmd), cwd=str(wt_path), capture_output=True,
                    text=True, timeout=120, shell=False,
                )
                out_for_wt.append({
                    "command": cmd, "exit_code": cp.returncode,
                    "stdout_tail": (cp.stdout or "")[-2000:],
                    "stderr_tail": (cp.stderr or "")[-2000:],
                })
            except subprocess.TimeoutExpired:
                out_for_wt.append({"command": cmd, "exit_code": -1, "timeout": True})
        all_pass = all(c.get("exit_code") == 0 for c in out_for_wt)
        per_wt.append({"role": a.get("role"), "worktree": str(wt_path), "passed": all_pass, "commands": out_for_wt})
    return {"results": per_wt}


# ---------------------------------------------------------------------------
# Background ticker + startup recovery (RFC review #15)
# ---------------------------------------------------------------------------

_TICKER_STOP: threading.Event | None = None
_TICKER_THREAD: threading.Thread | None = None


def start_ticker(interval_seconds: float = 5.0) -> None:
    """Idempotent: launch a background thread that polls every team task
    in a non-terminal state and calls advance_team_task on it.

    Without this, the user must manually POST /advance every time a
    specialist writes DONE.md — defeating the "feels autonomous" goal.
    """
    global _TICKER_STOP, _TICKER_THREAD
    if _TICKER_THREAD and _TICKER_THREAD.is_alive():
        return
    _TICKER_STOP = threading.Event()
    _TICKER_THREAD = threading.Thread(
        target=_run_ticker, args=(_TICKER_STOP, interval_seconds),
        name="team-advance-ticker", daemon=True,
    )
    _TICKER_THREAD.start()


def stop_ticker() -> None:
    global _TICKER_STOP
    if _TICKER_STOP:
        _TICKER_STOP.set()


def _run_ticker(stop: threading.Event, interval: float) -> None:
    while not stop.is_set():
        try:
            _tick_once()
        except Exception:
            _log.exception("team ticker tick failed")
        stop.wait(interval)


def _tick_once() -> None:
    conn = db.connect()
    try:
        db.init_schema(conn)
        rows = conn.execute(
            """SELECT id, status FROM tasks
               WHERE status IN ('planning', 'running', 'reviewing')"""
        ).fetchall()
    finally:
        conn.close()
    for r in rows:
        tid = int(r["id"])
        try:
            advance_team_task(tid)
        except KeyError:
            # The task or its project no longer resolves (e.g. the project was
            # deregistered out from under it). Fail it so the ticker stops
            # re-selecting the same orphan every tick.
            _log.warning("ticker: task %s unresolvable (project gone?); marking failed", tid)
            _c = db.connect()
            try:
                _c.execute(
                    "UPDATE tasks SET status='failed', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (tid,),
                )
                _c.commit()
            finally:
                _c.close()
        except Exception:
            _log.exception("ticker advance failed for task %s", tid)


def recover_orphan_runs() -> dict:
    """Walk runs in status='launched' or 'running' whose log file still
    exists; re-attach a tailer so events flow again. Called from
    server.py at app creation.

    For runs whose backing PID is dead AND no DONE.md is present, mark
    them stopped — they were orphaned by the prior backend crash.
    """
    conn = db.connect()
    try:
        db.init_schema(conn)
        rows = conn.execute(
            """SELECT r.id, r.task_id, r.prompt_path, r.pid, r.worktree_path,
                      w.provider, t.task_number
               FROM runs r JOIN workers w ON w.id = r.worker_id
               JOIN tasks t ON t.id = r.task_id
               WHERE r.status IN ('launched', 'running')"""
        ).fetchall()
    finally:
        conn.close()

    reattached = 0
    marked_stopped = 0
    for r in rows:
        run_id = int(r["id"])
        prompt_path = r["prompt_path"] or ""
        if not prompt_path:
            continue
        # The .jsonl sits next to prompts/ at logs/<basename>.jsonl. The
        # prompt file is at .../prompts/<name>.md; convert to logs/<name>.jsonl.
        pp = Path(prompt_path)
        jsonl = pp.parent.parent / "logs" / (pp.stem + ".jsonl")
        pid = r["pid"]
        alive = pid is not None and _pid_alive(int(pid))
        if alive and jsonl.exists():
            worker_events.start_tailer(
                jsonl_path=jsonl, run_id=run_id, task_id=int(r["task_id"]),
                provider=str(r["provider"]),
            )
            reattached += 1
            continue
        if not alive:
            # Orphan — mark stopped so the UI doesn't show a forever-spinner.
            conn = db.connect()
            try:
                conn.execute(
                    "UPDATE runs SET status = 'stopped' WHERE id = ? AND status IN ('launched','running')",
                    (run_id,),
                )
                conn.commit()
            finally:
                conn.close()
            marked_stopped += 1
    return {"reattached": reattached, "marked_stopped": marked_stopped, "total_seen": len(rows)}


def _pid_alive(pid: int) -> bool:
    try:
        # Signal 0 doesn't actually deliver a signal; it just checks for
        # the process's existence + send permission.
        import os
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")
