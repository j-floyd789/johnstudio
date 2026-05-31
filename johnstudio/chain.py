"""Chain mode: RFC → implement → review → revise → merge.

A bounded multi-agent workflow modelled on a real dev team's PR/RFC process.
Workers don't talk to each other; the chain runner brokers everything via
explicit artifacts (RFC.md, RFC_REVIEW.md, REVIEW_<n>.md, CONFLICT.md) and
human gates (RFC approval, final merge, conflict resolution).

Why a state machine and not coroutines: every phase is an opaque blocking call
into a CLI subprocess that may take 30s–5min. Modelling it as discrete states
in SQLite means the chain is fully resumable across server restarts and
fully inspectable in the UI.

Phase transitions (deterministic; no LLM in the loop):

    rfc_drafting
        ↓ (RFC.md exists, DONE.md present)
    rfc_review
        ↓ (RFC_REVIEW.md exists, has verdict)
    rfc_pending_approval                 ← human gate
        ↓ approve                            ↓ reject
    implementing                           rejected
        ↓ (all impl workers DONE)
    reviewing  (round=N)
        ↓ verdict=approve                ↓ verdict=needs-changes & N<max
    pending_merge                          revising (round=N+1)
        ↓ human gate                         ↓
    merged                                 → reviewing
                                          ↓ verdict=needs-changes & N>=max
                                        conflict                ← human gate
                                          ↓ merge | reject | revise-once
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from . import db


# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------

class Phase(str, Enum):
    RFC_DRAFTING = "rfc_drafting"
    RFC_REVIEW = "rfc_review"
    RFC_PENDING_APPROVAL = "rfc_pending_approval"
    IMPLEMENTING = "implementing"
    REVIEWING = "reviewing"
    REVISING = "revising"
    PENDING_MERGE = "pending_merge"
    CONFLICT = "conflict"
    MERGED = "merged"
    REJECTED = "rejected"


# Terminal states (chain done)
TERMINAL = {Phase.MERGED, Phase.REJECTED}

# Human-gated states (chain blocks here until a human acts)
HUMAN_GATES = {Phase.RFC_PENDING_APPROVAL, Phase.PENDING_MERGE, Phase.CONFLICT}


class Verdict(str, Enum):
    APPROVE = "approve"
    NEEDS_CHANGES = "needs-changes"
    REJECT = "reject"


# ---------------------------------------------------------------------------
# Verdict parser (deterministic — no LLM)
# ---------------------------------------------------------------------------

VERDICT_RE = re.compile(
    r"^##\s*Verdict\s*:?\s*(approve|needs[-\s]?changes|reject)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def parse_verdict(md: str) -> Verdict | None:
    """Find the first `## Verdict: <value>` line; case-insensitive; tolerates
    `needs-changes` / `needs changes` / `needschanges`.
    """
    m = VERDICT_RE.search(md)
    if not m:
        return None
    v = m.group(1).lower().replace(" ", "").replace("-", "")
    if v == "approve":
        return Verdict.APPROVE
    if v == "needschanges":
        return Verdict.NEEDS_CHANGES
    if v == "reject":
        return Verdict.REJECT
    return None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

@dataclass
class PhaseRow:
    id: int
    task_id: int
    phase: Phase
    round: int
    status: str
    artifact_path: str | None
    verdict: Verdict | None
    notes: str | None
    started_at: str | None
    completed_at: str | None


def _row_to_phase(row) -> PhaseRow:
    return PhaseRow(
        id=row["id"],
        task_id=row["task_id"],
        phase=Phase(row["phase"]),
        round=int(row["round"]),
        status=row["status"],
        artifact_path=row["artifact_path"],
        verdict=Verdict(row["verdict"]) if row["verdict"] else None,
        notes=row["notes"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
    )


def current_phase(task_id: int) -> PhaseRow | None:
    conn = db.connect()
    db.init_schema(conn)
    row = conn.execute(
        """SELECT * FROM task_phases WHERE task_id = ?
           ORDER BY id DESC LIMIT 1""",
        (task_id,),
    ).fetchone()
    conn.close()
    return _row_to_phase(row) if row else None


def list_phases(task_id: int) -> list[PhaseRow]:
    conn = db.connect()
    db.init_schema(conn)
    rows = conn.execute(
        "SELECT * FROM task_phases WHERE task_id = ? ORDER BY id",
        (task_id,),
    ).fetchall()
    conn.close()
    return [_row_to_phase(r) for r in rows]


def start_phase(
    task_id: int,
    phase: Phase,
    *,
    round: int = 0,
    notes: str | None = None,
) -> PhaseRow:
    conn = db.connect()
    db.init_schema(conn)
    cur = conn.execute(
        """INSERT INTO task_phases (task_id, phase, round, status, notes, started_at)
           VALUES (?,?,?,?,?,?) RETURNING *""",
        (task_id, phase.value, round, "running", notes, _now()),
    )
    row = cur.fetchone()
    conn.commit()
    conn.close()
    return _row_to_phase(row)


def complete_phase(
    phase_id: int,
    *,
    status: str,
    artifact_path: str | None = None,
    verdict: Verdict | None = None,
    notes: str | None = None,
) -> None:
    conn = db.connect()
    db.init_schema(conn)
    conn.execute(
        """UPDATE task_phases
           SET status = ?, artifact_path = ?, verdict = ?, notes = COALESCE(?, notes),
               completed_at = ?
           WHERE id = ?""",
        (status, artifact_path, verdict.value if verdict else None, notes, _now(), phase_id),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Transition logic (deterministic)
# ---------------------------------------------------------------------------

DEFAULT_MAX_REVISE_ROUNDS = 2


@dataclass
class Transition:
    """Decision the state machine made. Pure data."""

    next_phase: Phase | None        # None means stay in current state (awaiting work)
    round: int                      # round for the NEXT phase
    human_gate: bool                # True if next_phase is a gate; chain runner stops
    note: str = ""


def decide_next(
    current: PhaseRow,
    *,
    artifact_exists: bool,
    artifact_verdict: Verdict | None,
    max_revise_rounds: int = DEFAULT_MAX_REVISE_ROUNDS,
) -> Transition:
    """Given the current phase row and the latest artifact state, return the
    next transition. Pure function — easy to unit-test.
    """
    if current.phase == Phase.RFC_DRAFTING:
        if artifact_exists:
            return Transition(Phase.RFC_REVIEW, 0, False, "RFC drafted, route to reviewer")
        return Transition(None, current.round, False, "waiting for RFC.md")

    if current.phase == Phase.RFC_REVIEW:
        if artifact_exists and artifact_verdict is not None:
            return Transition(Phase.RFC_PENDING_APPROVAL, 0, True, f"RFC review verdict: {artifact_verdict.value}")
        if artifact_exists:
            return Transition(None, current.round, False, "RFC_REVIEW.md exists but no parseable verdict")
        return Transition(None, current.round, False, "waiting for RFC_REVIEW.md")

    if current.phase == Phase.RFC_PENDING_APPROVAL:
        # Resolved by approve_rfc / reject_rfc only.
        return Transition(None, current.round, True, "awaiting human RFC approval")

    if current.phase == Phase.IMPLEMENTING:
        if artifact_exists:
            return Transition(Phase.REVIEWING, 1, False, "implementation done, route to reviewer")
        return Transition(None, current.round, False, "waiting for implementer DONE")

    if current.phase == Phase.REVIEWING:
        if not artifact_exists:
            return Transition(None, current.round, False, "waiting for REVIEW_<n>.md")
        if artifact_verdict == Verdict.APPROVE:
            return Transition(Phase.PENDING_MERGE, current.round, True, "review approved")
        if artifact_verdict == Verdict.REJECT:
            return Transition(Phase.REJECTED, current.round, False, "reviewer rejected")
        if artifact_verdict == Verdict.NEEDS_CHANGES:
            if current.round >= max_revise_rounds:
                return Transition(Phase.CONFLICT, current.round, True,
                                  f"hit max revise rounds ({max_revise_rounds})")
            return Transition(Phase.REVISING, current.round, False,
                              f"needs-changes, round {current.round}/{max_revise_rounds}")
        return Transition(None, current.round, False, "REVIEW exists but no parseable verdict")

    if current.phase == Phase.REVISING:
        if artifact_exists:
            return Transition(Phase.REVIEWING, current.round + 1, False,
                              f"revision done, re-review (round {current.round + 1})")
        return Transition(None, current.round, False, "waiting for revised implementer DONE")

    if current.phase == Phase.PENDING_MERGE:
        return Transition(None, current.round, True, "awaiting human merge")

    if current.phase == Phase.CONFLICT:
        return Transition(None, current.round, True, "awaiting human conflict resolution")

    # Terminal
    return Transition(None, current.round, False, f"terminal: {current.phase.value}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Runner: launches a phase's worker(s) and updates DB state.
# ---------------------------------------------------------------------------

# Local imports kept inside functions to avoid a circular import with
# context_builder (which itself imports `chain`).

def task_folder(repo_path: str | Path, task_number: int) -> Path:
    return Path(repo_path) / ".johnstudio" / "tasks" / f"task-{task_number:04d}"


def _read_text(p: Path) -> str:
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8", errors="replace")


def expected_artifact(
    phase: Phase, round: int, *, task_folder: Path, worktree: Path | None
) -> tuple[Path, Path]:
    """Return (artifact_path, done_marker_path) for the given phase.

    - RFC_DRAFTING:  task_folder/RFC.md         + task_folder/DONE.md
    - RFC_REVIEW:    task_folder/RFC_REVIEW.md  + task_folder/DONE.md
    - IMPLEMENTING:  worktree/RESULT.md         + worktree/DONE.md
    - REVIEWING:     worktree/REVIEW_<N>.md     + worktree/DONE.md
    - REVISING:      worktree/RESULT.md         + worktree/DONE.md
    """
    if phase in (Phase.RFC_DRAFTING, Phase.RFC_REVIEW):
        name = "RFC.md" if phase == Phase.RFC_DRAFTING else "RFC_REVIEW.md"
        return (task_folder / name, task_folder / "DONE.md")
    if phase == Phase.REVIEWING:
        assert worktree is not None
        return (worktree / f"REVIEW_{round}.md", worktree / "DONE.md")
    if phase in (Phase.IMPLEMENTING, Phase.REVISING):
        assert worktree is not None
        return (worktree / "RESULT.md", worktree / "DONE.md")
    raise ValueError(f"No expected artifact for phase {phase}")


def _clear_done(done_path: Path) -> None:
    """Reset DONE.md so the runner can detect a fresh completion in the next phase."""
    try:
        done_path.unlink()
    except FileNotFoundError:
        pass


def begin_chain(
    *,
    project_name: str,
    task_text: str,
    architect_worker: str = "claude_review",
    rfc_reviewer_worker: str = "claude_review",
    implementer_worker: str = "claude_backend",
    reviewer_worker: str = "claude_review",
) -> dict:
    """Insert the task and the first phase (RFC_DRAFTING). Returns task info.

    Worker assignments are configurable so you can mix providers
    (e.g. implementer=claude, reviewer=gemini) once those CLIs are wired.
    """
    from . import config, project as project_mod

    proj = project_mod.get_project(project_name)
    if not proj:
        raise KeyError(f"Project not registered: {project_name}")
    pcfg = config.load_project_config(proj["repo_path"])

    conn = db.connect()
    db.init_schema(conn)

    # Reuse the same `tasks` row shape as parallel mode.
    cur = conn.execute(
        "SELECT COALESCE(MAX(task_number), 0) AS m FROM tasks WHERE project_id = ?",
        (proj["id"],),
    )
    task_number = int(cur.fetchone()["m"]) + 1
    cur = conn.execute(
        """INSERT INTO tasks (project_id, task_number, title, description, status, base_branch)
           VALUES (?,?,?,?,?,?) RETURNING id""",
        (proj["id"], task_number, task_text[:80], task_text, "running", pcfg.base_branch),
    )
    task_db_id = int(cur.fetchone()["id"])
    assignments = json.dumps({
        "architect": architect_worker,
        "rfc_reviewer": rfc_reviewer_worker,
        "implementer": implementer_worker,
        "reviewer": reviewer_worker,
        "project_name": project_name,
    })
    conn.execute(
        "INSERT OR REPLACE INTO chain_meta (task_id, assignments_json) VALUES (?,?)",
        (task_db_id, assignments),
    )
    conn.execute(
        """INSERT INTO task_phases (task_id, phase, round, status, started_at)
           VALUES (?,?,?,?,?)""",
        (task_db_id, Phase.RFC_DRAFTING.value, 0, "pending", _now()),
    )
    conn.commit()
    conn.close()

    # Scaffold the task folder so the architect (read-only) can write RFC.md into it.
    repo = Path(proj["repo_path"])
    tf = task_folder(repo, task_number)
    for sub in ("prompts", "results", "diffs", "test_results", "logs"):
        (tf / sub).mkdir(parents=True, exist_ok=True)
    (tf / "TASK.md").write_text(f"# Task {task_number:04d}\n\n{task_text}\n", encoding="utf-8")

    return {
        "task_db_id": task_db_id,
        "task_number": task_number,
        "project_name": project_name,
        "first_phase": Phase.RFC_DRAFTING.value,
    }


def _assignments(task_db_id: int) -> dict:
    """Read the worker assignments from the chain_meta table."""
    conn = db.connect()
    db.init_schema(conn)
    row = conn.execute(
        "SELECT assignments_json FROM chain_meta WHERE task_id = ?",
        (task_db_id,),
    ).fetchone()
    conn.close()
    if not row:
        return {}
    try:
        return json.loads(row["assignments_json"])
    except (json.JSONDecodeError, KeyError):
        return {}


def _worker_for_phase(phase: Phase, assignments: dict) -> str:
    return {
        Phase.RFC_DRAFTING: assignments.get("architect"),
        Phase.RFC_REVIEW: assignments.get("rfc_reviewer"),
        Phase.IMPLEMENTING: assignments.get("implementer"),
        Phase.REVIEWING: assignments.get("reviewer"),
        Phase.REVISING: assignments.get("implementer"),
    }.get(phase) or "claude_backend"


def _gather_prior_artifacts(
    phase: Phase, round: int, task_folder: Path, worktree: Path | None
) -> dict[str, str]:
    """Inline the relevant prior artifacts so the next agent has full context."""
    out: dict[str, str] = {}
    rfc = task_folder / "RFC.md"
    rfc_rev = task_folder / "RFC_REVIEW.md"

    if phase in (Phase.RFC_REVIEW,) and rfc.exists():
        out["RFC.md (to review)"] = _read_text(rfc)

    if phase in (Phase.IMPLEMENTING, Phase.REVIEWING, Phase.REVISING):
        if rfc.exists():
            out["Approved RFC"] = _read_text(rfc)
        if rfc_rev.exists():
            out["RFC review (apply this feedback)"] = _read_text(rfc_rev)

    if phase == Phase.REVIEWING and worktree:
        # Reviewer needs the diff + latest RESULT.md.
        from . import git_worktree as gw
        from . import config as cfg_mod, project as project_mod
        # cheap: just use git diff vs main; base_branch resolution happens here
        out["RESULT.md (implementer's)"] = _read_text(worktree / "RESULT.md")
        # Diff content
        # Try to find the project's base branch via the run record; fall back to "main".
        diff = gw.diff_against(worktree, base="main")
        if not diff.strip():
            diff = "(empty diff — implementer may have only added untracked files)"
        out["Diff vs base"] = "```diff\n" + diff[:20000] + "\n```"

    if phase == Phase.REVISING and worktree:
        # Reviser needs the latest REVIEW_<N>.md.
        # Round here is N+1 already incremented by decide_next; the reviewer wrote N.
        prev_review = worktree / f"REVIEW_{round}.md"  # most recent
        if prev_review.exists():
            out[f"REVIEW_{round}.md (address every required change)"] = _read_text(prev_review)
        # And the previous RESULT.md.
        if (worktree / "RESULT.md").exists():
            out["Previous RESULT.md"] = _read_text(worktree / "RESULT.md")

    return out


def run_phase(task_db_id: int, *, dry_run: bool = False) -> dict:
    """Run the CURRENT (pending) phase for a task.

    Sets up worktree if needed, builds the per-phase context pack, launches the
    worker as a subprocess, returns immediately with launch info.

    Caller (or `advance`) polls for DONE marker, then calls `complete_current_phase`.
    """
    from . import (
        config,
        context_builder,
        git_worktree as gw,
        project as project_mod,
        workers,
    )

    cur_phase = current_phase(task_db_id)
    if not cur_phase:
        raise RuntimeError(f"No phases for task {task_db_id}")
    if cur_phase.status == "running":
        return {"already_running": True, "phase": cur_phase.phase.value, "phase_id": cur_phase.id}
    if cur_phase.phase in TERMINAL or cur_phase.phase in HUMAN_GATES:
        return {"awaiting": True, "phase": cur_phase.phase.value}

    assignments = _assignments(task_db_id)
    project_name = assignments.get("project_name")
    proj = project_mod.get_project(project_name) if project_name else None
    if not proj:
        raise RuntimeError("Lost track of project name; chain corrupted")
    pcfg = config.load_project_config(proj["repo_path"])
    global_cfg = config.load_global_config()

    repo = Path(proj["repo_path"])
    # Task number
    conn = db.connect()
    trow = conn.execute(
        "SELECT task_number, title, description FROM tasks WHERE id = ?", (task_db_id,)
    ).fetchone()
    conn.close()
    task_number = int(trow["task_number"])
    tf = task_folder(repo, task_number)
    for sub in ("prompts", "results", "diffs", "test_results", "logs"):
        (tf / sub).mkdir(parents=True, exist_ok=True)

    # Worker + worktree setup
    worker_name = _worker_for_phase(cur_phase.phase, assignments)
    wcfg = global_cfg.workers[worker_name]
    worker = workers.make_worker(worker_name, wcfg)

    needs_worktree = cur_phase.phase in (Phase.IMPLEMENTING, Phase.REVISING)
    review_phase = cur_phase.phase == Phase.REVIEWING
    worktree_path: Path | None = None
    branch_name: str | None = None

    if needs_worktree or review_phase:
        # All implement/review phases share a single worktree per task: the
        # implementer's branch. Reviewer reads it; reviser writes to it.
        worktree_path = repo / ".johnstudio" / "worktrees" / f"task-{task_number:04d}-chain"
        branch_name = f"ai/task-{task_number:04d}/chain"
        if not worktree_path.exists() and needs_worktree:
            gw.add_worktree(repo, worktree_path, branch_name, base=pcfg.base_branch)

    # Build prompt
    prior = _gather_prior_artifacts(
        cur_phase.phase, cur_phase.round, tf, worktree_path if (needs_worktree or review_phase) else None
    )
    _, md = context_builder.build_phase_context_pack(
        phase=cur_phase.phase,
        round=cur_phase.round,
        project_cfg=pcfg,
        project_name=project_name,
        worker_name=worker_name,
        worker_cfg=wcfg,
        task_id=task_number,
        task_title=trow["title"],
        task_description=trow["description"],
        task_folder=tf,
        worktree_path=worktree_path if needs_worktree else None,
        prior_artifacts=prior,
    )
    prompt_path = tf / "prompts" / f"{cur_phase.phase.value}_round-{cur_phase.round}_{worker_name}.md"

    # Reviewer is read-only but still writes REVIEW_<n>.md inside the worktree
    # (next to the implementer's diff) — so its cwd must be the worktree too.
    cwd = worktree_path if (needs_worktree or review_phase) else tf
    log_path = tf / "logs" / f"{cur_phase.phase.value}_round-{cur_phase.round}.log"

    if dry_run:
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(md, encoding="utf-8")
        # Mark as running and return; tests can simulate the worker writing artifacts.
        _set_running(cur_phase.id)
        return {
            "phase": cur_phase.phase.value,
            "round": cur_phase.round,
            "worker": worker_name,
            "prompt_path": str(prompt_path),
            "worktree": str(worktree_path) if worktree_path else None,
            "task_folder": str(tf),
            "dry_run": True,
        }

    # Wipe any stale DONE.md so we can detect THIS phase's completion.
    if needs_worktree and worktree_path:
        _clear_done(worktree_path / "DONE.md")
    elif review_phase and worktree_path:
        _clear_done(worktree_path / "DONE.md")
    else:
        _clear_done(tf / "DONE.md")

    _set_running(cur_phase.id)

    # Funnel through the shared spawner — prompt write, worker launch,
    # run-row insert with PID, tailer start, stagger.
    from . import spawner
    artifact_paths = expected_artifact(
        cur_phase.phase, cur_phase.round, task_folder=tf,
        worktree=worktree_path if (needs_worktree or review_phase) else None,
    )
    result = spawner.spawn(spawner.SpawnRequest(
        worker_name=worker_name, worker_cfg=wcfg,
        cwd=cwd, prompt_md=md, prompt_path=prompt_path, log_path=log_path,
        task_db_id=task_db_id,
        worktree_path=worktree_path, branch_name=branch_name,
        result_path=artifact_paths[0],
        tmux_session=None, phase_id=cur_phase.id,
        stagger=False,   # chain mode runs one worker at a time
    ))
    return {
        "phase": cur_phase.phase.value,
        "round": cur_phase.round,
        "worker": worker_name,
        "prompt_path": str(prompt_path),
        "pid": result.pid,
        "worktree": str(worktree_path) if worktree_path else None,
        "task_folder": str(tf),
        "dry_run": False,
    }


def _set_running(phase_id: int) -> None:
    conn = db.connect()
    conn.execute("UPDATE task_phases SET status = ? WHERE id = ?", ("running", phase_id))
    conn.commit()
    conn.close()


def complete_current_phase_if_ready(task_db_id: int) -> dict:
    """Check whether the current phase's artifact + DONE.md exist; if so, finalize
    it and (if no human gate) start the next phase. Returns a status dict.
    """
    from . import config, project as project_mod

    cur_phase = current_phase(task_db_id)
    if not cur_phase:
        raise RuntimeError("no current phase")

    if cur_phase.phase in TERMINAL:
        return {"terminal": True, "phase": cur_phase.phase.value}

    if cur_phase.phase in HUMAN_GATES:
        return {"awaiting_human": True, "phase": cur_phase.phase.value}

    assignments = _assignments(task_db_id)
    proj = project_mod.get_project(assignments.get("project_name"))
    repo = Path(proj["repo_path"])
    conn = db.connect()
    trow = conn.execute("SELECT task_number FROM tasks WHERE id = ?", (task_db_id,)).fetchone()
    conn.close()
    task_number = int(trow["task_number"])
    tf = task_folder(repo, task_number)

    worktree_path: Path | None = None
    if cur_phase.phase in (Phase.IMPLEMENTING, Phase.REVIEWING, Phase.REVISING):
        worktree_path = repo / ".johnstudio" / "worktrees" / f"task-{task_number:04d}-chain"

    art_path, done_path = expected_artifact(
        cur_phase.phase, cur_phase.round,
        task_folder=tf, worktree=worktree_path,
    )
    if not done_path.exists() or not art_path.exists():
        return {"waiting": True, "expecting": str(art_path), "done": str(done_path)}

    # Parse verdict if applicable
    verdict = None
    if cur_phase.phase in (Phase.RFC_REVIEW, Phase.REVIEWING):
        verdict = parse_verdict(_read_text(art_path))

    transition = decide_next(
        cur_phase,
        artifact_exists=True,
        artifact_verdict=verdict,
    )

    complete_phase(
        cur_phase.id,
        status="completed",
        artifact_path=str(art_path),
        verdict=verdict,
        notes=transition.note,
    )

    # Update task status
    conn = db.connect()
    if transition.next_phase in (Phase.MERGED, Phase.REJECTED):
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (transition.next_phase.value, task_db_id))
    elif transition.next_phase:
        # Insert the next phase row
        conn.execute(
            """INSERT INTO task_phases (task_id, phase, round, status, started_at)
               VALUES (?,?,?,?,?)""",
            (task_db_id, transition.next_phase.value, transition.round, "pending", _now()),
        )
    conn.commit()
    conn.close()

    return {
        "completed": cur_phase.phase.value,
        "verdict": verdict.value if verdict else None,
        "next": transition.next_phase.value if transition.next_phase else None,
        "human_gate": transition.human_gate,
        "note": transition.note,
    }


# ---------------------------------------------------------------------------
# Human gates
# ---------------------------------------------------------------------------

def approve_rfc(task_db_id: int, *, note: str | None = None) -> dict:
    cur = current_phase(task_db_id)
    if not cur or cur.phase != Phase.RFC_PENDING_APPROVAL:
        raise RuntimeError(f"not awaiting RFC approval (current: {cur.phase.value if cur else 'none'})")
    complete_phase(cur.id, status="completed", verdict=Verdict.APPROVE, notes=note)
    conn = db.connect()
    conn.execute(
        """INSERT INTO task_phases (task_id, phase, round, status, started_at)
           VALUES (?,?,?,?,?)""",
        (task_db_id, Phase.IMPLEMENTING.value, 0, "pending", _now()),
    )
    conn.commit()
    conn.close()
    return {"approved": True}


def reject_rfc(task_db_id: int, *, reason: str | None = None) -> dict:
    cur = current_phase(task_db_id)
    if not cur or cur.phase != Phase.RFC_PENDING_APPROVAL:
        raise RuntimeError("not awaiting RFC approval")
    complete_phase(cur.id, status="completed", verdict=Verdict.REJECT, notes=reason)
    conn = db.connect()
    conn.execute(
        """INSERT INTO task_phases (task_id, phase, round, status, started_at)
           VALUES (?,?,?,?,?)""",
        (cur.task_id, Phase.REJECTED.value, 0, "completed", _now()),
    )
    conn.execute("UPDATE tasks SET status = 'rejected' WHERE id = ?", (cur.task_id,))
    conn.commit()
    conn.close()
    return {"rejected": True, "reason": reason}


def mark_merged(task_db_id: int) -> dict:
    """Called by the merger after a chain task's branch lands on base."""
    cur = current_phase(task_db_id)
    if not cur:
        return {"ok": False}
    if cur.phase in (Phase.PENDING_MERGE, Phase.CONFLICT):
        complete_phase(cur.id, status="completed", notes="merged by human")
        conn = db.connect()
        conn.execute(
            """INSERT INTO task_phases (task_id, phase, round, status, started_at, completed_at)
               VALUES (?,?,?,?,?,?)""",
            (cur.task_id, Phase.MERGED.value, 0, "completed", _now(), _now()),
        )
        conn.execute("UPDATE tasks SET status = 'merged' WHERE id = ?", (cur.task_id,))
        conn.commit()
        conn.close()
    return {"ok": True}


def reject_task(task_db_id: int, *, reason: str | None = None) -> dict:
    cur = current_phase(task_db_id)
    if not cur:
        return {"ok": False}
    complete_phase(cur.id, status="completed", verdict=Verdict.REJECT, notes=reason)
    conn = db.connect()
    conn.execute(
        """INSERT INTO task_phases (task_id, phase, round, status, started_at, completed_at)
           VALUES (?,?,?,?,?,?)""",
        (cur.task_id, Phase.REJECTED.value, 0, "completed", _now(), _now()),
    )
    conn.execute("UPDATE tasks SET status = 'rejected' WHERE id = ?", (cur.task_id,))
    conn.commit()
    conn.close()
    return {"ok": True, "rejected": True}
