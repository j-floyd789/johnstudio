"""Tests for the background-worker framework (`johnstudio.background_workers`).

Coverage:
- registry tracks workers, subscribes to bus on start()
- start() / stop() / register() / unregister() / clear() lifecycle
- throttle coalesces bursts (N events in T < throttle_seconds → handler runs once)
- handler exception isolation (one throwing worker doesn't break others)
- recent_runs() returns at most the last 20 runs
- register() validates name and events
"""
from __future__ import annotations

import threading
import time

import pytest

from johnstudio.background_workers import (
    BackgroundWorker,
    WorkerRegistry,
)
from johnstudio.hooks import EventTypes, HookBus


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_bus():
    b = HookBus()
    yield b
    b.clear()


@pytest.fixture
def fresh_registry(fresh_bus):
    r = WorkerRegistry(bus=fresh_bus)
    yield r
    r.clear()


# ---------------------------------------------------------------------------
# Test workers
# ---------------------------------------------------------------------------

def _make_recorder_worker(name: str, events: list[str], throttle: int = 0):
    """A worker that records each handle() call and exposes call count."""
    calls: list[tuple[str, dict]] = []
    done = threading.Event()

    class _W(BackgroundWorker):
        pass

    _W.name = name
    _W.events = events
    _W.throttle_seconds = throttle

    def handle(self, event, payload):
        calls.append((event, dict(payload)))
        done.set()

    _W.handle = handle
    return _W(), calls, done


# ---------------------------------------------------------------------------
# Registry basics
# ---------------------------------------------------------------------------

def test_register_validates_name(fresh_registry):
    class Bad(BackgroundWorker):
        events = [EventTypes.TASK_MERGED]
        def handle(self, e, p): pass
    with pytest.raises(ValueError):
        fresh_registry.register(Bad())


def test_register_validates_events(fresh_registry):
    class Bad(BackgroundWorker):
        name = "no-events"
        events = []
        def handle(self, e, p): pass
    with pytest.raises(ValueError):
        fresh_registry.register(Bad())


def test_register_rejects_duplicate(fresh_registry):
    w1, _, _ = _make_recorder_worker("dup", [EventTypes.TASK_MERGED])
    w2, _, _ = _make_recorder_worker("dup", [EventTypes.TASK_MERGED])
    fresh_registry.register(w1)
    with pytest.raises(ValueError):
        fresh_registry.register(w2)


def test_registry_lists_registered_workers(fresh_registry):
    w, _, _ = _make_recorder_worker("alpha", [EventTypes.TASK_MERGED])
    fresh_registry.register(w)
    assert [x.name for x in fresh_registry.workers()] == ["alpha"]
    assert fresh_registry.get("alpha") is w
    assert fresh_registry.get("missing") is None


# ---------------------------------------------------------------------------
# start() subscribes; stop() unsubscribes
# ---------------------------------------------------------------------------

def test_start_subscribes_workers_to_bus(fresh_bus, fresh_registry):
    w, calls, done = _make_recorder_worker(
        "sub", [EventTypes.TASK_MERGED, EventTypes.ARC_ITER_COMPLETE],
    )
    fresh_registry.register(w)
    assert fresh_bus.subscribers(EventTypes.TASK_MERGED) == 0
    fresh_registry.start()
    assert fresh_bus.subscribers(EventTypes.TASK_MERGED) == 1
    assert fresh_bus.subscribers(EventTypes.ARC_ITER_COMPLETE) == 1


def test_start_is_idempotent(fresh_bus, fresh_registry):
    w, _, _ = _make_recorder_worker("ide", [EventTypes.TASK_MERGED])
    fresh_registry.register(w)
    fresh_registry.start()
    fresh_registry.start()
    assert fresh_bus.subscribers(EventTypes.TASK_MERGED) == 1


def test_stop_unsubscribes(fresh_bus, fresh_registry):
    w, _, _ = _make_recorder_worker("stp", [EventTypes.TASK_MERGED])
    fresh_registry.register(w)
    fresh_registry.start()
    fresh_registry.stop()
    assert fresh_bus.subscribers(EventTypes.TASK_MERGED) == 0


def test_register_after_start_auto_subscribes(fresh_bus, fresh_registry):
    fresh_registry.start()
    w, _, _ = _make_recorder_worker("late", [EventTypes.TASK_MERGED])
    fresh_registry.register(w)
    assert fresh_bus.subscribers(EventTypes.TASK_MERGED) == 1


def test_unregister_removes_subscriptions(fresh_bus, fresh_registry):
    w, _, _ = _make_recorder_worker("rm", [EventTypes.TASK_MERGED])
    fresh_registry.register(w)
    fresh_registry.start()
    assert fresh_registry.unregister("rm") is True
    assert fresh_bus.subscribers(EventTypes.TASK_MERGED) == 0
    assert fresh_registry.unregister("rm") is False  # idempotent


# ---------------------------------------------------------------------------
# Handler invocation through the bus
# ---------------------------------------------------------------------------

