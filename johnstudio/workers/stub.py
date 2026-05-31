"""terminal_stub worker.

Always available. Reads its assigned prompt file, writes the artifacts the
prompt asks for, then writes DONE.md. Designed for offline pipeline tests.

Invocation: `python -m johnstudio.workers.stub <prompt_path>`

Two execution modes are auto-detected from the prompt content:

1. **Parallel-siblings mode** (no `phase:` token in prompt) — writes RESULT.md
   + DONE.md, optionally commits a tiny demo file.

2. **Chain mode** (prompt header includes `phase: <name>`) — writes the
   phase-specific artifact (RFC.md / RFC_REVIEW.md / RESULT.md / REVIEW_<n>.md).
   Verdicts default to "approve" but can be overridden per-phase with env vars:
     STUB_RFC_VERDICT      = approve | needs-changes | reject
     STUB_REVIEW_VERDICT_1 = approve | needs-changes | reject  (round 1)
     STUB_REVIEW_VERDICT_2 = ...                               (round 2)
   These are the only "knobs" tests need to drive the full chain through every
   branch deterministically.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from .base import BaseWorker
from ..models import WorkerConfig


class TerminalStubWorker(BaseWorker):
    def build_command(
        self,
        prompt_path: Path,
        *,
        log_path: Path | None = None,
        depth: int = 0,
    ) -> list[str]:
        return [sys.executable, "-m", "johnstudio.workers.stub", str(prompt_path)]


# ---------------------------------------------------------------------------
# Phase detection from prompt text
# ---------------------------------------------------------------------------

PHASE_RE = re.compile(r"phase:\s*(rfc_drafting|rfc_review|implementing|reviewing|revising)", re.IGNORECASE)
ROUND_RE = re.compile(r"round\s+(\d+)", re.IGNORECASE)


def _detect_phase(prompt_text: str) -> tuple[str | None, int]:
    m = PHASE_RE.search(prompt_text)
    if not m:
        return None, 0
    rm = ROUND_RE.search(prompt_text)
    return m.group(1).lower(), int(rm.group(1)) if rm else 0


# ---------------------------------------------------------------------------
# Artifact writers
# ---------------------------------------------------------------------------

def _write_done(folder: Path) -> Path:
    done = folder / "DONE.md"
    done.write_text("status: COMPLETE\n", encoding="utf-8")
    return done


def _write_rfc(task_folder: Path) -> Path:
    p = task_folder / "RFC.md"
    p.write_text(
        "# RFC — stub-authored\n\n"
        "## Goal\nAdd the requested feature with minimal scope.\n\n"
        "## Proposed approach\nDo the simplest thing that meets the acceptance criteria.\n\n"
        "## Alternatives considered\nNone (stub).\n\n"
        "## Tradeoffs\nSimplicity > flexibility.\n\n"
        "## Risks\nNone material.\n\n"
        "## Acceptance criteria\n- [ ] Feature exists\n- [ ] No tests broken\n\n"
        "## Open questions\nNone (stub).\n",
        encoding="utf-8",
    )
    return p


def _write_rfc_review(task_folder: Path, verdict: str) -> Path:
    p = task_folder / "RFC_REVIEW.md"
    p.write_text(
        f"# RFC review — stub\n\n"
        f"## Verdict: {verdict}\n\n"
        "## Strengths\nClear goal.\n\n"
        "## Concerns\nNone material.\n\n"
        "## Required changes\n_(none)_\n\n"
        "## Notes for implementer\nProceed as drafted.\n",
        encoding="utf-8",
    )
    return p


def _write_stub_note(worktree: Path, message: str) -> None:
    note = worktree / "STUB_NOTE.md"
    existing = note.read_text(encoding="utf-8") if note.exists() else ""
    note.write_text(existing + f"\n* {message} @ {datetime.utcnow().isoformat(timespec='seconds')}\n",
                    encoding="utf-8")


def _commit_stub_note(worktree: Path, message: str) -> None:
    subprocess.run(["git", "-C", str(worktree), "config", "user.email", "stub@johnstudio.local"], check=False)
    subprocess.run(["git", "-C", str(worktree), "config", "user.name", "terminal_stub"], check=False)
    subprocess.run(["git", "-C", str(worktree), "add", "STUB_NOTE.md"], check=False)
    subprocess.run(
        ["git", "-C", str(worktree), "commit", "-qm", f"stub: {message}"],
        check=False,
    )


def _write_demo_change_and_commit(worktree: Path, *, message: str) -> None:
    _write_stub_note(worktree, message)
    _commit_stub_note(worktree, message)


def _write_implementer_result(worktree: Path) -> Path:
    p = worktree / "RESULT.md"
    p.write_text(
        "# RESULT — stub implementer\n\n"
        "## Summary\nImplemented the requested change.\n\n"
        "## Files changed\n- `STUB_NOTE.md`\n\n"
        "## Tests run\n_(none)_\n\n"
        "## Risks\nNone.\n\n"
        "## Blockers\nNone.\n\n"
        "## How this maps to the RFC acceptance criteria\nAll items satisfied (stub).\n",
        encoding="utf-8",
    )
    return p


def _write_parallel_result(worktree: Path) -> Path:
    """Original parallel-siblings RESULT.md — keeps the full section list expected by
    the existing `johnstudio run` flow."""
    p = worktree / "RESULT.md"
    p.write_text(
        "# RESULT — terminal_stub\n\n"
        "## Summary\nStub worker executed offline.\n\n"
        "## Files changed\n- `STUB_NOTE.md` (created)\n\n"
        "## Tests run\n_(none; stub worker)_\n\n"
        "## Risks\n_(none; demo file only)_\n\n"
        "## Blockers\n_(none)_\n\n"
        "## Handoff requests\n_(none)_\n\n"
        "## Skill feedback\n- terminal-stub: useful\n\n"
        "## New memory facts\n- terminal_stub completed successfully\n\n"
        "## Suggested tags/entities\n- #stub #test-run\n\n"
        "## Next recommended action\nRun `johnstudio collect` then `review`.\n",
        encoding="utf-8",
    )
    return p


def _write_review(worktree: Path, round: int, verdict: str) -> Path:
    p = worktree / f"REVIEW_{round}.md"
    body = [
        f"# Review round {round} — stub\n",
        f"## Verdict: {verdict}\n",
        "## Summary\nLooked at the diff.\n",
    ]
    if verdict == "needs-changes":
        body.append("## Required changes\n1. Add a comment explaining intent.\n")
    elif verdict == "approve":
        body.append("## Required changes\n_(none — approved)_\n")
    body.append("## Concerns vs. RFC acceptance criteria\nNone.\n")
    p.write_text("\n".join(body), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def _is_inside_git_worktree(p: Path) -> bool:
    cp = subprocess.run(
        ["git", "-C", str(p), "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True,
    )
    return cp.returncode == 0 and cp.stdout.strip() == "true"


def _is_chain_task_folder(p: Path) -> bool:
    return p.name.startswith("task-") and p.parent.name == "tasks"


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    if not argv:
        print("usage: python -m johnstudio.workers.stub <prompt_path>", file=sys.stderr)
        return 2
    prompt = Path(argv[0]).resolve()
    if not prompt.exists():
        print(f"prompt not found: {prompt}", file=sys.stderr)
        return 2

    cwd = Path(os.getcwd()).resolve()
    prompt_text = prompt.read_text(encoding="utf-8", errors="replace")
    phase, round = _detect_phase(prompt_text)

    if phase is None:
        # Parallel-siblings mode (original behavior).
        _write_stub_note(cwd, "demo")
        if _is_inside_git_worktree(cwd):
            _commit_stub_note(cwd, "demo")
        _write_parallel_result(cwd)
        _write_done(cwd)
        print(f"stub (parallel mode) completed in {cwd}")
        return 0

    # Chain mode
    if phase == "rfc_drafting":
        # cwd is the task folder (read-only).
        _write_rfc(cwd)
        _write_done(cwd)
    elif phase == "rfc_review":
        verdict = os.environ.get("STUB_RFC_VERDICT", "approve")
        _write_rfc_review(cwd, verdict)
        _write_done(cwd)
    elif phase == "implementing":
        if _is_inside_git_worktree(cwd):
            _write_demo_change_and_commit(cwd, message=f"implement (round {round})")
        _write_implementer_result(cwd)
        _write_done(cwd)
    elif phase == "reviewing":
        verdict = os.environ.get(f"STUB_REVIEW_VERDICT_{round}", "approve")
        _write_review(cwd, round, verdict)
        _write_done(cwd)
    elif phase == "revising":
        if _is_inside_git_worktree(cwd):
            _write_demo_change_and_commit(cwd, message=f"revise (round {round})")
        _write_implementer_result(cwd)
        _write_done(cwd)
    else:
        print(f"unknown phase: {phase}", file=sys.stderr)
        return 2

    print(f"stub (chain phase={phase} round={round}) completed in {cwd}")
    return 0


def make(worker_name: str, cfg: WorkerConfig) -> TerminalStubWorker:
    return TerminalStubWorker(worker_name, cfg)


if __name__ == "__main__":
    sys.exit(main())
