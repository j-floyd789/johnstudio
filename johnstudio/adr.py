"""Architecture Decision Records (ADRs) over the Markdown memory vault.

An ADR is a short, immutable note capturing one significant decision: the
context that forced it, the decision itself, and the consequences. We store them
as `adr-NNNN-<slug>.md` inside the vault's existing `decisions/` folder so they
are versioned with the project and indexed by `rag.py` for free (no new
folder, no change to the retrieval index).

The `adr-scribe` role authors these in prose; this module is the deterministic
storage layer it (and the iteration arc) write through, so numbering and the
on-disk template stay consistent regardless of which agent emits an ADR.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import memory, utils

STATUSES: tuple[str, ...] = ("proposed", "accepted", "rejected", "superseded", "deprecated")

_ADR_FILE_RE = re.compile(r"^adr-(\d+)-")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(title: str) -> str:
    slug = _SLUG_RE.sub("-", title.lower()).strip("-")
    return slug or "decision"


@dataclass
class ADR:
    """One architecture decision record."""

    number: int
    title: str
    status: str = "accepted"
    context: str = "_TBD_"
    decision: str = "_TBD_"
    consequences: str = "_TBD_"
    date: str = field(default_factory=lambda: datetime.utcnow().date().isoformat())
    tags: list[str] = field(default_factory=list)

    @property
    def slug(self) -> str:
        return _slugify(self.title)

    @property
    def filename(self) -> str:
        return f"adr-{self.number:04d}-{self.slug}.md"

    def render(self) -> str:
        tags = ", ".join(self.tags) if self.tags else "_none_"
        return (
            f"# ADR {self.number:04d}: {self.title}"
            f"\n\n- **Status:** {self.status}"
            f"\n- **Date:** {self.date}"
            f"\n- **Tags:** {tags}"
            f"\n\n## Context\n\n{self.context or '_TBD_'}"
            f"\n\n## Decision\n\n{self.decision or '_TBD_'}"
            f"\n\n## Consequences\n\n{self.consequences or '_TBD_'}"
            "\n"
        )


def adr_dir(repo_path: str | Path) -> Path:
    """ADRs live alongside other decisions in the vault."""
    return memory.memory_root(repo_path) / "decisions"


def list_adrs(repo_path: str | Path) -> list[Path]:
    """All ADR files in the vault, sorted by ADR number ascending."""
    d = adr_dir(repo_path)
    if not d.exists():
        return []
    numbered: list[tuple[int, Path]] = []
    for p in d.glob("adr-*.md"):
        m = _ADR_FILE_RE.match(p.name)
        if m:
            numbered.append((int(m.group(1)), p))
    return [p for _n, p in sorted(numbered, key=lambda t: t[0])]


def next_number(repo_path: str | Path) -> int:
    """Next sequential ADR number (max existing + 1, starting at 1)."""
    d = adr_dir(repo_path)
    if not d.exists():
        return 1
    highest = 0
    for p in d.glob("adr-*.md"):
        m = _ADR_FILE_RE.match(p.name)
        if m:
            highest = max(highest, int(m.group(1)))
    return highest + 1


def write_adr(
    repo_path: str | Path,
    title: str,
    *,
    status: str = "accepted",
    context: str = "_TBD_",
    decision: str = "_TBD_",
    consequences: str = "_TBD_",
    tags: list[str] | None = None,
    overwrite: bool = False,
) -> Path:
    """Author and persist a new ADR. Returns the written file path."""
    d = adr_dir(repo_path)
    d.mkdir(parents=True, exist_ok=True)
    adr = ADR(
        number=next_number(repo_path),
        title=title,
        status=status,
        context=context,
        decision=decision,
        consequences=consequences,
        tags=list(tags or []),
    )
    path = d / adr.filename
    utils.write_text(path, adr.render(), overwrite=overwrite)
    return path
