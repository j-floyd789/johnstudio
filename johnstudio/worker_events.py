"""Per-worker event capture.

When a Claude worker launches, the adapter tees its `--output-format
stream-json` output to a sidecar `.jsonl` file. This module spawns a small
background thread per run that tails that file, parses each JSON line into
a structured event, and inserts it into the `worker_events` table. The
graph UI subscribes via SSE (see `api/routes_stream.py`) and renders the
latest event's `summary` as the node's "current step" text.

Design:
- One tailer thread per run, started by the orchestrator when the worker
  launches. The thread polls the file every ~300ms, parses any new
  complete lines (lines without a trailing `\n` are treated as partial
  and re-read next tick), and exits when DONE.md / RESULT.md exist in the
  run's worktree OR the file hasn't grown in 5 minutes.
- Each insert gets a monotonic `seq` per run. SSE clients track `id` (the
  global autoincrement) so they can resume after a reconnect.
- Parser is best-effort. Unknown event types are still recorded (kind set
  to the raw `type` field) so we never silently drop information.

Why threads, not async: the rest of JohnStudio is sync (FastAPI sync
handlers, sqlite3). A thread per active run is cheap (4–6 workers max in
parallel mode, 1–2 in chain mode) and keeps the model uniform.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import db


# ---------------------------------------------------------------------------
# Parser dispatch
# ---------------------------------------------------------------------------

def parse_stream_json(line: str, provider: str = "claude") -> dict[str, Any] | None:
    """Parse a single stream-json line into {kind, summary}.

    Returns None for blank lines or non-JSON noise (each CLI emits some
    stderr-style warnings on stdout before its first JSON event).
    Returns a dict with at least `kind` and `summary` otherwise; `summary`
    is the short human-readable text shown on the graph node.
    """
    line = line.strip()
    if not line:
        return None
    try:
        e = json.loads(line)
    except json.JSONDecodeError:
        # CLIs sometimes emit non-JSON banners before the first event.
        # Silently drop — they aren't useful on the graph.
        return None

    if provider == "codex":
        return _parse_codex_event(e)
    if provider == "gemini":
        return _parse_gemini_event(e)
    return _parse_claude_event(e)


# ---------------------------------------------------------------------------
# Claude — `claude --print --output-format stream-json --verbose`
# Schema: {type: system|assistant|user|result|rate_limit_event, ...}
# ---------------------------------------------------------------------------

def _parse_claude_event(e: dict) -> dict[str, Any] | None:
    t = e.get("type")
    if t == "system":
        sub = e.get("subtype") or ""
        model = e.get("model") or "?"
        sid = e.get("session_id") or ""
        cwd = e.get("cwd") or ""
        return {"kind": f"system:{sub}" if sub else "system",
                "summary": f"session started · model={model}",
                "extra": {"session_id": sid, "cwd": cwd}}
    # Assistant turns carry per-turn cost in their `usage` dict; we
    # surface it as a typed event so the cost-tracker can sum it.
    if t == "assistant":
        msg = e.get("message") or {}
        usage = msg.get("usage") or {}
        # Not every assistant event includes total cost; Claude only
        # emits cost on the final `result` event. We rely on `result`
        # for the authoritative number — handled below.
        pass
    if t == "assistant":
        msg = e.get("message") or {}
        for c in msg.get("content") or []:
            ct = c.get("type")
            if ct == "text":
                first_line = (c.get("text") or "").strip().split("\n", 1)[0]
                return {"kind": "assistant_text", "summary": first_line[:200]}
            if ct == "tool_use":
                name = c.get("name") or "tool"
                tool_id = c.get("id") or ""
                inp = c.get("input") or {}
                # Task tool spawns a subagent — promote to its own kind so the
                # graph can show the spawn-and-return cycle as a child node.
                if name == "Task":
                    sub_type = (inp.get("subagent_type") or "subagent")
                    brief = inp.get("prompt") or inp.get("description") or ""
                    if isinstance(brief, str):
                        first = brief.strip().split("\n", 1)[0][:160]
                    else:
                        first = json.dumps(brief)[:160]
                    return {
                        "kind": "spawn:subagent",
                        "summary": f"{sub_type} · {first}",
                        "extra": {
                            "tool_use_id": tool_id,
                            "subagent_type": sub_type,
                            "brief": brief if isinstance(brief, str) else json.dumps(brief),
                        },
                    }
                hint = (
                    inp.get("file_path") or inp.get("path") or inp.get("command")
                    or inp.get("pattern") or inp.get("url") or inp.get("query") or ""
                )
                if not isinstance(hint, str):
                    hint = json.dumps(hint)[:120]
                else:
                    hint = hint[:160]
                return {"kind": f"tool:{name}",
                        "summary": f"{name} · {hint}" if hint else name,
                        "extra": {"tool_use_id": tool_id}}
        return {"kind": "assistant", "summary": "(assistant turn)"}
    if t == "user":
        msg = e.get("message") or {}
        for c in msg.get("content") or []:
            if c.get("type") == "tool_result":
                ok = not c.get("is_error", False)
                tool_id = c.get("tool_use_id") or ""
                content = c.get("content")
                # Content may be a string or a list of blocks; extract text.
                if isinstance(content, list):
                    parts = [b.get("text", "") for b in content if isinstance(b, dict)]
                    body = "\n".join(p for p in parts if p)
                else:
                    body = str(content or "")
                first = body.strip().split("\n", 1)[0][:200]
                return {
                    "kind": "tool_result",
                    "summary": (first or ("tool ok" if ok else "tool error")),
                    "extra": {"tool_use_id": tool_id, "is_error": not ok, "content": body[:8000]},
                }
        return {"kind": "user", "summary": "(user turn)"}
    if t == "result":
        ok = not e.get("is_error", False)
        cost = e.get("total_cost_usd")
        turns = e.get("num_turns")
        dur = e.get("duration_ms")
        bits = []
        if turns is not None:
            bits.append(f"{turns} turns")
        if isinstance(dur, (int, float)):
            bits.append(f"{dur/1000:.1f}s")
        if isinstance(cost, (int, float)):
            bits.append(f"${cost:.4f}")
        tail = (" · " + ", ".join(bits)) if bits else ""
        return {
            "kind": "result",
            "summary": ("success" + tail) if ok else ("error" + tail),
            "extra": {
                "cost_usd": float(cost) if isinstance(cost, (int, float)) else None,
                "duration_ms": int(dur) if isinstance(dur, (int, float)) else None,
                "num_turns": int(turns) if isinstance(turns, int) else None,
                "is_error": not ok,
            },
        }
    if t == "rate_limit_event":
        info = e.get("rate_limit_info") or {}
        status = info.get("status", "?")
        # `rejected` is the one that actually means "stop spawning" —
        # we surface that distinctly so the cost-checker can short
        # circuit.
        return {
            "kind": "rate_limit",
            "summary": f"rate limit · {status}",
            "extra": {"status": status, "rejected": status == "rejected"},
        }
    return {"kind": t or "unknown", "summary": json.dumps(e)[:160]}


# ---------------------------------------------------------------------------
# Codex — `codex exec --json`
# Schema: {type: thread.started|turn.started|item.completed|turn.completed, ...}
# `item.completed` wraps either an `agent_message` (text), `function_call`
# (tool use), or `function_call_output` (tool result).
# ---------------------------------------------------------------------------

def _parse_codex_event(e: dict) -> dict[str, Any] | None:
    t = e.get("type")
    if t == "thread.started":
        return {"kind": "system:init", "summary": f"thread started · {e.get('thread_id', '?')[:12]}"}
    if t == "turn.started":
        return {"kind": "turn", "summary": "turn started"}
    # Codex emits both `item.started` (with full content) and
    # `item.completed` for the same item; parse them the same way and
    # treat `item.started` for command_execution / file_change as the
    # interesting one (it carries the command and result).
    if t in ("item.started", "item.completed"):
        item = e.get("item") or {}
        it = item.get("type")
        if it == "agent_message":
            first_line = (item.get("text") or "").strip().split("\n", 1)[0]
            return {"kind": "assistant_text", "summary": first_line[:200]}
        if it == "function_call":
            name = item.get("name") or "tool"
            args = item.get("arguments") or ""
            hint = args if isinstance(args, str) else json.dumps(args)
            return {"kind": f"tool:{name}", "summary": f"{name} · {hint[:160]}" if hint else name}
        if it == "function_call_output":
            ok = not item.get("is_error", False)
            return {"kind": "tool_result", "summary": "tool ok" if ok else "tool error"}
        if it == "command_execution":
            cmd = item.get("command") or ""
            # Strip /bin/zsh -lc wrapper for readability
            short = cmd
            for pre in ("/bin/zsh -lc ", "/bin/bash -lc ", "/bin/sh -c "):
                if short.startswith(pre):
                    short = short[len(pre):]
            short = short.strip("'\"")[:160]
            exit_code = item.get("exit_code")
            tail = f" · exit={exit_code}" if exit_code is not None else ""
            return {"kind": "tool:bash", "summary": f"{short}{tail}"}
        if it == "file_change":
            changes = item.get("changes") or []
            paths = []
            for ch in changes if isinstance(changes, list) else []:
                p = ch.get("path") if isinstance(ch, dict) else None
                kind = ch.get("kind") if isinstance(ch, dict) else None
                if p:
                    short_p = p.rsplit("/", 1)[-1]
                    paths.append(f"{kind or 'edit'}:{short_p}" if kind else short_p)
            summary = ", ".join(paths)[:200] or "file change"
            return {"kind": "tool:edit", "summary": summary}
        if it == "reasoning":
            text = (item.get("text") or "").strip().split("\n", 1)[0]
            return {"kind": "reasoning", "summary": text[:200]}
        # Unknown item type — still record with a useful summary.
        return {"kind": f"item:{it}", "summary": json.dumps(item)[:160]}
    if t == "turn.completed":
        usage = e.get("usage") or {}
        bits = []
        for k in ("input_tokens", "output_tokens"):
            if k in usage:
                bits.append(f"{k.split('_')[0]}={usage[k]}")
        tail = (" · " + ", ".join(bits)) if bits else ""
        return {"kind": "result", "summary": f"turn completed{tail}"}
    if t == "error":
        return {"kind": "error", "summary": str(e.get("message") or e)[:200]}
    return {"kind": t or "unknown", "summary": json.dumps(e)[:160]}


# ---------------------------------------------------------------------------
# Gemini — `gemini -p "" --output-format stream-json`
# Schema: {type: init|message|tool_use|tool_result|result, ...}
# `message` carries role + content (delta=True for partials).
# ---------------------------------------------------------------------------

def _parse_gemini_event(e: dict) -> dict[str, Any] | None:
    t = e.get("type")
    if t == "init":
        model = e.get("model") or "?"
        return {"kind": "system:init", "summary": f"session started · model={model}"}
    if t == "message":
        role = e.get("role") or "?"
        content = e.get("content") or ""
        if isinstance(content, list):
            content = " ".join(str(c) for c in content)
        first_line = str(content).strip().split("\n", 1)[0]
        kind = "assistant_text" if role == "assistant" else f"message:{role}"
        return {"kind": kind, "summary": first_line[:200]}
    if t in ("tool_use", "tool_call"):
        name = e.get("name") or e.get("tool_name") or "tool"
        args = e.get("args") or e.get("arguments") or e.get("input") or ""
        hint = args if isinstance(args, str) else json.dumps(args)[:160]
        return {"kind": f"tool:{name}", "summary": f"{name} · {hint[:160]}" if hint else name}
    if t in ("tool_result", "tool_response"):
        ok = e.get("status") != "error" and not e.get("is_error", False)
        return {"kind": "tool_result", "summary": "tool ok" if ok else "tool error"}
    if t == "result":
        status = e.get("status") or "?"
        stats = e.get("stats") or {}
        dur = stats.get("duration_ms")
        toks = stats.get("total_tokens")
        bits = []
        if toks is not None:
            bits.append(f"{toks} tok")
        if isinstance(dur, (int, float)):
            bits.append(f"{dur/1000:.1f}s")
        tail = (" · " + ", ".join(bits)) if bits else ""
        return {"kind": "result", "summary": f"{status}{tail}"}
    if t == "error":
        return {"kind": "error", "summary": str(e.get("message") or e)[:200]}
    return {"kind": t or "unknown", "summary": json.dumps(e)[:160]}


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def insert_event(
    *,
    run_id: int | None,
    task_id: int | None,
    phase_id: int | None,
    seq: int,
    kind: str,
    summary: str,
    raw: str,
    extra: dict | None = None,
) -> None:
    """Insert one event + apply any tracked side-effects (cost roll-up).

    Each insert opens its own connection so tailer threads don't share a
    sqlite3.Connection (which is unsafe across threads).
    """
    conn = db.connect()
    try:
        db.init_schema(conn)
        conn.execute(
            """INSERT INTO worker_events (run_id, task_id, phase_id, seq, ts, kind, summary, raw_json)
               VALUES (?,?,?,?,?,?,?,?)""",
            (run_id, task_id, phase_id, seq, _now(), kind, summary[:400], raw[:8000]),
        )
        # Cost roll-up: a `result` event with `cost_usd` adds to both the
        # run's rolling cost and the task's rolling cost. Keeps the
        # budget check O(1).
        if extra and kind == "result" and run_id is not None:
            cost = extra.get("cost_usd")
            if isinstance(cost, (int, float)) and cost > 0:
                conn.execute(
                    "UPDATE runs SET cost_usd = COALESCE(cost_usd, 0) + ? WHERE id = ?",
                    (float(cost), run_id),
                )
                if task_id is not None:
                    conn.execute(
                        "UPDATE tasks SET cost_usd = COALESCE(cost_usd, 0) + ? WHERE id = ?",
                        (float(cost), task_id),
                    )
        conn.commit()
    finally:
        conn.close()


def task_cost_status(task_id: int) -> dict:
    """Return current cost + budget posture for a task. O(1) read.

    Used by team_orchestrator before spawning new workers — if a budget
    is set and the rolling cost is past it, we don't spawn the next
    specialist or revise round.
    """
    conn = db.connect()
    try:
        db.init_schema(conn)
        row = conn.execute(
            "SELECT cost_usd, budget_usd FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {"cost_usd": 0.0, "budget_usd": None, "over_budget": False}
    cost = float(row["cost_usd"] or 0)
    budget = row["budget_usd"]
    over = (budget is not None) and (cost >= float(budget))
    return {"cost_usd": cost, "budget_usd": budget, "over_budget": over}


def list_events_since(project_id: int, since_id: int, limit: int = 200) -> list[sqlite3.Row]:
    """All events for a project's tasks with id > since_id."""
    conn = db.connect()
    try:
        db.init_schema(conn)
        rows = conn.execute(
            """SELECT e.* FROM worker_events e
               JOIN tasks t ON t.id = e.task_id
               WHERE t.project_id = ? AND e.id > ?
               ORDER BY e.id ASC LIMIT ?""",
            (project_id, since_id, limit),
        ).fetchall()
        return rows
    finally:
        conn.close()


