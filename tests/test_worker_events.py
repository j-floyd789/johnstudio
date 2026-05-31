"""Coverage for worker_events: per-provider parsers, tailer, cost roll-up.

The three CLI providers (Claude, Codex, Gemini) each emit different
stream-json shapes. The parser dispatches by provider and the production
audit flagged that this module had zero tests — a schema change in any
provider would silently break the live tree.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from johnstudio import db, worker_events


# ---------------------------------------------------------------------------
# Claude parser
# ---------------------------------------------------------------------------

def test_claude_system_init_carries_session_and_cwd():
    line = json.dumps({
        "type": "system", "subtype": "init",
        "model": "claude-opus-4-7",
        "session_id": "abc-123",
        "cwd": "/Users/x/repo",
    })
    out = worker_events.parse_stream_json(line, "claude")
    assert out["kind"] == "system:init"
    assert "claude-opus" in out["summary"]
    assert out["extra"]["session_id"] == "abc-123"
    assert out["extra"]["cwd"] == "/Users/x/repo"


def test_claude_assistant_text_picks_first_line():
    line = json.dumps({
        "type": "assistant",
        "message": {
            "content": [{"type": "text", "text": "First line\nSecond line\nThird"}],
        },
    })
    out = worker_events.parse_stream_json(line, "claude")
    assert out["kind"] == "assistant_text"
    assert out["summary"] == "First line"


def test_claude_tool_use_extracts_path_hint():
    line = json.dumps({
        "type": "assistant",
        "message": {
            "content": [{
                "type": "tool_use", "name": "Edit", "id": "toolu_x",
                "input": {"file_path": "/Users/x/app.py", "old_string": "a", "new_string": "b"},
            }],
        },
    })
    out = worker_events.parse_stream_json(line, "claude")
    assert out["kind"] == "tool:Edit"
    assert "/Users/x/app.py" in out["summary"]
    assert out["extra"]["tool_use_id"] == "toolu_x"


def test_claude_task_tool_promotes_to_spawn_subagent():
    line = json.dumps({
        "type": "assistant",
        "message": {
            "content": [{
                "type": "tool_use", "name": "Task", "id": "toolu_task1",
                "input": {"subagent_type": "general-purpose", "prompt": "Research X"},
            }],
        },
    })
    out = worker_events.parse_stream_json(line, "claude")
    assert out["kind"] == "spawn:subagent"
    assert out["extra"]["subagent_type"] == "general-purpose"
    assert out["extra"]["brief"] == "Research X"
    assert out["extra"]["tool_use_id"] == "toolu_task1"


def test_claude_result_carries_cost_and_duration():
    line = json.dumps({
        "type": "result", "is_error": False,
        "total_cost_usd": 0.0234, "duration_ms": 5210, "num_turns": 3,
    })
    out = worker_events.parse_stream_json(line, "claude")
    assert out["kind"] == "result"
    assert out["extra"]["cost_usd"] == pytest.approx(0.0234)
    assert out["extra"]["duration_ms"] == 5210
    assert out["extra"]["num_turns"] == 3
    assert out["extra"]["is_error"] is False


def test_claude_rate_limit_rejected_is_flagged():
    line = json.dumps({
        "type": "rate_limit_event",
        "rate_limit_info": {"status": "rejected"},
    })
    out = worker_events.parse_stream_json(line, "claude")
    assert out["kind"] == "rate_limit"
    assert out["extra"]["rejected"] is True


def test_claude_tool_result_extracts_content_and_id():
    line = json.dumps({
        "type": "user",
        "message": {
            "content": [{
                "type": "tool_result", "tool_use_id": "toolu_task1",
                "content": [{"text": "Subagent's full reply\nwith details"}],
            }],
        },
    })
    out = worker_events.parse_stream_json(line, "claude")
    assert out["kind"] == "tool_result"
    assert out["extra"]["tool_use_id"] == "toolu_task1"
    assert "Subagent" in out["extra"]["content"]


# ---------------------------------------------------------------------------
# Codex parser
# ---------------------------------------------------------------------------

def test_codex_thread_started():
    out = worker_events.parse_stream_json(
        '{"type":"thread.started","thread_id":"019e7088-aaa"}', "codex",
    )
    assert out["kind"] == "system:init"
    assert "019e7088" in out["summary"]


def test_codex_item_command_execution_extracts_command_and_exit():
    line = json.dumps({
        "type": "item.started",
        "item": {"type": "command_execution",
                 "command": "/bin/zsh -lc 'pytest -q'",
                 "exit_code": 0, "aggregated_output": "ok"},
    })
    out = worker_events.parse_stream_json(line, "codex")
    assert out["kind"] == "tool:bash"
    assert "pytest -q" in out["summary"]
    assert "exit=0" in out["summary"]


def test_codex_item_file_change_lists_paths():
    line = json.dumps({
        "type": "item.completed",
        "item": {"type": "file_change",
                 "changes": [
                     {"path": "/x/app.py", "kind": "update"},
                     {"path": "/x/test.py", "kind": "add"},
                 ]},
    })
    out = worker_events.parse_stream_json(line, "codex")
    assert out["kind"] == "tool:edit"
    assert "app.py" in out["summary"]
    assert "test.py" in out["summary"]


def test_codex_turn_completed_carries_usage():
    line = json.dumps({
        "type": "turn.completed",
        "usage": {"input_tokens": 100, "output_tokens": 50},
    })
    out = worker_events.parse_stream_json(line, "codex")
    assert out["kind"] == "result"
    assert "input=100" in out["summary"]
    assert "output=50" in out["summary"]


# ---------------------------------------------------------------------------
# Gemini parser
# ---------------------------------------------------------------------------

def test_gemini_init():
    out = worker_events.parse_stream_json(
        '{"type":"init","model":"gemini-3-pro"}', "gemini",
    )
    assert out["kind"] == "system:init"
    assert "gemini-3-pro" in out["summary"]


def test_gemini_assistant_message():
    line = json.dumps({
        "type": "message", "role": "assistant",
        "content": "Hello world\nignored",
    })
    out = worker_events.parse_stream_json(line, "gemini")
    assert out["kind"] == "assistant_text"
    assert out["summary"] == "Hello world"


def test_gemini_result_with_stats():
    line = json.dumps({
        "type": "result", "status": "success",
        "stats": {"total_tokens": 1234, "duration_ms": 4200},
    })
    out = worker_events.parse_stream_json(line, "gemini")
    assert out["kind"] == "result"
    assert "1234 tok" in out["summary"]
    assert "4.2s" in out["summary"]


# ---------------------------------------------------------------------------
# Cost roll-up + budget
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_db(jh_home):
    """Initialized DB with one project + one task ready for cost testing."""
    conn = db.connect()
    db.init_schema(conn)
    cur = conn.execute(
        "INSERT INTO projects (name, repo_path) VALUES ('p', '/tmp') RETURNING id"
    )
    pid = int(cur.fetchone()["id"])
    cur = conn.execute(
        """INSERT INTO tasks (project_id, task_number, title, description, status, base_branch, budget_usd)
           VALUES (?,?,?,?,?,?,?) RETURNING id""",
        (pid, 1, "t", "t", "running", "main", 1.0),
    )
    tid = int(cur.fetchone()["id"])
    cur = conn.execute(
        """INSERT INTO workers (name, provider, role, command, can_edit, worktree_enabled)
           VALUES ('w','claude','x','claude',1,1) RETURNING id"""
    )
    wid = int(cur.fetchone()["id"])
    cur = conn.execute(
        """INSERT INTO runs (task_id, worker_id, status, started_at)
           VALUES (?,?,?,?) RETURNING id""",
        (tid, wid, "launched", "2026-01-01T00:00:00"),
    )
    rid = int(cur.fetchone()["id"])
    conn.commit()
    conn.close()
    return {"task_id": tid, "run_id": rid}


def test_insert_event_rolls_up_cost(fresh_db):
    """Two result events with cost_usd should accumulate into both
    runs.cost_usd and tasks.cost_usd."""
    worker_events.insert_event(
        run_id=fresh_db["run_id"], task_id=fresh_db["task_id"],
        phase_id=None, seq=0, kind="result", summary="success",
        raw="{}", extra={"cost_usd": 0.10},
    )
    worker_events.insert_event(
        run_id=fresh_db["run_id"], task_id=fresh_db["task_id"],
        phase_id=None, seq=1, kind="result", summary="success",
        raw="{}", extra={"cost_usd": 0.25},
    )
    bs = worker_events.task_cost_status(fresh_db["task_id"])
    assert bs["cost_usd"] == pytest.approx(0.35)
    assert bs["budget_usd"] == 1.0
    assert bs["over_budget"] is False


def test_task_cost_status_flags_over_budget(fresh_db):
    worker_events.insert_event(
        run_id=fresh_db["run_id"], task_id=fresh_db["task_id"],
        phase_id=None, seq=0, kind="result", summary="big",
        raw="{}", extra={"cost_usd": 1.5},
    )
    bs = worker_events.task_cost_status(fresh_db["task_id"])
    assert bs["over_budget"] is True


def test_insert_event_ignores_cost_on_non_result(fresh_db):
    worker_events.insert_event(
        run_id=fresh_db["run_id"], task_id=fresh_db["task_id"],
        phase_id=None, seq=0, kind="tool_result", summary="ok",
        raw="{}", extra={"cost_usd": 999.0},
    )
    bs = worker_events.task_cost_status(fresh_db["task_id"])
    assert bs["cost_usd"] == 0.0


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------

def test_parser_drops_non_json_noise():
    """Each CLI emits some stderr-style banners on stdout before its
    first JSON event. The parser must silently drop them rather than
    poison the event stream."""
    assert worker_events.parse_stream_json("YOLO mode is enabled.", "gemini") is None
    assert worker_events.parse_stream_json("", "claude") is None
    assert worker_events.parse_stream_json("   ", "codex") is None


def test_parser_unknown_event_type_still_recorded():
    """A new event type we didn't anticipate should still produce a
    parseable event — losing telemetry is worse than logging an
    unknown kind."""
    out = worker_events.parse_stream_json(
        '{"type":"future_event_kind","data":42}', "claude",
    )
    assert out is not None
    assert out["kind"] == "future_event_kind"
