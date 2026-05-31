"""Terminal-state arc webhook.

When an iteration arc reaches a terminal state (cleared / exhausted /
over_budget / failed), `iteration_arc._fire_terminal_hooks` emits an
`ARC_TERMINAL` event carrying the live `cfg` and `state` objects. This
module registers a *synchronous* subscriber on that event (at import
time) which POSTs a small JSON payload to `cfg.webhook_url` when one is
configured.

Design notes:
  - Importing this module is what wires the subscriber onto the global
    bus (see `iteration_arc` import list + tests/test_hooks.py).
  - The subscriber is best-effort and must NEVER raise: the bus catches
    subscriber exceptions, but we also guard here so a malformed payload
    or a network error just records a skipped/failed delivery on the
    arc state instead of disrupting the stepper.
  - Idempotent: once `state.webhook_fired_at` is set we don't re-fire,
    so repeated ticks on an already-terminal arc don't spam the endpoint.
  - We use stdlib urllib so this carries no third-party dependency.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime

from .hooks import EventTypes, bus

_log = logging.getLogger("johnstudio.arc_webhook")

_TIMEOUT_S = 10.0


def _now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")


def fire_if_needed(cfg, state, *, arc_folder=None) -> bool:
    """POST the terminal-state webhook if one is configured and unfired.

    Returns True if a request was actually attempted (regardless of HTTP
    outcome), False if skipped (no url, or already fired). Records the
    outcome on `state.webhook_fired_at / webhook_ok / webhook_detail`.

    Best-effort: any exception is swallowed and recorded.
    """
    url = getattr(cfg, "webhook_url", None)
    if not url:
        # Nothing to do; mark as "considered" so we don't re-check forever.
        if getattr(state, "webhook_fired_at", None) is None:
            state.webhook_fired_at = _now()
            state.webhook_ok = None
            state.webhook_detail = "no webhook_url configured"
        return False

    if getattr(state, "webhook_fired_at", None) is not None:
        return False  # already fired (idempotent)

    last = state.iterations[-1] if getattr(state, "iterations", None) else {}
    body = {
        "arc_name": getattr(cfg, "name", None),
        "project_name": getattr(cfg, "project_name", None),
        "status": getattr(state, "status", None),
        "final_iter": last.get("iter", 0),
        "reason": last.get("reason", ""),
    }
    if arc_folder is not None:
        body["arc_folder"] = str(arc_folder)

    state.webhook_fired_at = _now()
    try:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            code = getattr(resp, "status", None) or resp.getcode()
        state.webhook_ok = 200 <= int(code) < 300
        state.webhook_detail = f"HTTP {code}"
        _log.info("arc %s: webhook POST -> %s (HTTP %s)", body["arc_name"], url, code)
    except urllib.error.HTTPError as e:
        state.webhook_ok = False
        state.webhook_detail = f"HTTP {e.code}"
        _log.warning("arc %s: webhook HTTP error %s", body["arc_name"], e.code)
    except Exception as e:  # network/timeout/anything — never raise
        state.webhook_ok = False
        state.webhook_detail = f"{type(e).__name__}: {e}"
        _log.warning("arc %s: webhook delivery failed: %s", body["arc_name"], e)
    return True


def _on_arc_terminal(event: str, payload: dict) -> None:
    """Bus subscriber for `arc.terminal`. Skips silently if the payload
    doesn't carry the live cfg/state objects (e.g. a test emit)."""
    cfg = payload.get("cfg")
    state = payload.get("state")
    if cfg is None or state is None:
        # Required objects absent — nothing actionable, skip silently.
        return
    try:
        fired = fire_if_needed(cfg, state, arc_folder=payload.get("arc_folder"))
    except Exception:  # defensive — fire_if_needed already guards internally
        _log.exception("arc_webhook subscriber failed")
        return
    if fired:
        # Persist the firing outcome back to STATE.json when we know where
        # it lives. RECONSTRUCTED: iteration_arc writes STATE.json itself
        # after the synchronous emit returns, so re-persisting here is
        # belt-and-suspenders rather than strictly required.
        arc_folder = payload.get("arc_folder")
        if arc_folder:
            try:
                from pathlib import Path
                state.to_json(Path(arc_folder) / "STATE.json")
            except Exception:
                _log.debug("arc_webhook: could not re-persist STATE.json", exc_info=True)


# Import-time wiring: registering the subscriber is the side effect that
# makes `from johnstudio import arc_webhook` activate webhook delivery.
bus.subscribe(EventTypes.ARC_TERMINAL, _on_arc_terminal)