def list_events_for_run(run_id: int, limit: int = 200) -> list[sqlite3.Row]:
    conn = db.connect()
    try:
        db.init_schema(conn)
        rows = conn.execute(
            "SELECT * FROM worker_events WHERE run_id = ? ORDER BY id ASC LIMIT ?",
            (run_id, limit),
        ).fetchall()
        return rows
    finally:
        conn.close()


def latest_event_for_run(run_id: int) -> sqlite3.Row | None:
    conn = db.connect()
    try:
        db.init_schema(conn)
        row = conn.execute(
            "SELECT * FROM worker_events WHERE run_id = ? ORDER BY id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        return row
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tailer
# ---------------------------------------------------------------------------

_TAILERS: dict[tuple[int | None, str], threading.Thread] = {}
_TAILER_STOPS: dict[tuple[int | None, str], threading.Event] = {}
_TAILER_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Forbidden-tool detector & PID killer
# ---------------------------------------------------------------------------
#
# Specialists run autonomously with no human in the loop. Tools that
# REQUIRE a human response (AskUserQuestion, plan-approval gates, etc.)
# will stall the run indefinitely if the specialist calls them. The
# tailer detects this signature in the stream-json and SIGTERMs the PID.

# Roles with `can_spawn_subagents: true` are allowed to invoke Task; we
# only kill on the genuinely-interactive set. EnterPlanMode is included
# because it gates the run on a human pressing Approve.
_FORBIDDEN_AUTONOMOUS_TOOLS = {
    "AskUserQuestion",
    "ExitPlanMode",
    "EnterPlanMode",
    "ScheduleWakeup",
    "CronCreate",
    "Workflow",
}


def _detect_forbidden_tool_use(parsed: dict, raw: str) -> str | None:
    """If this event is a tool_use for a forbidden tool, return its name."""
    kind = parsed.get("kind", "")
    if not kind.startswith("tool:"):
        return None
    tool_name = kind.split(":", 1)[1]
    # Codex uses lowercase, Claude uses TitleCase. Normalize.
    for f in _FORBIDDEN_AUTONOMOUS_TOOLS:
        if tool_name == f or tool_name.lower() == f.lower():
            return f
    # Defensive: also check raw JSON for `"name":"AskUserQuestion"` in case
    # the parser's kind extraction missed something.
    for f in _FORBIDDEN_AUTONOMOUS_TOOLS:
        if f'"name":"{f}"' in raw:
            return f
    return None


def _kill_run_pid(run_id: int) -> None:
    """Look up the run's PID and send SIGTERM. Best-effort."""
    import signal
    conn = db.connect()
    try:
        row = conn.execute("SELECT pid FROM runs WHERE id = ?", (run_id,)).fetchone()
        pid = row["pid"] if row else None
    finally:
        conn.close()
    if not pid:
        return
    try:
        os.kill(int(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    # Mark the run row so the UI shows why it died.
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE runs SET status = 'killed' WHERE id = ? AND status IN ('launched','running')",
            (run_id,),
        )
        conn.commit()
    finally:
        conn.close()


def start_tailer(
    *,
    jsonl_path: Path | str,
    run_id: int | None = None,
    task_id: int | None = None,
    phase_id: int | None = None,
    provider: str = "claude",
    poll_interval: float = 0.3,
    idle_timeout: float = 300.0,
) -> None:
    """Start a background thread that tails `jsonl_path` and inserts events.

    `provider` selects the parser ("claude" | "codex" | "gemini").
    Idempotent: re-calling for the same (run_id, jsonl_path) is a no-op.
    Thread exits when the file hasn't grown in `idle_timeout` seconds.
    """
    key = (run_id, str(jsonl_path))
    with _TAILER_LOCK:
        existing = _TAILERS.get(key)
        if existing and existing.is_alive():
            return
        stop = threading.Event()
        _TAILER_STOPS[key] = stop
        t = threading.Thread(
            target=_run_tailer,
            kwargs={
                "jsonl_path": Path(jsonl_path),
                "run_id": run_id,
                "task_id": task_id,
                "phase_id": phase_id,
                "provider": provider,
                "stop": stop,
                "poll_interval": poll_interval,
                "idle_timeout": idle_timeout,
            },
            name=f"event-tailer({run_id or '-'}:{Path(jsonl_path).name})",
            daemon=True,
        )
        _TAILERS[key] = t
        t.start()


def stop_tailer(*, jsonl_path: Path | str, run_id: int | None = None) -> None:
    key = (run_id, str(jsonl_path))
    with _TAILER_LOCK:
        stop = _TAILER_STOPS.pop(key, None)
        _TAILERS.pop(key, None)
    if stop:
        stop.set()


def _run_tailer(
    *,
    jsonl_path: Path,
    run_id: int | None,
    task_id: int | None,
    phase_id: int | None,
    provider: str,
    stop: threading.Event,
    poll_interval: float,
    idle_timeout: float,
) -> None:
    pos = 0
    seq = 0
    # Resume-safe: on a backend restart, recover_orphan_runs re-attaches a tailer
    # to a still-running log. Without this we'd re-read from byte 0 and re-INSERT
    # the run's entire event history every restart (duplicating the feed/graph).
    # Seed seq + byte offset past the events already persisted for this run.
    if run_id is not None:
        try:
            conn = db.connect()
            try:
                row = conn.execute(
                    "SELECT MAX(seq) AS m, COUNT(*) AS c FROM worker_events WHERE run_id=?",
                    (run_id,),
                ).fetchone()
            finally:
                conn.close()
            already = int(row["c"] or 0) if row else 0
            if already > 0:
                seq = int(row["m"] or 0) + 1
                if jsonl_path.exists():
                    with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
                        for _ in range(already):
                            if not f.readline():
                                break
                        pos = f.tell()
        except Exception:
            pass
    buf = ""
    last_grow = time.time()

    while not stop.is_set():
        try:
            if jsonl_path.exists():
                size = jsonl_path.stat().st_size
                if size > pos:
                    with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
                        f.seek(pos)
                        chunk = f.read()
                        pos = f.tell()
                    buf += chunk
                    # Drain complete lines.
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        parsed = parse_stream_json(line, provider=provider)
                        if parsed is None:
                            continue
                        # Forbidden-tool guard. The Autonomy banner already
                        # tells specialists not to use these, but if a
                        # specialist invokes one anyway we kill it
                        # immediately rather than waiting indefinitely for
                        # a human response that will never come.
                        forbidden_tool = _detect_forbidden_tool_use(parsed, line)
                        if forbidden_tool and run_id is not None:
                            try:
                                insert_event(
                                    run_id=run_id, task_id=task_id, phase_id=phase_id,
                                    seq=seq,
                                    kind="error",
                                    summary=(
                                        f"killed: specialist called forbidden tool "
                                        f"{forbidden_tool!r} (Autonomy contract violation)"
                                    ),
                                    raw=line, extra=None,
                                )
                                seq += 1
                                _kill_run_pid(run_id)
                            except Exception:
                                pass
                            return  # stop tailing — the run is dead.
                        try:
                            insert_event(
                                run_id=run_id, task_id=task_id, phase_id=phase_id,
                                seq=seq, kind=parsed["kind"], summary=parsed["summary"],
                                raw=line, extra=parsed.get("extra"),
                            )
                            seq += 1
                            # Auto-stop after a `result` event (Claude is done).
                            if parsed.get("kind") == "result":
                                return
                        except Exception:
                            # Swallow DB hiccups; the tailer must never crash.
                            pass
                    last_grow = time.time()
            if time.time() - last_grow > idle_timeout:
                return
        except Exception:
            # Defensive: keep the thread alive across transient FS errors.
            pass
        stop.wait(poll_interval)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def jsonl_path_for_log(log_path: Path | str) -> Path:
    """Mirror of the convention in workers/claude.py: <stem>.jsonl next to .log"""
    p = Path(log_path)
    return p.with_suffix(".jsonl")
