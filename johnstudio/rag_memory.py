"""High-level retrieval API over the Markdown memory vault.

`rag.py` provides the BM25 machinery (chunking, indexing, scoring). This module
is the thin, prompt-facing layer on top of it: a single `query()` call that an
orchestrator can drop into a context pack without knowing anything about chunks,
scores, or character budgets.

The split exists so callers depend on a stable verb (`rag_memory.query`) rather
than the index internals — the BM25 implementation can change without touching
prompt-assembly code. Everything stays dependency-free, deterministic and
offline, exactly like `rag.py`.

Typical use (see `team_orchestrator._build_specialist_prompt`):

    block = rag_memory.query(repo, task_text, k=5)
    if block:
        sections.append(block)
"""
from __future__ import annotations

from pathlib import Path

from . import rag

_SECTION_HEADER = "## Relevant project memory (retrieved)"
DEFAULT_K = 5
DEFAULT_MAX_CHARS = 3000


def search(repo_path: str | Path, query_text: str, k: int = DEFAULT_K):
    """Return the raw top-k `(Chunk, score)` results for a query.

    Thin pass-through to `rag.retrieve`; exposed so callers that want the
    structured results (e.g. for ranking or display) don't have to re-parse the
    rendered Markdown from `query()`.
    """
    if not query_text or not query_text.strip():
        return []
    return rag.retrieve(repo_path, query_text, k=k)


def query(
    repo_path: str | Path,
    query_text: str,
    *,
    k: int = DEFAULT_K,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """Retrieve vault chunks relevant to `query_text` and render a prompt block.

    Returns a Markdown section (header + snippets) ready to splice into a
    context pack, or an empty string when the vault is missing/empty or nothing
    scores above zero — so callers can `if block:` and omit the section.
    """
    results = search(repo_path, query_text, k=k)
    if not results:
        return ""
    body = rag.render_snippets(results, max_chars=max_chars)
    if not body:
        return ""
    return f"{_SECTION_HEADER}\n\n{body}\n"
