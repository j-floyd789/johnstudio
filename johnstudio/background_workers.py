"""Background-worker framework — auto-reactive in-process daemons.

A `BackgroundWorker` subscribes to one or more lifecycle events on the
hook bus (see `johnstudio.hooks`) and runs a `handle(event, payload)`
side-effect off the emitter's thread. The framework provides:

  - lifecycle: register / unregister / start / stop / clear
  - per-worker dedicated runner thread so a slow handler can't block the
    bus emitter (which dispatches subscribers inline)
  - **throttle/coalesce**: with `throttle_seconds > 0`, the FIRST event
    fires immediately; a burst of events arriving within the window
    collapses into a single follow-up run carrying the LATEST payload,
    annotated with how many events it coalesced
  - **isolation**: a handler raising is caught, recorded as a failed run,
    and never breaks the bus or sibling workers
  - **observability**: `recent_runs()` returns the last 20 runs

`registry` is the process-wide singleton wired into the global bus and
started by the FastAPI app on boot (server.py).
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .hooks import HookBus, bus as _global_bus

_log = logging.getLogger("johnstudio.background_workers")

_MAX_RUNS = 20


@dataclass
class WorkerRun:
    """A single (possibly coalesced) invocation of a worker's handle()."""
    event: str
    started_at: str
    ok: bool
    coalesced_count: int = 1
    error: Optional[str] = None
    duration_ms: float = 0.0


class BackgroundWorker:
    """Base class. Subclasses set `name`, `events`, optional
    `throttle_seconds`, and implement `handle(self, event, payload)`."""

    name: str = ""
    events: list[str] = []
    throttle_seconds: int = 0

    def __init__(self) -> None:
        # Pending coalesced work: the latest (event, payload) seen while the
        # throttle window is open. Guarded by _lock.
        self._lock = threading.Lock()
        self._pending: Optional[tuple[str, dict]] = None
        self._coalesced = 0
        self._last_run_monotonic: float = 0.0
        self._runner: Optional[threading.Thread] = None
        self._runner_active = False
        self._runs: deque[WorkerRun] = deque(maxlen=_MAX_RUNS)

    # -- subclass contract --------------------------------------------------

    def handle(self, event: str, payload: dict) -> None:  # pragma: no cover
        raise NotImplementedError

    # -- observability ------------------------------------------------------

    def recent_runs(self) -> list[WorkerRun]:
        with self._lock:
            return list(self._runs)

    # -- bus callback -------------------------------------------------------

    def _on_event(self, event: str, payload: dict) -> None:
        """Bus subscriber callback. Runs inline on the emitter thread; we
        only enqueue + (maybe) spawn a runner so we never block emit()."""
        with self._lock:
            self._pending = (event, dict(payload))
            self._coalesced += 1
            if self._runner_active:
                # A runner is already in flight; it will pick up the latest
                # pending payload when it next drains.
                return
            self._runner_active = True
            self._runner = threading.Thread(
                target=self._run_loop, name=f"bgw-{self.name}", daemon=True,
            )
        self._runner.start()

    def _run_loop(self) -> None:
        """Drain pending work, honoring the throttle window between runs."""
        while True:
            with self._lock:
                if self._pending is None:
                    self._runner_active = False
                    return
                # Throttle: if we ran recently, wait out the remainder of
                # the window before firing again (the immediate first run
                # happens because _last_run_monotonic starts at 0).
                wait = 0.0
                if self.throttle_seconds and self._last_run_monotonic:
                    elapsed = time.monotonic() - self._last_run_monotonic
                    wait = self.throttle_seconds - elapsed
            if wait > 0:
                time.sleep(wait)
                # New events may have coalesced in while we slept; loop back
                # to grab the freshest pending payload.
                continue

            with self._lock:
                event, payload = self._pending
                coalesced = self._coalesced
                self._pending = None
                self._coalesced = 0
                self._last_run_monotonic = time.monotonic()

            self._invoke(event, payload, coalesced)

    def _invoke(self, event: str, payload: dict, coalesced: int) -> None:
        started = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
        t0 = time.monotonic()
        run = WorkerRun(
            event=event, started_at=started, ok=True, coalesced_count=coalesced,
        )
        try:
            self.handle(event, payload)
        except Exception as e:  # isolation: never propagate to bus/siblings
            run.ok = False
            run.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
            _log.warning("background worker %s failed: %s", self.name, e)
        finally:
            run.duration_ms = (time.monotonic() - t0) * 1000.0
            with self._lock:
                self._runs.append(run)


class WorkerRegistry:
    """Owns a set of workers and their subscriptions on a hook bus."""

    def __init__(self, bus: HookBus | None = None) -> None:
        self.bus = bus if bus is not None else _global_bus
        self._workers: dict[str, BackgroundWorker] = {}
        # worker name -> list of subscription tokens on the bus
        self._tokens: dict[str, list] = {}
        self._started = False
        self._lock = threading.Lock()

    # -- registration -------------------------------------------------------

    def register(self, worker: BackgroundWorker) -> None:
        if not getattr(worker, "name", ""):
            raise ValueError("worker must define a non-empty `name`")
        if not getattr(worker, "events", None):
            raise ValueError(f"worker {worker.name!r} must subscribe to >=1 event")
        with self._lock:
            if worker.name in self._workers:
                raise ValueError(f"duplicate worker name: {worker.name!r}")
            self._workers[worker.name] = worker
            started = self._started
        if started:
            self._subscribe(worker)

    def unregister(self, name: str) -> bool:
        with self._lock:
            worker = self._workers.pop(name, None)
            if worker is None:
                return False
            tokens = self._tokens.pop(name, [])
        for tok in tokens:
            self.bus.unsubscribe(tok)
        return True

    def clear(self) -> None:
        for name in list(self._workers.keys()):
            self.unregister(name)
        with self._lock:
            self._started = False

    # -- introspection ------------------------------------------------------

    def workers(self) -> list[BackgroundWorker]:
        with self._lock:
            return list(self._workers.values())

    def get(self, name: str) -> BackgroundWorker | None:
        with self._lock:
            return self._workers.get(name)

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            workers = list(self._workers.values())
        for w in workers:
            self._subscribe(w)

    def stop(self) -> None:
        with self._lock:
            if not self._started:
                return
            self._started = False
            all_tokens = list(self._tokens.items())
            self._tokens.clear()
        for _name, tokens in all_tokens:
            for tok in tokens:
                self.bus.unsubscribe(tok)

    # -- internals ----------------------------------------------------------

    def _subscribe(self, worker: BackgroundWorker) -> None:
        with self._lock:
            if worker.name in self._tokens:
                return  # already subscribed (idempotent)
            tokens: list = []
        for event in worker.events:
            tokens.append(self.bus.subscribe(event, worker._on_event))
        with self._lock:
            self._tokens[worker.name] = tokens


# Process-wide singleton wired to the global bus. server.py registers the
# built-in workers (workers_bg.register_all) and calls registry.start().
registry = WorkerRegistry(bus=_global_bus)
