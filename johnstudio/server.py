"""FastAPI app that wraps the JohnStudio backend for local UI consumption.

Binds to 127.0.0.1 only by default. CORS opens just to the Vite dev origin
(http://localhost:5173) so the React app can talk to it during development.

Loopback bearer-token auth (see auth.py) protects every /api/* route
except /api/health. Tests can build the app with `create_app(require_auth=False)`.

Observability: stdlib logging, a request-ID middleware that tags every
request with an X-Request-ID header, and a catch-all exception handler that
logs the traceback and returns a structured 500.
"""
from __future__ import annotations

import logging
import uuid

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .api.routes_arcs import router as arcs_router
from .api.routes_chain import router as chain_router
from .api.routes_memory import router as memory_router
from .api.routes_projects import router as projects_router
from .api.routes_skills import project_skills_router, router as skills_router
from .api.routes_stream import router as stream_router
from .api.routes_system import public_router as system_public_router, router as system_router
from .api.routes_tasks import router as tasks_router
from .api.routes_team import router as team_router
from .api.routes_transcripts import router as transcripts_router
from .api.routes_workers import router as workers_router
from .auth import PUBLIC_PATHS, require_token


_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s req=%(request_id)s %(message)s"


def _configure_logging() -> None:
    """Idempotent: safe to call from create_app() in tests and prod."""
    root = logging.getLogger()
    if any(getattr(h, "_johnstudio", False) for h in root.handlers):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    handler._johnstudio = True  # type: ignore[attr-defined]
    handler.addFilter(_RequestIdFilter())
    root.addHandler(handler)
    root.setLevel(logging.INFO)


class _RequestIdFilter(logging.Filter):
    """Inject request_id into log records; defaults to '-' outside a request."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = "-"
        return True


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Generate/propagate X-Request-ID and stash it on request.state."""

    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        request.state.request_id = rid
        try:
            response = await call_next(request)
        except Exception:
            # Let the exception_handler render the 500. Re-raise so the
            # registered handler sees it.
            raise
        response.headers["x-request-id"] = rid
        return response


def create_app(*, require_auth: bool = True) -> FastAPI:
    _configure_logging()
    log = logging.getLogger("johnstudio.server")
    # Ensure the schema (including any newly-added tables like
    # `worker_events`) exists on startup so endpoints that connect ad-hoc
    # don't crash with "no such table".
    from . import db as _db
    _c = _db.connect()
    try:
        _db.init_schema(_c)
    finally:
        _c.close()

    # Team-mode autonomous loop: recover orphan runs from the prior
    # backend run, then start the background ticker that calls
    # advance_team_task every 5s on non-terminal team tasks. Without
    # this, a specialist that writes DONE.md silently sits at
    # status='running' until the user clicks advance.
    try:
        from . import team_orchestrator as _to
        recovery = _to.recover_orphan_runs()
        log.info(
            "team orchestrator recovery: %s", recovery,
            extra={"request_id": "-"},
        )
        _to.start_ticker(interval_seconds=5.0)
    except Exception:
        log.exception("team orchestrator startup hook failed",
                      extra={"request_id": "-"})

    # Background workers: auto-reactive daemons that subscribe to hook
    # bus events (arc.iter_complete, task.merged, ...) and self-trigger
    # status regen / worktree GC / BUILDLOG appends. registry.start()
    # is purely an in-process subscribe — never blocks on external
    # CLIs or services, so an Ollama/git outage does not delay startup.
    try:
        from . import background_workers as _bgw
        from . import workers_bg as _wbg
        _wbg.register_all(_bgw.registry)
        _bgw.registry.start()
        log.info(
            "background workers started: %s",
            [w.name for w in _bgw.registry.workers()],
            extra={"request_id": "-"},
        )
    except Exception:
        log.exception("background workers startup hook failed",
                      extra={"request_id": "-"})

    # Watchdog daemon: reaps dead-PID, idle, and over-runtime workers so tasks
    # converge instead of hanging on a stuck/slow specialist. It was previously
    # never started — workers could run unbounded. Run it in a daemon thread.
    try:
        import threading
        from . import watchdog as _wd
        threading.Thread(
            target=_wd.run_forever,
            kwargs={"idle_minutes": 10, "max_runtime_minutes": 15, "poll_seconds": 15},
            name="watchdog", daemon=True,
        ).start()
        log.info("watchdog started (idle=10min, max_runtime=15min)",
                 extra={"request_id": "-"})
    except Exception:
        log.exception("watchdog startup hook failed", extra={"request_id": "-"})

    app = FastAPI(
        title="JohnStudio",
        version="0.1.0",
        description="Local-first AI dev-team orchestrator. Local-only API.",
    )
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",   # Vite dev
            "http://127.0.0.1:5173",
            "tauri://localhost",       # future Tauri shell
        ],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.on_event("shutdown")
    async def _stop_background_workers() -> None:
        # Symmetric teardown for the bg worker registry. Idempotent;
        # in-flight daemon threads finish on their own.
        try:
            from . import background_workers as _bgw
            _bgw.registry.stop()
        except Exception:
            log.exception("background workers shutdown failed",
                          extra={"request_id": "-"})

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        rid = getattr(request.state, "request_id", "-")
        log.exception(
            "unhandled exception path=%s method=%s",
            request.url.path, request.method,
            extra={"request_id": rid},
        )
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "request_id": rid},
            headers={"x-request-id": rid},
        )

    # Authed routers: every /api/* route gets the bearer-token gate unless
    # require_auth=False (tests) or the path is in PUBLIC_PATHS (handled
    # inside the dependency by the system router below).
    deps = [Depends(require_token)] if require_auth else []

    # /health is intentionally unauthenticated so the UI can show a
    # connection badge before the user has loaded the token. Everything
    # else, including /doctor, sits behind the bearer-token gate.
    app.include_router(system_public_router)
    app.include_router(system_router, dependencies=deps)
    app.include_router(projects_router, dependencies=deps)
    app.include_router(tasks_router, dependencies=deps)
    app.include_router(skills_router, dependencies=deps)
    app.include_router(project_skills_router, dependencies=deps)
    app.include_router(memory_router, dependencies=deps)
    app.include_router(workers_router, dependencies=deps)
    app.include_router(chain_router, dependencies=deps)
    app.include_router(team_router, dependencies=deps)
    app.include_router(arcs_router, dependencies=deps)
    app.include_router(transcripts_router, dependencies=deps)
    # SSE: gates auth itself (so it can accept ?token= for EventSource);
    # not behind the global Bearer dependency.
    app.include_router(stream_router)

    log.info(
        "johnstudio app created (auth=%s, public_paths=%d)",
        require_auth, len(PUBLIC_PATHS),
        extra={"request_id": "-"},
    )
    return app


app = create_app()


def serve(host: str = "127.0.0.1", port: int = 8765, reload: bool = False) -> None:
    """Run the server via uvicorn programmatically."""
    import uvicorn
    # Surface the token path so the user knows where the UI/CLI reads it from.
    from .auth import get_or_create_token, token_path
    get_or_create_token()  # ensure the file exists before the UI starts
    logging.getLogger("johnstudio.server").info(
        "server token written to %s", token_path(),
        extra={"request_id": "-"},
    )
    if reload:
        uvicorn.run("johnstudio.server:app", host=host, port=port, reload=True)
    else:
        uvicorn.run(app, host=host, port=port, reload=False)
