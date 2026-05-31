"""Tests for the lifecycle hook bus (`johnstudio.hooks`).

Coverage:
- subscribe / unsubscribe (sync and async)
- sync dispatch is inline
- async dispatch runs on the daemon executor (off-thread)
- handler exceptions are swallowed (don't break emit or other subs)
- event log is written one JSON-per-line to `<JH_HOME>/events.jsonl`
- `EventTypes.all()` exposes every required canonical event name
- the arc-webhook subscriber is registered on `arc.terminal`
"""
from __future__ import annotations

import json
import threading

import pytest

from johnstudio.hooks import EventTypes, HookBus, bus as global_bus


@pytest.fixture
def fresh_bus():
    """A brand-new HookBus so tests don't share global subscriber state."""
    b = HookBus()
    yield b
    b.clear()


@pytest.fixture(autouse=True)
def _reset_global_bus(jh_home):
    """Snapshot+restore global bus subscriptions so the arc_webhook
    auto-subscription registered at import time isn't lost between
    tests, and so test-added subs don't leak across tests."""
    # Snapshot
    with global_bus._lock:
        snap_by_event = {k: list(v) for k, v in global_bus._subs_by_event.items()}
        snap_by_token = dict(global_bus._subs_by_token)
        snap_next = global_bus._next_token
    yield
    with global_bus._lock:
        global_bus._subs_by_event = snap_by_event
        global_bus._subs_by_token = snap_by_token
        global_bus._next_token = snap_next


# ---------------------------------------------------------------------------
# EventTypes
# ---------------------------------------------------------------------------

REQUIRED_EVENTS = {
    "worker.spawned", "worker.died", "worker.killed", "worker.failed",
    "task.created", "task.transitioned", "task.completed", "task.merged",
    "artifact.landed",
    "plan.landed", "plan.approved", "review.completed",
    "arc.iter_complete", "arc.terminal",
}


def test_event_types_cover_required_set():
    declared = set(EventTypes.all())
    missing = REQUIRED_EVENTS - declared
    assert not missing, f"EventTypes missing: {missing}"


def test_event_types_are_strings():
    for name in EventTypes.all():
        assert isinstance(name, str) and "." in name


# ---------------------------------------------------------------------------
# Subscribe / emit / unsubscribe (sync)
# ---------------------------------------------------------------------------

def test_subscribe_and_emit_sync(fresh_bus):
    seen: list[tuple[str, dict]] = []
    fresh_bus.subscribe(EventTypes.WORKER_SPAWNED, lambda e, p: seen.append((e, p)))
    fresh_bus.emit(EventTypes.WORKER_SPAWNED, {"task_id": 1, "worker": "a"})
    assert seen == [(EventTypes.WORKER_SPAWNED, {"task_id": 1, "worker": "a"})]


def test_unsubscribe_removes_handler(fresh_bus):
    calls = []
    token = fresh_bus.subscribe(EventTypes.TASK_TRANSITIONED, lambda e, p: calls.append(p))
    fresh_bus.emit(EventTypes.TASK_TRANSITIONED, {"task_id": 1, "old": "a", "new": "b"})
    assert fresh_bus.unsubscribe(token) is True
    fresh_bus.emit(EventTypes.TASK_TRANSITIONED, {"task_id": 1, "old": "b", "new": "c"})
    assert len(calls) == 1
    # Second unsubscribe of the same token is a no-op.
    assert fresh_bus.unsubscribe(token) is False


def test_subscribers_count(fresh_bus):
    assert fresh_bus.subscribers() == 0
    t1 = fresh_bus.subscribe(EventTypes.WORKER_DIED, lambda e, p: None)
    t2 = fresh_bus.subscribe(EventTypes.WORKER_DIED, lambda e, p: None)
    fresh_bus.subscribe(EventTypes.ARC_TERMINAL, lambda e, p: None)
    assert fresh_bus.subscribers(EventTypes.WORKER_DIED) == 2
    assert fresh_bus.subscribers(EventTypes.ARC_TERMINAL) == 1
    assert fresh_bus.subscribers() == 3
    fresh_bus.unsubscribe(t1)
    fresh_bus.unsubscribe(t2)
    assert fresh_bus.subscribers(EventTypes.WORKER_DIED) == 0


# ---------------------------------------------------------------------------
# Async dispatch
# ---------------------------------------------------------------------------