def test_emit_invokes_handler(fresh_bus, fresh_registry):
    w, calls, done = _make_recorder_worker("h", [EventTypes.TASK_MERGED])
    fresh_registry.register(w)
    fresh_registry.start()
    fresh_bus.emit(EventTypes.TASK_MERGED, {"task_id": 5})
    assert done.wait(timeout=2.0)
    # Tiny pause to allow the runner thread to finish writing recent_runs.
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not w.recent_runs():
        time.sleep(0.01)
    assert len(calls) == 1
    assert calls[0] == (EventTypes.TASK_MERGED, {"task_id": 5})
    runs = w.recent_runs()
    assert len(runs) == 1 and runs[0].ok is True


def test_handler_exception_does_not_break_other_workers(fresh_bus, fresh_registry):
    class Bad(BackgroundWorker):
        name = "bad"
        events = [EventTypes.TASK_MERGED]
        def handle(self, e, p):
            raise RuntimeError("boom")

    bad = Bad()
    good, calls, done = _make_recorder_worker("good", [EventTypes.TASK_MERGED])
    fresh_registry.register(bad)
    fresh_registry.register(good)
    fresh_registry.start()
    fresh_bus.emit(EventTypes.TASK_MERGED, {"task_id": 1})
    assert done.wait(timeout=2.0)
    # Wait for bad's runner to complete too.
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not bad.recent_runs():
        time.sleep(0.01)
    assert calls == [(EventTypes.TASK_MERGED, {"task_id": 1})]
    bad_runs = bad.recent_runs()
    assert len(bad_runs) == 1
    assert bad_runs[0].ok is False
    assert "RuntimeError" in (bad_runs[0].error or "")


# ---------------------------------------------------------------------------
# Throttle / coalesce
# ---------------------------------------------------------------------------

def test_throttle_coalesces_bursts(fresh_bus, fresh_registry):
    """N events fired in <throttle_seconds collapse to a single handle() call.

    Strategy: 1s throttle. Fire one event (runs immediately). Fire 4 more
    events back-to-back within the throttle window. The second runner
    must wait out the window then fire exactly once with the LATEST
    payload, coalescing all 4 bursts.
    """
    handle_calls: list[dict] = []
    handle_events: list[str] = []
    second_done = threading.Event()

    class W(BackgroundWorker):
        name = "throttled"
        events = [EventTypes.TASK_MERGED]
        throttle_seconds = 1
        def handle(self, e, p):
            handle_calls.append(dict(p))
            handle_events.append(e)
            if len(handle_calls) >= 2:
                second_done.set()

    w = W()
    fresh_registry.register(w)
    fresh_registry.start()

    # First emit: handler runs immediately.
    fresh_bus.emit(EventTypes.TASK_MERGED, {"i": 0})
    # Give the runner a moment to actually invoke handle().
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not handle_calls:
        time.sleep(0.01)
    assert handle_calls == [{"i": 0}]

    # Burst of 4 within the throttle window.
    for i in range(1, 5):
        fresh_bus.emit(EventTypes.TASK_MERGED, {"i": i})
        time.sleep(0.02)

    # Wait for the coalesced run.
    assert second_done.wait(timeout=3.0), f"second run never happened (calls={handle_calls})"
    # Exactly 2 handle() calls total: the immediate one + one coalesced.
    assert len(handle_calls) == 2, handle_calls
    # The coalesced run saw the LATEST payload.
    assert handle_calls[1] == {"i": 4}
    # The runs ring-buffer reflects coalescing.
    runs = w.recent_runs()
    assert len(runs) == 2
    assert runs[1].coalesced_count == 4


def test_no_throttle_runs_every_event(fresh_bus, fresh_registry):
    """throttle_seconds=0 means every event eventually produces a run."""
    calls: list[int] = []
    target = 5
    done = threading.Event()

    class W(BackgroundWorker):
        name = "nothrottle"
        events = [EventTypes.TASK_MERGED]
        throttle_seconds = 0
        def handle(self, e, p):
            calls.append(p["i"])
            if len(calls) >= target:
                done.set()

    fresh_registry.register(W())
    fresh_registry.start()
    for i in range(target):
        fresh_bus.emit(EventTypes.TASK_MERGED, {"i": i})
        time.sleep(0.01)
    assert done.wait(timeout=3.0)
    assert len(calls) == target


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------

def test_recent_runs_capped_at_20(fresh_bus, fresh_registry):
    done = threading.Event()
    counter = {"n": 0}
    target = 25

    class W(BackgroundWorker):
        name = "ring"
        events = [EventTypes.TASK_MERGED]
        throttle_seconds = 0
        def handle(self, e, p):
            counter["n"] += 1
            if counter["n"] >= target:
                done.set()

    w = W()
    fresh_registry.register(w)
    fresh_registry.start()
    for i in range(target):
        fresh_bus.emit(EventTypes.TASK_MERGED, {"i": i})
        time.sleep(0.005)
    assert done.wait(timeout=3.0)
    # Allow last run to finalize.
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and len(w.recent_runs()) < 20:
        time.sleep(0.01)
    runs = w.recent_runs()
    assert len(runs) <= 20
