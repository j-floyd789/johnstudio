"""Model Context Protocol (MCP) server exposing JohnStudio capabilities.

This is a dependency-free, stdlib-only implementation of the MCP server
side of the protocol (JSON-RPC 2.0 over a newline-delimited stdio
transport). It deliberately avoids importing the optional ``mcp`` SDK so
that this module always imports cleanly and is unit-testable without any
extra install.

It lets an MCP-capable client (Claude Desktop, an IDE, another agent)
read JohnStudio's projects, memory vault, and knowledge graph as tools.

Run it as a stdio MCP server::

    python -m johnstudio.mcp_server

The wire format is one JSON object per line in each direction. The
methods implemented are the subset every MCP client needs to bootstrap
and call tools: ``initialize``, ``tools/list``, ``tools/call`` and
``ping``. Notifications (e.g. ``notifications/initialized``) are accepted
and ignored.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable, Iterable

from . import (
    artifacts as artifacts_mod,
    blackboard as _bb,
    knowledge_graph,
    memory,
    patterns as patterns_mod,
    project,
    reasoning_bank,
    vector_store,
)

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "johnstudio"
SERVER_VERSION = "0.1.0"

# JSON-RPC 2.0 error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class ToolError(Exception):
    """Raised by a tool handler to signal a clean, user-facing failure."""


# ---------------------------------------------------------------------------
# Project resolution helper
# ---------------------------------------------------------------------------

def _resolve_project(name: str) -> dict:
    proj = project.get_project(name)
    if proj is None:
        raise ToolError(f"unknown project: {name!r}")
    return proj


def _safe_vault_path(repo_path: str, relative_path: str) -> Path:
    """Resolve ``relative_path`` inside the project's memory vault.

    Guards against path traversal: the resolved target must stay within
    the vault root.
    """
    root = memory.memory_root(repo_path).resolve()
    target = (root / relative_path).resolve()
    if root != target and root not in target.parents:
        raise ToolError(f"path escapes memory vault: {relative_path!r}")
    return target


# ---------------------------------------------------------------------------
# Tool handlers — each returns a JSON-serialisable value.
# ---------------------------------------------------------------------------

def _tool_list_projects(_args: dict) -> Any:
    return {"projects": project.list_projects()}


def _tool_get_project(args: dict) -> Any:
    name = args.get("name")
    if not name:
        raise ToolError("missing required arg: name")
    return _resolve_project(name)


def _tool_search_memory(args: dict) -> Any:
    name = args.get("project")
    query = args.get("query")
    if not name:
        raise ToolError("missing required arg: project")
    if not query:
        raise ToolError("missing required arg: query")
    limit = int(args.get("limit", 50))
    proj = _resolve_project(name)
    root = memory.memory_root(proj["repo_path"])
    needle = str(query).lower()
    matches: list[dict] = []
    if root.exists():
        for path in _iter_markdown(root):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if needle in line.lower():
                    matches.append({
                        "path": str(path.relative_to(root)),
                        "line": lineno,
                        "text": line.strip()[:300],
                    })
                    if len(matches) >= limit:
                        return {"query": query, "matches": matches, "truncated": True}
    return {"query": query, "matches": matches, "truncated": False}


def _tool_read_memory_note(args: dict) -> Any:
    name = args.get("project")
    rel = args.get("path")
    if not name:
        raise ToolError("missing required arg: project")
    if not rel:
        raise ToolError("missing required arg: path")
    proj = _resolve_project(name)
    target = _safe_vault_path(proj["repo_path"], rel)
    if not target.exists() or not target.is_file():
        raise ToolError(f"note not found: {rel!r}")
    return {"path": rel, "content": target.read_text(encoding="utf-8")}


def _tool_list_graph_entities(args: dict) -> Any:
    name = args.get("project")
    if not name:
        raise ToolError("missing required arg: project")
    proj = _resolve_project(name)
    return {"entities": knowledge_graph.list_entities(proj["id"])}


def _tool_list_graph_relationships(args: dict) -> Any:
    name = args.get("project")
    if not name:
        raise ToolError("missing required arg: project")
    proj = _resolve_project(name)
    return {"relationships": knowledge_graph.list_relationships(proj["id"])}


def _tool_memory_backlinks(args: dict) -> Any:
    name = args.get("project")
    if not name:
        raise ToolError("missing required arg: project")
    proj = _resolve_project(name)
    return {"backlinks": knowledge_graph.build_backlink_index(proj["repo_path"])}


def _tool_artifacts_register(args: dict) -> Any:
    project_name = args.get("project")
    if not project_name:
        raise ToolError("missing required arg: project")
    kind = args.get("kind")
    if not kind:
        raise ToolError("missing required arg: kind")
    path = args.get("path")
    if not path:
        raise ToolError("missing required arg: path")
    proj = _resolve_project(project_name)
    tags = args.get("tags") or []
    if not isinstance(tags, list):
        raise ToolError("`tags` must be an array of strings")
    task_number = args.get("task_number")
    agent = args.get("agent")
    try:
        art_id = artifacts_mod.Manifests(proj["id"]).register(
            task_number=int(task_number) if task_number is not None else None,
            kind=kind,
            path=path,
            tags=[str(t) for t in tags],
            agent=agent,
        )
    except FileNotFoundError as exc:
        raise ToolError(str(exc))
    return {"id": art_id}


def _tool_artifacts_get(args: dict) -> Any:
    project_name = args.get("project")
    art_id = args.get("id")
    if not project_name:
        raise ToolError("missing required arg: project")
    if art_id is None:
        raise ToolError("missing required arg: id")
    proj = _resolve_project(project_name)
    m = artifacts_mod.Manifests(proj["id"]).get(int(art_id))
    if m is None:
        raise ToolError(f"artifact not found: {art_id}")
    return m.to_dict()


def _tool_artifacts_find(args: dict) -> Any:
    project_name = args.get("project")
    if not project_name:
        raise ToolError("missing required arg: project")
    proj = _resolve_project(project_name)
    tags = args.get("tags")
    if tags is not None and not isinstance(tags, list):
        raise ToolError("`tags` must be an array of strings")
    task_number = args.get("task_number")
    results = artifacts_mod.Manifests(proj["id"]).find(
        task_number=int(task_number) if task_number is not None else None,
        kind=args.get("kind"),
        tags=[str(t) for t in tags] if tags else None,
        agent=args.get("agent"),
    )
    return {"artifacts": [m.to_dict() for m in results]}


def _tool_reasoning_bank_record(args: dict) -> Any:
    task_number = args.get("task_number")
    goal = args.get("goal")
    outcome = args.get("outcome")
    approach_summary = args.get("approach_summary")
    tags = args.get("tags") or []
    project_id = args.get("project_id")
    project_name = args.get("project")
    if task_number is None:
        raise ToolError("missing required arg: task_number")
    if not goal:
        raise ToolError("missing required arg: goal")
    if not outcome:
        raise ToolError("missing required arg: outcome")
    if not approach_summary:
        raise ToolError("missing required arg: approach_summary")
    if project_id is None:
        if not project_name:
            raise ToolError("provide either project_id or project (name)")
        project_id = _resolve_project(project_name)["id"]
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise ToolError("tags must be a list of strings")
    rb = reasoning_bank.ReasoningBank(project_id=int(project_id))
    try:
        rb.record_task(
            task_number=int(task_number),
            goal=str(goal),
            outcome=str(outcome),
            approach_summary=str(approach_summary),
            tags=tags,
        )
    finally:
        rb.close()
    return {"task_number": int(task_number), "project_id": int(project_id), "recorded": True}


def _tool_reasoning_bank_find(args: dict) -> Any:
    goal = args.get("goal")
    if not goal:
        raise ToolError("missing required arg: goal")
    k = int(args.get("k", 5))
    project_id = args.get("project_id")
    project_name = args.get("project")
    if project_id is None and project_name:
        project_id = _resolve_project(project_name)["id"]
    rb = reasoning_bank.ReasoningBank(project_id=int(project_id) if project_id is not None else None)
    try:
        priors = rb.find_priors(str(goal), k=k)
    finally:
        rb.close()
    return {
        "goal": goal,
        "k": k,
        "priors": [
            {
                "task_number": p.task_number,
                "project_id": p.project_id,
                "goal": p.goal,
                "outcome": p.outcome,
                "approach_summary": p.approach_summary,
                "tags": p.tags,
                "score": p.score,
            }
            for p in priors
        ],
    }


def _resolve_project_id(args: dict) -> int:
    """Pull a project id from ``args``. Accepts ``project_id`` or ``project`` name."""
    pid = args.get("project_id")
    if pid is not None:
        return int(pid)
    name = args.get("project")
    if not name:
        raise ToolError("provide either project_id or project (name)")
    return int(_resolve_project(name)["id"])


def _tool_patterns_record(args: dict) -> Any:
    kind = args.get("kind")
    text = args.get("text")
    if not kind:
        raise ToolError("missing required arg: kind")
    if not text:
        raise ToolError("missing required arg: text")
    project_id = _resolve_project_id(args)
    confidence = float(args.get("confidence", patterns_mod.DEFAULT_CONFIDENCE))
    tags = args.get("tags") or []
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise ToolError("tags must be a list of strings")
    evidence = args.get("evidence_artifact_ids") or []
    if not isinstance(evidence, list):
        raise ToolError("evidence_artifact_ids must be a list of integers")
    try:
        evidence_ids = [int(x) for x in evidence]
    except (TypeError, ValueError):
        raise ToolError("evidence_artifact_ids must be integers")
    source_task_number = args.get("source_task_number")
    ps = patterns_mod.Patterns(project_id=project_id)
    try:
        pattern_id = ps.record(
            kind=str(kind),
            text=str(text),
            confidence=confidence,
            tags=tags,
            evidence_artifact_ids=evidence_ids,
            source_task_number=int(source_task_number) if source_task_number is not None else None,
        )
    finally:
        ps.close()
    return {"id": pattern_id, "project_id": project_id}


def _tool_patterns_find(args: dict) -> Any:
    query = args.get("query")
    if not query:
        raise ToolError("missing required arg: query")
    project_id = _resolve_project_id(args)
    k = int(args.get("k", 5))
    min_confidence = float(args.get("min_confidence", 0.0))
    ps = patterns_mod.Patterns(project_id=project_id)
    try:
        results = ps.find_similar(str(query), k=k, min_confidence=min_confidence)
    finally:
        ps.close()
    return {
        "query": query,
        "k": k,
        "min_confidence": min_confidence,
        "patterns": [p.to_dict() for p in results],
    }


def _tool_patterns_boost(args: dict) -> Any:
    pattern_id = args.get("id")
    if pattern_id is None:
        raise ToolError("missing required arg: id")
    project_id = _resolve_project_id(args)
    delta = float(args.get("delta", 0.05))
    ps = patterns_mod.Patterns(project_id=project_id)
    try:
        new_conf = ps.boost(int(pattern_id), delta=delta)
    except KeyError as exc:
        raise ToolError(str(exc))
    finally:
        ps.close()
    return {"id": int(pattern_id), "confidence": new_conf}


def _tool_patterns_demote(args: dict) -> Any:
    pattern_id = args.get("id")
    if pattern_id is None:
        raise ToolError("missing required arg: id")
    project_id = _resolve_project_id(args)
    delta = float(args.get("delta", 0.10))
    ps = patterns_mod.Patterns(project_id=project_id)
    try:
        new_conf = ps.demote(int(pattern_id), delta=delta)
    except KeyError as exc:
        raise ToolError(str(exc))
    finally:
        ps.close()
    return {"id": int(pattern_id), "confidence": new_conf}


def _tool_patterns_summarize_arc_iter(args: dict) -> Any:
    task_number = args.get("task_number")
    if task_number is None:
        raise ToolError("missing required arg: task_number")
    project_id = _resolve_project_id(args)
    pattern_id = patterns_mod.summarize_arc_iter(project_id, int(task_number))
    return {"id": pattern_id, "task_number": int(task_number), "project_id": project_id}


def _tool_vector_search(args: dict) -> Any:
    namespace = args.get("namespace")
    query = args.get("query")
    if not namespace:
        raise ToolError("missing required arg: namespace")
    if not query:
        raise ToolError("missing required arg: query")
    k = int(args.get("k", 5))
    vs = vector_store.VectorStore()
    try:
        results = vs.search(str(namespace), str(query), k=k)
    finally:
        vs.close()
    return {
        "namespace": namespace,
        "query": query,
        "k": k,
        "results": [
            {"ref_id": rid, "score": score, "text": text}
            for (rid, score, text) in results
        ],
    }


# ---------------------------------------------------------------------------
# Blackboard tools — per-task shared state with TTL'd entries.
# ---------------------------------------------------------------------------

def _bb_resolve(args: dict) -> tuple[_bb.Blackboard, int]:
    """Resolve the project + task_number args to a Blackboard handle.

    The project may be passed by ``project`` (name) or ``project_id``
    (int). ``task_number`` is required.
    """
    task_number = args.get("task_number")
    if task_number is None:
        raise ToolError("missing required arg: task_number")
    try:
        tn = int(task_number)
    except (TypeError, ValueError):
        raise ToolError("task_number must be an integer")

    name = args.get("project")
    pid = args.get("project_id")
    if name:
        proj = _resolve_project(name)
        project_id = int(proj["id"])
    elif pid is not None:
        try:
            project_id = int(pid)
        except (TypeError, ValueError):
            raise ToolError("project_id must be an integer")
    else:
        raise ToolError("missing required arg: project or project_id")
    return _bb.Blackboard(project_id=project_id, task_number=tn), tn


def _tool_blackboard_post(args: dict) -> Any:
    bb, _ = _bb_resolve(args)
    key = args.get("key")
    if not key:
        raise ToolError("missing required arg: key")
    if "value" not in args:
        raise ToolError("missing required arg: value")
    ttl = args.get("ttl_seconds")
    if ttl is None:
        raise ToolError("missing required arg: ttl_seconds")
    try:
        ttl_int = int(ttl)
    except (TypeError, ValueError):
        raise ToolError("ttl_seconds must be an integer")
    agent = args.get("agent")
    entry = bb.post(key=key, value=args["value"], ttl_seconds=ttl_int, agent=agent)
    return {
        "key": key,
        "value": entry.value,
        "agent": entry.agent,
        "posted_at": entry.posted_at,
        "expires_at": entry.expires_at,
    }


def _tool_blackboard_append(args: dict) -> Any:
    bb, _ = _bb_resolve(args)
    key = args.get("key")
    if not key:
        raise ToolError("missing required arg: key")
    if "value" not in args:
        raise ToolError("missing required arg: value")
    ttl = args.get("ttl_seconds")
    if ttl is None:
        raise ToolError("missing required arg: ttl_seconds")
    try:
        ttl_int = int(ttl)
    except (TypeError, ValueError):
        raise ToolError("ttl_seconds must be an integer")
    agent = args.get("agent")
    entry = bb.append(key=key, value=args["value"], ttl_seconds=ttl_int, agent=agent)
    return {
        "key": key,
        "value": entry.value,
        "agent": entry.agent,
        "posted_at": entry.posted_at,
        "expires_at": entry.expires_at,
    }


def _tool_blackboard_get(args: dict) -> Any:
    bb, _ = _bb_resolve(args)
    key = args.get("key")
    if not key:
        raise ToolError("missing required arg: key")
    entry = bb.get(key=key)
    if entry is None:
        return {"key": key, "found": False}
    return {
        "key": key,
        "found": True,
        "value": entry.value,
        "agent": entry.agent,
        "posted_at": entry.posted_at,
        "expires_at": entry.expires_at,
    }


def _tool_blackboard_snapshot(args: dict) -> Any:
    bb, tn = _bb_resolve(args)
    return {"task_number": tn, "snapshot": bb.snapshot()}


def _iter_markdown(root: Path) -> Iterable[Path]:
    # Thin wrapper so tests can monkeypatch if needed; delegates to utils.
    from . import utils
    return utils.iter_markdown_files(root)


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

_PROJECT_ARG = {"type": "string", "description": "Registered project name."}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_projects",
        "description": "List all JohnStudio projects registered in the local DB.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": _tool_list_projects,
    },
    {
        "name": "get_project",
        "description": "Get a single project's id, repo path and base branch by name.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": _PROJECT_ARG},
            "required": ["name"],
            "additionalProperties": False,
        },
        "handler": _tool_get_project,
    },
    {
        "name": "search_memory",
        "description": "Case-insensitive substring search across a project's Markdown memory vault. Returns path/line/text matches.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": _PROJECT_ARG,
                "query": {"type": "string", "description": "Substring to search for."},
                "limit": {"type": "integer", "description": "Max matches to return (default 50)."},
            },
            "required": ["project", "query"],
            "additionalProperties": False,
        },
        "handler": _tool_search_memory,
    },
    {
        "name": "read_memory_note",
        "description": "Read a single Markdown note from a project's memory vault (path relative to the vault root).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": _PROJECT_ARG,
                "path": {"type": "string", "description": "Path relative to the memory vault root, e.g. 'current_state.md'."},
            },
            "required": ["project", "path"],
            "additionalProperties": False,
        },
        "handler": _tool_read_memory_note,
    },
    {
        "name": "list_graph_entities",
        "description": "List knowledge-graph entities (people, projects, tasks, systems, ...) for a project.",
        "inputSchema": {
            "type": "object",
            "properties": {"project": _PROJECT_ARG},
            "required": ["project"],
            "additionalProperties": False,
        },
        "handler": _tool_list_graph_entities,
    },
    {
        "name": "list_graph_relationships",
        "description": "List knowledge-graph relationships (edges) for a project.",
        "inputSchema": {
            "type": "object",
            "properties": {"project": _PROJECT_ARG},
            "required": ["project"],
            "additionalProperties": False,
        },
        "handler": _tool_list_graph_relationships,
    },
    {
        "name": "artifacts_register",
        "description": "Register an artifact (file output by an agent) in the project's manifest store. Computes sha256, dedupes by (project, sha256), returns the artifact id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": _PROJECT_ARG,
                "task_number": {"type": "integer", "description": "Owning task number (optional)."},
                "kind": {"type": "string", "description": "Artifact kind, e.g. 'arc_iter_result'."},
                "path": {"type": "string", "description": "Absolute path to the file to register."},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags used for AND-match retrieval."},
                "agent": {"type": "string", "description": "Producing agent identifier (optional)."},
            },
            "required": ["project", "kind", "path"],
            "additionalProperties": False,
        },
        "handler": _tool_artifacts_register,
    },
    {
        "name": "artifacts_get",
        "description": "Fetch a single artifact manifest by id within a project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": _PROJECT_ARG,
                "id": {"type": "integer", "description": "Artifact id returned by artifacts_register."},
            },
            "required": ["project", "id"],
            "additionalProperties": False,
        },
        "handler": _tool_artifacts_get,
    },
    {
        "name": "artifacts_find",
        "description": "Search the artifact manifest store. Filters are conjunctive; tag matching is AND.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": _PROJECT_ARG,
                "task_number": {"type": "integer"},
                "kind": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "agent": {"type": "string"},
            },
            "required": ["project"],
            "additionalProperties": False,
        },
        "handler": _tool_artifacts_find,
    },
    {
        "name": "memory_backlinks",
        "description": "Build a backlink index (wiki-link target -> notes that reference it) for a project's vault.",
        "inputSchema": {
            "type": "object",
            "properties": {"project": _PROJECT_ARG},
            "required": ["project"],
            "additionalProperties": False,
        },
        "handler": _tool_memory_backlinks,
    },
    {
        "name": "reasoning_bank_record",
        "description": (
            "Persist a past-task trajectory (goal, outcome, approach_summary, tags) "
            "to the ReasoningBank AND embed it for semantic recall. "
            "Provide either project_id or project (name)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_number": {"type": "integer", "description": "Task number (PK; idempotent UPSERT)."},
                "project_id": {"type": "integer", "description": "Project id (alternative to `project`)."},
                "project": {"type": "string", "description": "Project name (alternative to `project_id`)."},
                "goal": {"type": "string", "description": "What the task was trying to accomplish."},
                "outcome": {"type": "string", "description": "Outcome label, e.g. 'edge_found=false'."},
                "approach_summary": {"type": "string", "description": "Short prose of what was tried."},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Free-form tags."},
            },
            "required": ["task_number", "goal", "outcome", "approach_summary"],
            "additionalProperties": False,
        },
        "handler": _tool_reasoning_bank_record,
    },
    {
        "name": "reasoning_bank_find",
        "description": (
            "Semantic search the ReasoningBank for priors similar to the supplied goal. "
            "Returns up to k priors with task_number, outcome, approach_summary, tags, score."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "Query text — typically the new task's description."},
                "k": {"type": "integer", "description": "Max priors to return (default 5)."},
                "project_id": {"type": "integer", "description": "Restrict to one project id."},
                "project": {"type": "string", "description": "Restrict to one project by name."},
            },
            "required": ["goal"],
            "additionalProperties": False,
        },
        "handler": _tool_reasoning_bank_find,
    },
    {
        "name": "patterns_record",
        "description": (
            "Record a confidence-scored pattern (lesson, arc-iter outcome, leakage finding) "
            "into the patterns store AND embed it for semantic recall. "
            "Provide either project_id or project (name)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "Project id (alternative to `project`)."},
                "project": {"type": "string", "description": "Project name (alternative to `project_id`)."},
                "kind": {"type": "string", "description": "Pattern kind, e.g. 'arc_iter_outcome', 'candidate_class_null'."},
                "text": {"type": "string", "description": "Free-form summary of the lesson."},
                "confidence": {"type": "number", "description": "Initial confidence 0..0.99 (default 0.7)."},
                "tags": {"type": "array", "items": {"type": "string"}},
                "evidence_artifact_ids": {"type": "array", "items": {"type": "integer"}},
                "source_task_number": {"type": "integer", "description": "Task that produced this pattern (optional)."},
            },
            "required": ["kind", "text"],
            "additionalProperties": False,
        },
        "handler": _tool_patterns_record,
    },
    {
        "name": "patterns_find",
        "description": (
            "Semantic search the patterns store for lessons similar to the supplied query. "
            "Filters by min_confidence so low-confidence patterns don't pollute results."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "project": {"type": "string"},
                "query": {"type": "string", "description": "Query text — typically the new task's description."},
                "k": {"type": "integer", "description": "Max patterns to return (default 5)."},
                "min_confidence": {"type": "number", "description": "Filter floor (default 0.0)."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "handler": _tool_patterns_find,
    },
    {
        "name": "patterns_boost",
        "description": "Increase a pattern's confidence by `delta` (default 0.05), capped at 0.99.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "project": {"type": "string"},
                "id": {"type": "integer", "description": "Pattern id."},
                "delta": {"type": "number", "description": "Increment (default 0.05)."},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
        "handler": _tool_patterns_boost,
    },
    {
        "name": "patterns_demote",
        "description": "Decrease a pattern's confidence by `delta` (default 0.10), floored at 0.0.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "project": {"type": "string"},
                "id": {"type": "integer", "description": "Pattern id."},
                "delta": {"type": "number", "description": "Decrement (default 0.10)."},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
        "handler": _tool_patterns_demote,
    },
    {
        "name": "patterns_summarize_arc_iter",
        "description": (
            "Summarize a completed arc iter's TASK.md + DONE.md + artifact JSONs "
            "into a confidence-scored pattern row. Returns the new pattern id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "project": {"type": "string"},
                "task_number": {"type": "integer", "description": "Task number of the completed arc iter."},
            },
            "required": ["task_number"],
            "additionalProperties": False,
        },
        "handler": _tool_patterns_summarize_arc_iter,
    },
    {
        "name": "vector_search",
        "description": (
            "Brute-force cosine search over the local sqlite vector store within a namespace. "
            "Embeddings are produced by the local Ollama daemon — no paid APIs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Vector namespace, e.g. 'reasoning_bank'."},
                "query": {"type": "string", "description": "Query text."},
                "k": {"type": "integer", "description": "Max results (default 5)."},
            },
            "required": ["namespace", "query"],
            "additionalProperties": False,
        },
        "handler": _tool_vector_search,
    },
    {
        "name": "blackboard_post",
        "description": (
            "Upsert a TTL'd entry on the per-task blackboard. Other agents "
            "working on the same task will see it via blackboard_snapshot "
            "and via injection into their next prompt."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": _PROJECT_ARG,
                "project_id": {"type": "integer", "description": "Project id (alternative to 'project')."},
                "task_number": {"type": "integer", "description": "Per-project task number."},
                "key": {"type": "string", "description": "Blackboard key (unique per task)."},
                "value": {"description": "JSON-serialisable value."},
                "ttl_seconds": {"type": "integer", "description": "Seconds until the entry expires."},
                "agent": {"type": "string", "description": "Optional posting agent's name."},
            },
            "required": ["task_number", "key", "value", "ttl_seconds"],
            "additionalProperties": False,
        },
        "handler": _tool_blackboard_post,
    },
    {
        "name": "blackboard_append",
        "description": (
            "Append a value to a list-valued blackboard key (read-modify-write). "
            "Creates the list if absent; promotes a scalar predecessor to a list."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": _PROJECT_ARG,
                "project_id": {"type": "integer", "description": "Project id (alternative to 'project')."},
                "task_number": {"type": "integer", "description": "Per-project task number."},
                "key": {"type": "string", "description": "Blackboard key (unique per task)."},
                "value": {"description": "JSON-serialisable element to append."},
                "ttl_seconds": {"type": "integer", "description": "Seconds until the entry expires (refreshed on append)."},
                "agent": {"type": "string", "description": "Optional posting agent's name."},
            },
            "required": ["task_number", "key", "value", "ttl_seconds"],
            "additionalProperties": False,
        },
        "handler": _tool_blackboard_append,
    },
    {
        "name": "blackboard_get",
        "description": "Read a single live blackboard entry by key. Expired entries return found=false.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": _PROJECT_ARG,
                "project_id": {"type": "integer", "description": "Project id (alternative to 'project')."},
                "task_number": {"type": "integer", "description": "Per-project task number."},
                "key": {"type": "string", "description": "Blackboard key."},
            },
            "required": ["task_number", "key"],
            "additionalProperties": False,
        },
        "handler": _tool_blackboard_get,
    },
    {
        "name": "blackboard_snapshot",
        "description": "Return all live {key: value} entries for the task — same shape that gets injected into specialist prompts.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": _PROJECT_ARG,
                "project_id": {"type": "integer", "description": "Project id (alternative to 'project')."},
                "task_number": {"type": "integer", "description": "Per-project task number."},
            },
            "required": ["task_number"],
            "additionalProperties": False,
        },
        "handler": _tool_blackboard_snapshot,
    },
]

_TOOLS_BY_NAME: dict[str, dict[str, Any]] = {t["name"]: t for t in TOOLS}


def _public_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """Strip the internal ``handler`` key before sending to a client."""
    return {k: v for k, v in tool.items() if k != "handler"}


# ---------------------------------------------------------------------------
# JSON-RPC plumbing
# ---------------------------------------------------------------------------

def _result(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str, data: Any = None) -> dict:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _handle_initialize(_params: dict) -> dict:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    }


def _handle_tools_list(_params: dict) -> dict:
    return {"tools": [_public_tool(t) for t in TOOLS]}


def _args_summary(args: dict, *, max_len: int = 200) -> str:
    """Small, log-safe one-line summary of a tool's arguments.

    Keeps the MCP-tool-called event payload tiny (we never dump full
    file bodies / big query results that some tools accept)."""
    try:
        s = json.dumps(args, default=str, sort_keys=True)
    except Exception:
        s = repr(args)
    if len(s) > max_len:
        s = s[: max_len - 1] + "…"
    return s


def _emit_tool_called(name: Any, args: dict) -> None:
    """Item 20: best-effort MCP-tool-usage event. Powers a future live
    MCP feed. Never raises — a telemetry failure must not break the call."""
    try:
        from .hooks import EventTypes, bus
        # task_id is only known when the caller passes one (a few tools
        # take task_db_id / task_id); otherwise omit it.
        task_id = None
        for key in ("task_db_id", "task_id"):
            if isinstance(args, dict) and args.get(key) is not None:
                task_id = args.get(key)
                break
        bus.emit(EventTypes.MCP_TOOL_CALLED, {
            "tool": name,
            "task_id": task_id,
            "args_summary": _args_summary(args),
        })
    except Exception:
        pass


def _handle_tools_call(params: dict) -> dict:
    name = params.get("name")
    args = params.get("arguments") or {}
    tool = _TOOLS_BY_NAME.get(name)
    if tool is None:
        raise ToolError(f"unknown tool: {name!r}")
    _emit_tool_called(name, args)
    handler: Callable[[dict], Any] = tool["handler"]
    value = handler(args)
    # MCP tool results are a content array; we return one JSON text block.
    return {
        "content": [{"type": "text", "text": json.dumps(value, indent=2, default=str)}],
        "isError": False,
    }


_METHODS: dict[str, Callable[[dict], dict]] = {
    "initialize": _handle_initialize,
    "tools/list": _handle_tools_list,
    "tools/call": _handle_tools_call,
    "ping": lambda _params: {},
}


def handle_message(msg: dict) -> dict | None:
    """Dispatch one parsed JSON-RPC message.

    Returns the response dict, or ``None`` for notifications (messages
    with no ``id``), which per JSON-RPC must not be answered.
    """
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _error(msg.get("id") if isinstance(msg, dict) else None,
                      INVALID_REQUEST, "invalid JSON-RPC 2.0 message")

    method = msg.get("method")
    req_id = msg.get("id")
    is_notification = "id" not in msg

    if not isinstance(method, str):
        if is_notification:
            return None
        return _error(req_id, INVALID_REQUEST, "missing method")

    handler = _METHODS.get(method)
    if handler is None:
        # Unknown notifications (e.g. notifications/initialized) are dropped.
        if is_notification:
            return None
        return _error(req_id, METHOD_NOT_FOUND, f"unknown method: {method}")

    params = msg.get("params") or {}
    try:
        result = handler(params)
    except ToolError as exc:
        if is_notification:
            return None
        return _error(req_id, INVALID_PARAMS, str(exc))
    except Exception as exc:  # pragma: no cover - defensive catch-all
        if is_notification:
            return None
        return _error(req_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")

    if is_notification:
        return None
    return _result(req_id, result)


# ---------------------------------------------------------------------------
# stdio transport
# ---------------------------------------------------------------------------

def serve_stdio(stdin=None, stdout=None) -> None:
    """Run the newline-delimited JSON-RPC loop over stdio until EOF."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _write(stdout, _error(None, PARSE_ERROR, "parse error"))
            continue
        response = handle_message(msg)
        if response is not None:
            _write(stdout, response)


def _write(stdout, obj: dict) -> None:
    stdout.write(json.dumps(obj) + "\n")
    stdout.flush()


def main() -> None:  # pragma: no cover - thin entrypoint
    serve_stdio()


if __name__ == "__main__":  # pragma: no cover
    main()
