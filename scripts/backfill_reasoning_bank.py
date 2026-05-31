#!/usr/bin/env python3
"""Backfill the ReasoningBank from completed tasks on disk.

Walks every registered project's ``.johnstudio/tasks/task-XXXX`` folder
and, for every task that has a ``DONE.md``, summarises ``TASK.md`` and
``DONE.md`` into a ``reasoning_bank.record_task`` call.

Idempotent — same task_number UPSERTs, so re-running is safe (but will
re-embed). ``--dry-run`` prints what WOULD be written without touching
the vector store or DB and is the right thing to do before the first
real backfill on a server that has Ollama queued up.

EMBEDDINGS ARE LOCAL ONLY (Ollama). NO paid API calls.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# Make the package importable when running from a clean checkout.
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from johnstudio import project as project_mod  # noqa: E402
from johnstudio import reasoning_bank as rb_mod  # noqa: E402

_TASK_RE = re.compile(r"^task-(\d+)$")
_SUMMARY_MAX_CHARS = 1200


@dataclass(frozen=True)
class BackfillRow:
    project_id: int
    project_name: str
    task_number: int
    goal: str
    outcome: str
    approach_summary: str
    tags: list[str]


def _first_non_blank_block(text: str, *, max_chars: int = _SUMMARY_MAX_CHARS) -> str:
    """Return the first meaningful paragraph (or up to max_chars)."""
    text = text.strip()
    if not text:
        return ""
    # Drop the leading H1 if present, then take the first paragraph.
    lines = text.splitlines()
    cleaned: list[str] = []
    for ln in lines:
        if ln.strip().startswith("# ") and not cleaned:
            continue  # skip top-level title
        cleaned.append(ln)
    body = "\n".join(cleaned).strip()
    # Split on blank line for "first paragraph".
    para = re.split(r"\n\s*\n", body, maxsplit=1)[0].strip()
    if not para:
        para = body
    return para[:max_chars]


def _outcome_from_done(done_text: str) -> str:
    """Extract an outcome label from a DONE.md body.

    Convention varies, so we use heuristics: look for `status:` line,
    `outcome:` line, or a Brier/edge_found phrase. Fall back to "complete".
    """
    lower = done_text.lower()
    m = re.search(r"^\s*outcome\s*:\s*(.+)$", done_text, re.IGNORECASE | re.MULTILINE)
    if m:
        return m.group(1).strip()[:120]
    m = re.search(r"^\s*status\s*:\s*(.+)$", done_text, re.IGNORECASE | re.MULTILINE)
    if m:
        return m.group(1).strip()[:120]
    if "edge_found=false" in lower or "no edge" in lower:
        return "edge_found=false"
    if "edge_found=true" in lower or "edge found" in lower:
        return "edge_found=true"
    return "complete"


def _goal_from_task(task_text: str) -> str:
    """First line of TASK.md (or the H1 if present), trimmed."""
    for line in task_text.splitlines():
        s = line.strip().lstrip("#").strip()
        if s:
            return s[:240]
    return "(no TASK.md content)"


def _tags_from_task_folder(task_folder: Path) -> list[str]:
    """Cheap tag extraction — arc/sweep folder hints."""
    tags: list[str] = []
    name = task_folder.name
    tags.append(name)
    return tags


def _iter_completed_tasks(repo_path: Path) -> Iterable[tuple[int, Path]]:
    tasks_root = repo_path / ".johnstudio" / "tasks"
    if not tasks_root.exists():
        return
    for child in sorted(tasks_root.iterdir()):
        if not child.is_dir():
            continue
        m = _TASK_RE.match(child.name)
        if not m:
            continue
        if not (child / "DONE.md").exists():
            continue
        yield int(m.group(1)), child


def _collect_rows() -> list[BackfillRow]:
    rows: list[BackfillRow] = []
    for proj in project_mod.list_projects():
        repo = Path(proj["repo_path"])
        if not repo.exists():
            continue
        for task_number, tf in _iter_completed_tasks(repo):
            task_md = tf / "TASK.md"
            done_md = tf / "DONE.md"
            try:
                task_text = task_md.read_text(encoding="utf-8") if task_md.exists() else ""
                done_text = done_md.read_text(encoding="utf-8")
            except OSError:
                continue
            goal = _goal_from_task(task_text) if task_text else f"task-{task_number:04d}"
            outcome = _outcome_from_done(done_text)
            approach = _first_non_blank_block(done_text) or _first_non_blank_block(task_text) or "(no summary)"
            rows.append(BackfillRow(
                project_id=int(proj["id"]),
                project_name=str(proj.get("name") or proj.get("repo_path") or "?"),
                task_number=task_number,
                goal=goal,
                outcome=outcome,
                approach_summary=approach,
                tags=_tags_from_task_folder(tf),
            ))
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be written; do not call Ollama or touch the DB.",
    )
    ap.add_argument(
        "--limit", type=int, default=0,
        help="Cap rows processed (0 = no cap).",
    )
    args = ap.parse_args(argv)

    rows = _collect_rows()
    if args.limit > 0:
        rows = rows[: args.limit]

    print(f"# Backfill plan — {len(rows)} completed task(s) found")
    if args.dry_run:
        for r in rows:
            print(f"- project_id={r.project_id} task={r.task_number:04d} outcome={r.outcome!r}")
            print(f"    goal: {r.goal}")
            first_line = r.approach_summary.splitlines()[0] if r.approach_summary else ""
            print(f"    approach[0]: {first_line[:200]}")
            print(f"    tags: {r.tags}")
        print("# DRY RUN — no embeddings called, no DB writes.")
        return 0

    # Live mode: requires Ollama.
    written = 0
    failed = 0
    by_project: dict[int, rb_mod.ReasoningBank] = {}
    try:
        for r in rows:
            bank = by_project.get(r.project_id)
            if bank is None:
                bank = rb_mod.ReasoningBank(project_id=r.project_id)
                by_project[r.project_id] = bank
            try:
                bank.record_task(
                    task_number=r.task_number,
                    goal=r.goal,
                    outcome=r.outcome,
                    approach_summary=r.approach_summary,
                    tags=r.tags,
                )
                written += 1
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"  ! task-{r.task_number:04d} failed: {e}")
    finally:
        for bank in by_project.values():
            bank.close()
    print(f"# Done — wrote {written}, failed {failed}.")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
