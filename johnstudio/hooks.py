"""In-process publish/subscribe event bus for johnstudio.

Today the only event side-effect is the arc-terminal webhook (see
`arc_webhook.py`) — a single hard-coded HTTP POST baked into
`iteration_arc._fire_terminal_hooks`. As the system grows we want
background workers (status regenerators, observability sinks,
notification fan-out, future hooks like Slack / shell / file-tailers)
to react to lifecycle events without modifying orchestrator core code.

This module is that seam:

    from johnstudio.hooks import bus, EventTypes
    token = bus.subscribe(EventTypes.WORKER_DIED, my_handler)
    bus.emit(EventTypes.WORKER_DIED, {"task_id": 45, ...})
    bus.unsubscribe(token)

Design notes
------------
- **Stdlib only.** No pydantic, no asyncio, no third-party brokers.
- **Sync vs. async handlers.** `subscribe` registers a synchronous
  handler that runs inline during `emit`. `subscribe_async` registers
  one that gets dispatched on a daemon `ThreadPoolExecutor`, so a slow
  handler can never block the orchestrator hot path.
- **Crash isolation.** Every handler call is wrapped — a thrown
  exception is logged and swallowed. One bad subscriber must not
  break `emit()` or the other subscribers.
- **Event log.** Every emit is appended as one JSON line to
  `<JOHNSTUDIO_HOME>/events.jsonl`. Re-uses `config.home_dir()` so the
  override env var works in tests. The log write is itself crash-safe
  (best-effort, exceptions logged).
- **Thread safety.** Subscriber registration / dispatch are guarded by
  a re-entrant lock — a handler is free to subscribe / unsubscribe
  during dispatch without deadlocking.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import config

_log = logging.getLogger("johnstudio.hooks")

Handler = Callable[[str, dict], None]


class EventTypes:
    """Canonical event-name constants. String values are the wire format.

    Keep these stable — they appear in the on-disk event log and any
    out-of-process consumer (a tail of `events.jsonl`) will key on them.
    """

    WORKER_SPAWNED = "worker.spawned"
    WORKER_DIED = "worker.died"
    WORKER_KILLED = "worker.killed"
    WORKER_FAILED = "worker.failed"
    TASK_CREATED = "task.created"
    TASK_TRANSITIONED = "task.transitioned"
    TASK_COMPLETED = "task.completed"
    TASK_MERGED = "task.merged"
    ARTIFACT_LANDED = "artifact.landed"
    PLAN_LANDED = "plan.landed"
    PLAN_APPROVED = "plan.approved"
    REVIEW_COMPLETED = "review.completed"
    ARC_ITER_COMPLETE = "arc.iter_complete"
    ARC_TERMINAL = "arc.terminal"
    # Item 20 — expanded coverage so consumers can react to liveness, commits,
    # MCP-tool usage, and budget pressure without polling.
    WORKER_EXITED = "worker.exited"            # subprocess exited (any code)
    GIT_COMMITTED = "git.committed"            # a commit landed in a worktree
    MCP_TOOL_CALLED = "mcp.tool_called"        # a worker invoked an MCP tool
    COST_THRESHOLD_CROSSED = "cost.threshold_crossed"  # task cost crossed a band

    @classmethod
    def all(cls) -> list[str]:
        """Return every declared canonical event-name string."""
        return [
            v
            for k, v in vars(cls).items()
            if not k.startswith("_") and isinstance(v, str)
        ]


class _Subscription:
    __slots__ = ("token", "event", "handler", "is_async")

    def __init__(self, token: int, event: str, handler: Handler, is_async: bool):
        self.token = token
        self.event = event
        self.handler = handler
        self.is_async = is_async


class HookBus:
    """In-process event bus. Singleton; access via the module-level `bus`."""

    def __init__(self, *, max_async_workers: int = 4):
        self._lock = threading.RLock()
        self._subs_by_event: dict[str, list[_Subscription]] = {}
        self._subs_by_token: dict[int, _Subscription] = {}
        self._next_token = 1
        self._executor: ThreadPoolExecutor | None = None
        self._max_async_workers = max_async_workers

    # -- subscription -----------------------------------------------------

    def subscribe(self, event: str, handler: Handler) -> int:
        """Register a SYNCHRONOUS handler. Returns a token for unsubscribe."""
        return self._add(event, handler, is_async=False)

    def subscribe_async(self, event: str, handler: Handler) -> int:
        """Register an ASYNCHRONOUS handler — runs in a daemon worker pool.

        Use this for anything that does I/O (HTTP, big disk writes) so
        the emitter's hot path stays fast.
        """
        return self._add(event, handler, is_async=True)

    def _add(self, event: str, handler: Handler, *, is_async: bool) -> int:
        with self._lock:
            token = self._next_token
            self._next_token += 1
            sub = _Subscription(token, event, handler, is_async)
            self._subs_by_event.setdefault(event, []).append(sub)
            self._subs_by_token[token] = sub
            return token

    def unsubscribe(self, token: int) -> bool:
        """Remove a subscription. Returns True iff a sub was removed."""
        with self._lock:
            sub = self._subs_by_token.pop(token, None)
            if sub is None:
                return False
            subs = self._subs_by_event.get(sub.event)
            if subs is not None:
                try:
                    subs.remove(sub)
                except ValueError:
                    pass
                if not subs:
                    self._subs_by_event.pop(sub.event, None)
            return True

    def subscribers(self, event: str | None = None) -> int:
        """Return the count of subscriptions (for an event or in total)."""
        with self._lock:
            if event is None:
                return len(self._subs_by_token)
            return len(self._subs_by_event.get(event, ()))

    def clear(self) -> None:
        """Drop every subscription. Tests use this for isolation."""
        with self._lock:
            self._subs_by_event.clear()
            self._subs_by_token.clear()

    # -- emit / dispatch --------------------------------------------------

    def emit(self, event: str, payload: dict | None = None) -> None:
        """Publish an event. Never raises. Never blocks on async handlers.

        Order of operations:
        1. Append to `events.jsonl` (best-effort).
        2. Snapshot subscribers under the lock.
        3. Dispatch sync handlers inline (each guarded).
        4. Hand async handlers to the daemon executor.
        """
        if payload is None:
            payload = {}
        ts = _utcnow_iso()
        self._append_log({"ts": ts, "event": event, "payload": payload})
        with self._lock:
            subs = list(self._subs_by_event.get(event, ()))
        for sub in subs:
            if sub.is_async:
                self._dispatch_async(sub, event, payload)
            else:
                self._dispatch_sync(sub, event, payload)

    def _dispatch_sync(self, sub: _Subscription, event: str, payload: dict) -> None:
        try:
            sub.handler(event, payload)
        except Exception:
            _log.exception("hook handler (sync) raised for event=%s", event)

    def _dispatch_async(self, sub: _Subscription, event: str, payload: dict) -> None:
        ex = self._get_executor()
        try:
            ex.submit(self._run_async, sub, event, payload)
        except RuntimeError:
            # Executor shut down (e.g. interpreter teardown) — run inline.
            self._dispatch_sync(sub, event, payload)

    def _run_async(self, sub: _Subscription, event: str, payload: dict) -> None:
        try:
            sub.handler(event, payload)
        except Exception:
            _log.exception("hook handler (async) raised for event=%s", event)

    def _get_executor(self) -> ThreadPoolExecutor:
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=self._max_async_workers,
                    thread_name_prefix="jh-hooks",
                )
            return self._executor

    # -- event log --------------------------------------------------------

    def _event_log_path(self) -> Path:
        return config.home_dir() / "events.jsonl"

    def _append_log(self, record: dict) -> None:
        """Append a single JSON line. Crash-safe."""
        try:
            p = self._event_log_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(record, default=_json_default) + "\n"
            with p.open("a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            _log.exception(
                "hook event-log append failed for %s", record.get("event")
            )

    # -- test helpers -----------------------------------------------------

    def wait_idle(self, timeout_s: float = 5.0) -> bool:
        """Block until the async executor drains (best-effort). Test helper.

        We don't expose pending-task introspection on ThreadPoolExecutor
        directly, so we re-submit a no-op and wait for it; if every prior
        task completes before ours, the queue is idle.
        """
        with self._lock:
            ex = self._executor
        if ex is None:
            return True
        try:
            fut = ex.submit(lambda: None)
        except RuntimeError:
            return True
        try:
            fut.result(timeout=timeout_s)
            return True
        except Exception:
            return False


def _utcnow_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _json_default(o: Any):
    if isinstance(o, Path):
        return str(o)
    if isinstance(o, datetime):
        return o.isoformat()
    try:
        return repr(o)
    except Exception:
        return "<unrepr>"


bus = HookBus()

__all__ = ["bus", "HookBus", "EventTypes", "Handler"]