def test_async_dispatch_runs_off_thread(fresh_bus):
    """An async handler must run on a thread that is not the emitter's."""
    emitter_thread_id = threading.get_ident()
    done = threading.Event()
    seen: dict = {}

    def handler(event, payload):
        seen["tid"] = threading.get_ident()
        seen["payload"] = payload
        done.set()

    fresh_bus.subscribe_async(EventTypes.ARTIFACT_LANDED, handler)
    fresh_bus.emit(EventTypes.ARTIFACT_LANDED, {"task_id": 7})
    assert done.wait(timeout=2.0), "async handler did not run within timeout"
    assert seen["tid"] != emitter_thread_id
    assert seen["payload"] == {"task_id": 7}


def test_wait_idle_returns_when_executor_drains(fresh_bus):
    counter = {"n": 0}
    ev = threading.Event()

    def handler(event, payload):
        counter["n"] += 1
        ev.set()

    fresh_bus.subscribe_async(EventTypes.PLAN_LANDED, handler)
    fresh_bus.emit(EventTypes.PLAN_LANDED, {})
    assert ev.wait(timeout=2.0)
    assert fresh_bus.wait_idle(timeout_s=2.0) is True
    assert counter["n"] == 1


# ---------------------------------------------------------------------------
# Handler-error isolation
# ---------------------------------------------------------------------------

def test_handler_exception_is_swallowed(fresh_bus):
    calls = []

    def bad(event, payload):
        raise RuntimeError("boom")

    def good(event, payload):
        calls.append(payload)

    fresh_bus.subscribe(EventTypes.WORKER_FAILED, bad)
    fresh_bus.subscribe(EventTypes.WORKER_FAILED, good)
    # Must not raise.
    fresh_bus.emit(EventTypes.WORKER_FAILED, {"run_id": 1})
    assert calls == [{"run_id": 1}]


def test_async_handler_exception_is_swallowed(fresh_bus):
    done = threading.Event()

    def bad(event, payload):
        done.set()
        raise RuntimeError("async boom")

    fresh_bus.subscribe_async(EventTypes.REVIEW_COMPLETED, bad)
    fresh_bus.emit(EventTypes.REVIEW_COMPLETED, {})
    assert done.wait(timeout=2.0)
    # Executor still healthy after a bad handler — a follow-up emit works.
    seen = []
    fresh_bus.subscribe_async(EventTypes.REVIEW_COMPLETED, lambda e, p: seen.append(p))
    fresh_bus.emit(EventTypes.REVIEW_COMPLETED, {"ok": True})
    assert fresh_bus.wait_idle(timeout_s=2.0) is True
    assert seen == [{"ok": True}]


# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------

def test_event_log_writes_one_json_per_line(jh_home):
    """The module-level bus uses config.home_dir() so the JH_HOME env
    override (set by the `jh_home` fixture) routes the log into tmp."""
    global_bus.emit(EventTypes.TASK_CREATED, {"task_id": 99, "title": "x"})
    global_bus.emit(EventTypes.TASK_COMPLETED, {"task_id": 99})
    log_path = jh_home / "events.jsonl"
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    # Other tests may have already written entries; assert ours land.
    parsed = [json.loads(ln) for ln in lines]
    events = [(p["event"], p["payload"]) for p in parsed]
    assert ("task.created", {"task_id": 99, "title": "x"}) in events
    assert ("task.completed", {"task_id": 99}) in events
    for p in parsed:
        assert "ts" in p and isinstance(p["ts"], str)


def test_event_log_records_when_no_subscribers(jh_home):
    """Bus log must capture events even if nothing is subscribed."""
    global_bus.emit(EventTypes.PLAN_APPROVED, {"task_id": 123})
    log = (jh_home / "events.jsonl").read_text(encoding="utf-8")
    assert "plan.approved" in log
    assert "123" in log


# ---------------------------------------------------------------------------
# arc_webhook subscriber wiring
# ---------------------------------------------------------------------------

def test_arc_webhook_subscriber_is_registered(jh_home):
    # Importing arc_webhook registers a subscriber on the global bus.
    from johnstudio import arc_webhook  # noqa: F401
    assert global_bus.subscribers(EventTypes.ARC_TERMINAL) >= 1


def test_arc_terminal_subscriber_skips_silently_on_missing_payload(jh_home):
    # Subscriber must not raise when required keys are absent.
    from johnstudio import arc_webhook  # noqa: F401
    # Should be a complete no-op (no exception) — payload lacks cfg/state.
    global_bus.emit(EventTypes.ARC_TERMINAL, {"arc_name": "missing-cfg"})
