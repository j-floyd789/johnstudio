from __future__ import annotations

from johnstudio.circuit_breaker import (
    BreakerState,
    CircuitBreaker,
    normalize_error,
    repeated_failure,
)


def test_normalize_strips_volatile_bits():
    a = normalize_error("Error at /tmp/x/foo.py:42:3 addr 0xDEADBEEF count 17")
    b = normalize_error("Error at /tmp/y/foo.py:99:1 addr 0xCAFEBABE count 4")
    assert a == b


def test_repeated_failure_true_after_three_identical():
    reasons = ["fail at line 1", "fail at line 2", "fail at line 3"]
    assert repeated_failure(reasons, 3) is True


def test_repeated_failure_false_when_varied():
    reasons = ["alpha", "beta", "gamma"]
    assert repeated_failure(reasons, 3) is False


def test_repeated_failure_needs_minimum_count():
    assert repeated_failure(["same", "same"], 3) is False


def test_repeated_failure_ignores_empty_reasons():
    assert repeated_failure(["", "boom 1", "", "boom 2", "boom 3"], 3) is True


def test_breaker_trips_on_three_consecutive():
    cb = CircuitBreaker(max_repeats=3)
    assert cb.record_error("boom at line 1") is False
    assert cb.record_error("boom at line 2") is False
    assert cb.record_error("boom at line 3") is True
    assert cb.tripped and cb.state is BreakerState.OPEN


def test_progress_resets_counter():
    cb = CircuitBreaker(max_repeats=3)
    cb.record_error("same failure")
    cb.record_error("same failure")
    cb.record_error("")  # a clean iteration is progress
    assert cb.record_error("same failure") is False
    assert not cb.tripped


def test_edit_revert_churn_trips():
    cb = CircuitBreaker()
    tripped = False
    for h in ["A", "B", "A", "B", "A"]:
        tripped = cb.record_state(h)
    assert tripped and cb.tripped
    assert "churn" in (cb.reason or "")


def test_report_shape():
    rep = CircuitBreaker().report()
    assert {"state", "tripped", "reason", "repeat_count"} <= set(rep)
