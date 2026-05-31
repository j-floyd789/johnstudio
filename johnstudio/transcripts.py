"""Claude Code on-disk transcript discovery.

Claude Code writes a per-session JSONL transcript at
`~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl`. Path encoding is
"replace each `/` and `.` with `-`" (so
`/Users/john/Desktop/coolsite/.johnstudio/worktrees/task-0006-claude-backend`
becomes `-Users-john-Desktop-coolsite--johnstudio-worktrees-task-0006-claude-backend`).

When a specialist invokes the `Task` tool to spawn a subagent, the
subagent's events land in the SAME file with `isSidechain: true`. This
module gives the UI access to those transcripts so the user can replay
the full reasoning + tool calls of any agent, including subagents that
the live stream-json couldn't reach.

We do not modify Claude Code's files; we only read them.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable


CLAUDE_PROJECTS_ROOT = Path.home() / ".claude" / "projects"


def encode_cwd(cwd: str | Path) -> str:
    """Apply Claude Code's path-encoding rule.

    `/` → `-` and `.` → `-`. So
    `/Users/john/Desktop/coolsite/.johnstudio` →
    `-Users-john-Desktop-coolsite--johnstudio`.
    """
    s = str(cwd)
    out: list[str] = []
    for ch in s:
        if ch == "/" or ch == ".":
            out.append("-")
        else:
            out.append(ch)
    return "".join(out)


def transcript_dir_for_cwd(cwd: str | Path, root: Path | None = None) -> Path:
    return (root or CLAUDE_PROJECTS_ROOT) / encode_cwd(cwd)


def find_session_transcript(cwd: str | Path, session_id: str) -> Path | None:
    """Return the .jsonl path for `session_id` under the cwd's project dir."""
    d = transcript_dir_for_cwd(cwd)
    if not d.exists():
        return None
    candidate = d / f"{session_id}.jsonl"
    return candidate if candidate.exists() else None


def find_recent_transcripts(cwd: str | Path, limit: int = 20) -> list[Path]:
    """All transcripts under the cwd's project dir, newest first."""
    d = transcript_dir_for_cwd(cwd)
    if not d.exists():
        return []
    files = sorted(
        d.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return files[:limit]


def read_transcript(
    p: Path, *, limit: int = 500, include_sidechain: bool = True,
    only_sidechain: bool = False,
) -> list[dict]:
    """Parse up to `limit` lines from a transcript file.

    Returns a list of dicts. Each dict gets these convenience fields added
    on top of the raw line:
    - `_index` — line number (0-based)
    - `_kind_summary` — one-line label suitable for a tree view
    """
    out: list[dict] = []
    if not p.exists():
        return out
    with p.open("r", encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            is_side = bool(d.get("isSidechain"))
            if only_sidechain and not is_side:
                continue
            if not include_sidechain and is_side:
                continue
            d["_index"] = i
            d["_kind_summary"] = _summarize(d)
            out.append(d)
            if len(out) >= limit:
                break
    return out


def _summarize(entry: dict) -> str:
    """A short label for one transcript line (for sidebar/tree rendering)."""
    t = entry.get("type") or "?"
    msg = entry.get("message") or {}
    if t == "user":
        content = msg.get("content")
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    return f"tool_result · {str(c.get('content', ''))[:80]}"
            return f"user · {json.dumps(content)[:80]}"
        return f"user · {str(content)[:80]}"
    if t == "assistant":
        content = msg.get("content") or []
        for c in content:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "text":
                return f"assistant · {(c.get('text','') or '').strip().splitlines()[0][:80]}"
            if c.get("type") == "tool_use":
                name = c.get("name") or "tool"
                inp = c.get("input") or {}
                hint = (
                    inp.get("file_path") or inp.get("path") or inp.get("command")
                    or inp.get("subagent_type") or inp.get("query") or ""
                )
                return f"tool_use:{name} · {str(hint)[:80]}"
        return f"assistant · (turn)"
    return t
