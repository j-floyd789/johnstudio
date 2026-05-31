"""Dependency-free retrieval over the Markdown memory vault.

The memory vault (see `memory.py`) is a tree of Obsidian-style Markdown files:
project_brief.md, current_state.md, decisions/, bugs/, runs/, agent_lessons/, …

Until now the context builder pasted `current_state.md` / `project_brief.md`
verbatim (truncated to a char budget) into every agent's prompt. That ignores
the bulk of accumulated knowledge — past run summaries, decisions and bug
write-ups — and silently drops anything past the truncation point.

This module adds a small RAG (retrieval-augmented generation) layer: it chunks
the whole vault by Markdown section, ranks chunks against a query with BM25, and
returns the most relevant snippets. No embeddings, no network, no extra deps —
BM25 over tokenized text is deterministic, fast on vaults of this size, and good
enough to surface the right decision/bug/run note for a given task.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import memory

_INDEXED_DIRS: tuple[str, ...] = (
    "",
    "decisions",
    "bugs",
    "runs",
    "summaries",
    "handoffs",
    "agent_lessons",
)
_TOKEN_RE = re.compile("[a-z0-9]+")
_STOPWORDS: frozenset[str] = frozenset(
    "a an and are as at be but by для for from has have if in into is it its "
    "no not of on or s so t that the their then there these this to was were "
    "will with you your we our".split()
)
_K1 = 1.5
_B = 0.75
_MAX_CHUNK_CHARS = 1200


def _tokenize(text: str) -> list[str]:
    return [
        t
        for t in _TOKEN_RE.findall(text.lower())
        if len(t) > 1 and t not in _STOPWORDS
    ]


@dataclass
class Chunk:
    """A single retrievable section of the vault."""

    source: str
    heading: str
    text: str
    tokens: list[str] = field(default_factory=list, repr=False)


def _split_sections(raw: str) -> list[tuple[str, str]]:
    """Split Markdown into (heading, body) sections on ATX headings.

    Text before the first heading is returned under an empty heading.
    """
    lines = raw.splitlines()
    sections: list[tuple[str, list[str]]] = []
    current_heading = ""
    current: list[str] = []
    for line in lines:
        if line.lstrip().startswith("#"):
            if current:
                sections.append((current_heading, current))
            current_heading = line.lstrip("#").strip()
            current = [line]
            continue
        current.append(line)
    if current:
        sections.append((current_heading, current))
    return [(h, "\n".join(body).strip()) for h, body in sections]


def iter_chunks(repo_path: str | Path) -> list[Chunk]:
    """Walk the indexed vault folders and produce one Chunk per Markdown section."""
    root = memory.memory_root(repo_path)
    chunks: list[Chunk] = []
    if not root.exists():
        return chunks
    seen: set[Path] = set()
    for d in _INDEXED_DIRS:
        base = root / d if d else root
        if not base.exists():
            continue
        pattern = "**/*.md" if d else "*.md"
        for path in sorted(base.glob(pattern)):
            if not path.is_file() or path in seen:
                continue
            seen.add(path)
            rel = path.relative_to(root).as_posix()
            try:
                raw = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for heading, body in _split_sections(raw):
                if not body.strip():
                    continue
                text = body[:_MAX_CHUNK_CHARS]
                chunks.append(
                    Chunk(
                        source=rel,
                        heading=heading or path.stem,
                        text=text,
                        tokens=_tokenize(text),
                    )
                )
    return chunks


class MemoryIndex:
    """In-memory BM25 index over vault chunks.

    Built once per context-pack render; cheap enough that caching isn't worth
    the staleness risk on a vault that agents are actively writing to.
    """

    def __init__(self, chunks: list[Chunk]):
        self.chunks = chunks
        self.N = len(chunks)
        self._df: dict[str, int] = {}
        self._tf: list[dict[str, int]] = []
        total_len = 0
        for c in chunks:
            counts: dict[str, int] = {}
            for tok in c.tokens:
                counts[tok] = counts.get(tok, 0) + 1
            self._tf.append(counts)
            total_len += len(c.tokens)
            for tok in counts:
                self._df[tok] = self._df.get(tok, 0) + 1
        self.avgdl = (total_len / self.N) if self.N else 0

    def _idf(self, term: str) -> float:
        df = self._df.get(term, 0)
        return math.log(1 + (self.N - df + 0.5) / (df + 0.5))

    def search(self, query: str, k: int = 5) -> list[tuple[Chunk, float]]:
        q_terms = _tokenize(query)
        if not q_terms or self.N == 0:
            return []
        scored: list[tuple[Chunk, float]] = []
        for i, chunk in enumerate(self.chunks):
            counts = self._tf[i]
            dl = len(chunk.tokens) or 1
            score = 0
            for term in q_terms:
                tf = counts.get(term, 0)
                if tf == 0:
                    continue
                idf = self._idf(term)
                denom = tf + _K1 * (1 - _B + _B * dl / (self.avgdl or 1))
                score += idf * (tf * (_K1 + 1)) / denom
            if score > 0:
                scored.append((chunk, score))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:k]


def retrieve(repo_path: str | Path, query: str, k: int = 5) -> list[tuple[Chunk, float]]:
    """Convenience: build an index over the vault and return the top-k chunks."""
    return MemoryIndex(iter_chunks(repo_path)).search(query, k=k)


def render_snippets(
    results: list[tuple[Chunk, float]], *, max_chars: int = 3000
) -> str:
    """Render retrieval results as a Markdown block for a context pack.

    Returns an empty string when there is nothing relevant so callers can omit
    the section entirely.
    """
    if not results:
        return ""
    blocks: list[str] = []
    used = 0
    for chunk, score in results:
        body = chunk.text.strip()
        block = f"**`{chunk.source}` — {chunk.heading}** (score {score:.1f})\n\n{body}"
        if used + len(block) > max_chars and blocks:
            break
        blocks.append(block)
        used += len(block)
    return "\n\n---\n\n".join(blocks)
