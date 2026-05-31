"""BuildlogAppendWorker — auto-append one line to docs/BUILDLOG.md.

The Khalshi project (and others) keep a `docs/BUILDLOG.md` file with
one line per session. Today an operator has to remember to append it
by hand after each arc iteration completes. This worker fires on
`arc.iter_complete`, reads the iteration's DONE.md plus a short
summary from the arc STATE.json, and appends one line.

Skip rules (BOTH must hold for a write):
  - `docs/BUILDLOG.md` exists. (We never CREATE a buildlog — if a
    project doesn't have one, that's intentional.)
  - The last non-empty line of the buildlog does NOT already mention
    this iteration's task number. (Idempotency: if the worker runs
    twice for the same event, we don't duplicate the line.)

Payload contract: we read `project_repo` (resolved via the same helper
as the other workers), `iter` (1-based iteration number), `arc_name`,
plus we open `<repo>/.johnstudio/arcs/<arc_name>/STATE.json` to pick up
the latest iteration's `task_number`, `reason`, and DONE.md path.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from ..background_workers import BackgroundWorker
from .status_regen import _resolve_repo

_log = logging.getLogger("johnstudio.workers_bg.buildlog_append")


def _read_state(repo: Path, arc_name: str) -> dict | None:
    p = repo / ".johnstudio" / "arcs" / arc_name / "STATE.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        _log.exception("buildlog-append: failed to parse %s", p)
        return None


def _done_md_summary(done_md_path: Path) -> str:
    """Pull a short one-line summary from DONE.md. Empty string if absent."""
    if not done_md_path.exists():
        return ""
    try:
        text = done_md_path.read_text(encoding="utf-8")
    except Exception:
        return ""
    # First non-empty non-heading line wins.
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Strip markdown list bullets.
        line = re.sub(r"^[-*]\s+", "", line)
        return line[:200]
    return ""


def _last_nonempty_line(p: Path) -> str:
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        return ""
    for ln in reversed(text.splitlines()):
        s = ln.strip()
        if s:
            return s
    return ""


class BuildlogAppendWorker(BackgroundWorker):
    name = "buildlog-append"
    events = ["arc.iter_complete"]
    throttle_seconds = 5

    def handle(self, event: str, payload: dict) -> None:
        repo = _resolve_repo(payload)
        if repo is None:
            _log.info(
                "buildlog-append: no repo resolvable from payload keys=%s; skipping",
                sorted(payload.keys()),
            )
            return

        buildlog = repo / "docs" / "BUILDLOG.md"
        if not buildlog.exists():
            _log.debug("buildlog-append: %s does not exist; skipping", buildlog)
            return

        arc_name = payload.get("arc_name")
        if not arc_name:
            _log.info("buildlog-append: payload missing arc_name; skipping")
            return

        state = _read_state(repo, str(arc_name))
        if not state:
            _log.info("buildlog-append: no STATE.json for arc %s; skipping", arc_name)
            return

        iters = state.get("iterations") or []
        if not iters:
            _log.info("buildlog-append: no iterations in state; skipping")
            return

        # Prefer the iter in the payload; else last in state.
        target_iter = payload.get("iter")
        latest = None
        if target_iter is not None:
            for it in iters:
                if it.get("iter") == target_iter:
                    latest = it
                    break
        if latest is None:
            latest = iters[-1]

        task_number = latest.get("task_number")
        if task_number is None:
            _log.info("buildlog-append: iteration has no task_number; skipping")
            return
        task_tag = f"task-{int(task_number):04d}"

        # Idempotency: skip if the last line already mentions THIS task.
        last_line = _last_nonempty_line(buildlog)
        if task_tag in last_line:
            _log.debug(
                "buildlog-append: %s already in last line of buildlog; skipping",
                task_tag,
            )
            return

        # Build the one-line entry.
        done_md = latest.get("artifact_path")
        summary = ""
        if done_md:
            done_path = Path(str(done_md))
            if not done_path.is_absolute():
                done_path = repo / done_path
            summary = _done_md_summary(done_path)
        if not summary:
            summary = str(latest.get("reason") or "iteration complete")[:200]

        date = datetime.utcnow().strftime("%Y-%m-%d")
        line = f"- {date} {task_tag} ({arc_name} iter {latest.get('iter')}): {summary}\n"

        # Append; ensure a leading newline only if file doesn't end in one.
        existing = buildlog.read_text(encoding="utf-8")
        prefix = "" if existing.endswith("\n") or not existing else "\n"
        with buildlog.open("a", encoding="utf-8") as f:
            f.write(prefix + line)
