"""Item 17 — transient-vs-permanent worker-exit classifier (spawner.classify_exit)."""
from __future__ import annotations

import pytest

from johnstudio import spawner


@pytest.mark.parametrize("tail", [
    "anthropic.RateLimitError: 429 Too Many Requests",
    "Error: rate limit exceeded, retry after 30s",
    "google.api_core.exceptions.ResourceExhausted: 429 Quota exceeded",
    "the model is currently Overloaded, please try again",
    "HTTP/1.1 429",
])
def test_rate_limit_is_transient(tail):
    assert spawner.classify_exit(tail=tail, exit_code=None) == "rate_limit"


@pytest.mark.parametrize("tail,code", [
    ("", 137),                                   # OOM-killer signature
    ("Out of memory: Killed process 1234", None),
    ("RuntimeError: CUDA out of memory", None),
    ("MemoryError", None),
    ("Cannot allocate memory", None),
])
def test_oom_is_transient(tail, code):
    assert spawner.classify_exit(tail=tail, exit_code=code) == "oom"


@pytest.mark.parametrize("tail,code", [
    ("", 0),                                       # clean exit
    ("Traceback ... AssertionError: expected 2", 1),
    ("ImportError: no module named foo", 1),
    ("Task complete. Wrote DONE.md", 0),
    (None, None),
])
def test_normal_failures_are_permanent(tail, code):
    assert spawner.classify_exit(tail=tail, exit_code=code) == "permanent"


def test_rate_limit_wins_over_oom_marker_noise():
    # A 429 body that also happens to contain "killed" should classify as
    # rate_limit (retryable for the same reason, but tracked distinctly).
    tail = "429 rate limit; previous worker was killed"
    assert spawner.classify_exit(tail=tail, exit_code=None) == "rate_limit"


def test_read_tail_missing_file_returns_none(tmp_path):
    assert spawner._read_tail(tmp_path / "nope.log") is None
    assert spawner._read_tail(None) is None


def test_read_tail_truncates_to_last_bytes(tmp_path):
    p = tmp_path / "w.log"
    p.write_text("A" * 100 + "RATE LIMIT")
    tail = spawner._read_tail(p, max_bytes=16)
    assert tail is not None and len(tail) <= 16
    assert "RATE LIMIT" in tail
