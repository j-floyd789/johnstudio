"""Coverage for the Claude Code transcript discovery + parser."""
from __future__ import annotations

import json
from pathlib import Path

from johnstudio import transcripts


# ---------------------------------------------------------------------------
# Path encoding
# ---------------------------------------------------------------------------

def test_encode_cwd_simple():
    assert transcripts.encode_cwd("/Users/x/repo") == "-Users-x-repo"


def test_encode_cwd_dotted_directory():
    """A leading dot in a path segment becomes another `-` because we
    encode both `/` and `.` as `-`."""
    assert (
        transcripts.encode_cwd("/Users/john/Desktop/coolsite/.johnstudio/worktrees/x")
        == "-Users-john-Desktop-coolsite--johnstudio-worktrees-x"
    )


def test_encode_cwd_accepts_pathlib_path():
    assert transcripts.encode_cwd(Path("/a/b")) == "-a-b"


# ---------------------------------------------------------------------------
# Transcript reading
# ---------------------------------------------------------------------------

def _write_jsonl(p: Path, entries: list[dict]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


def test_read_transcript_assigns_index_and_summary(tmp_path):
    p = tmp_path / "session.jsonl"
    _write_jsonl(p, [
        {"type": "user", "message": {"content": "hi"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "/x/a.py"}}
        ]}},
    ])
    out = transcripts.read_transcript(p)
    assert len(out) == 3
    assert out[0]["_index"] == 0
    assert out[1]["_kind_summary"].startswith("assistant")
    assert "Edit" in out[2]["_kind_summary"]
    assert "/x/a.py" in out[2]["_kind_summary"]


def test_read_transcript_filters_sidechain(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "main"}]}},
        {"type": "assistant", "isSidechain": True,
         "message": {"content": [{"type": "text", "text": "subagent"}]}},
    ])
    only_side = transcripts.read_transcript(p, only_sidechain=True)
    no_side = transcripts.read_transcript(p, include_sidechain=False)
    assert len(only_side) == 1
    assert only_side[0]["isSidechain"] is True
    assert len(no_side) == 1
    assert "main" in no_side[0]["_kind_summary"]


def test_read_transcript_caps_at_limit(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [{"type": "user", "message": {"content": str(i)}} for i in range(50)])
    out = transcripts.read_transcript(p, limit=10)
    assert len(out) == 10


def test_find_recent_transcripts_returns_newest_first(tmp_path, monkeypatch):
    import os
    d = tmp_path / "-fake-cwd"
    d.mkdir()
    older = d / "old.jsonl"; older.write_text('{}\n'); os.utime(older, (1000, 1000))
    newer = d / "new.jsonl"; newer.write_text('{}\n'); os.utime(newer, (2000, 2000))
    monkeypatch.setattr(transcripts, "CLAUDE_PROJECTS_ROOT", tmp_path)
    out = transcripts.find_recent_transcripts("/fake/cwd")
    assert len(out) == 2
    assert out[0].name == "new.jsonl"


def test_read_transcript_skips_bad_lines(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(
        '{"type":"user","message":{"content":"ok"}}\n'
        'this-is-not-json\n'
        '{"type":"assistant","message":{"content":[{"type":"text","text":"yes"}]}}\n',
        encoding="utf-8",
    )
    out = transcripts.read_transcript(p)
    assert len(out) == 2  # bad line dropped
    assert out[0]["_kind_summary"].startswith("user")
    assert out[1]["_kind_summary"].startswith("assistant")
