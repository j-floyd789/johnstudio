"""Built-in background workers and the registration helper.

`workers_bg` ships the small set of auto-reactive daemons JohnStudio runs
in-process: they subscribe to hook-bus lifecycle events and perform a
side effect off the emitter thread (see `..background_workers` for the
framework). server.py wires them up on boot via::

    from . import background_workers as _bgw
    from . import workers_bg as _wbg
    _wbg.register_all(_bgw.registry)
    _bgw.registry.start()

`register_all(registry)` instantiates one of each worker and registers it.
Keep this list in sync with the workers in this package.
"""
from __future__ import annotations

from .buildlog_append import BuildlogAppendWorker
from .status_regen import StatusRegenWorker
from .worktree_gc import WorktreeGCWorker

__all__ = [
    "BuildlogAppendWorker",
    "StatusRegenWorker",
    "WorktreeGCWorker",
    "register_all",
]


def register_all(registry) -> None:
    """Register every built-in background worker on `registry`.

    Order is not significant — each worker subscribes independently. The
    test suite asserts exactly these three names register, so any new
    worker added to this package should be appended here.

    Idempotent: server.create_app() calls this on every startup (and tests
    build many apps against the shared module-level registry), so we clear
    first to avoid 'duplicate worker name' on re-registration.
    """
    if hasattr(registry, "clear"):
        registry.clear()
    registry.register(StatusRegenWorker())
    registry.register(BuildlogAppendWorker())
    registry.register(WorktreeGCWorker())
