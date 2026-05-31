
























































































































"""One-prompt arc launcher.

Turns a single English prompt into a fully-configured iteration arc: derives
the arc name, materializes a default predicate + plan template inside the arc
folder, writes ARC.yaml/STATE.json via `iteration_arc.create_arc`, and
(optionally) kicks off iteration 1 so the user can walk away.

# RECONSTRUCTED: the module header (imports, helpers, default templates) was
# lost to the iCloud event and had no file-history backup. It is rebuilt from
# the exact symbols `launch_from_prompt` (the surviving original body) calls:
# derive_arc_name, _slugify, _persist_webhook_url, _DEFAULT_PREDICATE_PY,
# _DEFAULT_PLAN_TEMPLATE. Behavior matches those call sites; the default
# predicate/plan wording is a faithful best-effort, not the byte-exact original.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import yaml

from . import iteration_arc


# A no-op default predicate: never auto-stops, so a freshly-launched arc runs
# to max_iterations unless the user supplies a real predicate. Signature matches
# iteration_arc's contract: def predicate(artifact: dict) -> (stop, reason).
_DEFAULT_PREDICATE_PY = '''"""Default arc predicate — replace with a real success test."""


def predicate(artifact: dict) -> tuple[bool, str]:
    # Return (True, reason) to stop the arc early. By default we never stop,
    # so the arc runs until max_iterations is exhausted.
    return False, "no stop condition (default predicate)"
'''


# Plan template the team follows each iteration. `.format()`-ed with seed_text
# and artifact_basename at launch; `{{prior_summary}}` is left literal for the
# arc to substitute with the previous iteration's artifact on each tick.
_DEFAULT_PLAN_TEMPLATE = """# Arc plan

## Goal
{seed_text}

## Each iteration
1. Read the prior iteration's result below and build on it — do not restart
   from the prompt.
2. Do the work toward the goal.
3. Emit your result as `artifacts/{artifact_basename}_v<iter>.json` so the
   predicate and the next iteration can read it.

## Prior iteration summary
{{prior_summary}}
"""


def _slugify(text: str, *, max_len: int = 40) -> str:
    """Lowercase, hyphenate, strip to a filesystem/branch-safe slug."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    if max_len and len(s) > max_len:
        s = s[:max_len].rstrip("-")
    return s


def derive_arc_name(prompt: str, *, now: datetime | None = None) -> str:
    """Derive a stable, readable arc name from the prompt.

    # RECONSTRUCTED: the original derivation is unknown; this slugs the prompt
    # (the override `arc_name=` is used wherever a specific name matters).
    """
    base = _slugify(prompt, max_len=40) or "arc"
    return base


def _persist_webhook_url(arc_yaml_path: Path, webhook_url: str) -> None:
    """Write webhook_url back onto an existing ARC.yaml so it survives restarts."""
    try:
        data = yaml.safe_load(arc_yaml_path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        data = {}
    data["webhook_url"] = webhook_url
    arc_yaml_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def launch_from_prompt(
    *,
    repo: Path,
    project_name: str,
    prompt: str,
    arc_name: str | None = None,
    max_iterations: int = 10,
    webhook_url: str | None = None,
    now: datetime | None = None,
    auto_advance: bool = True,
    approve_func=None,
) -> dict:
    """Create + (optionally) kick off an arc derived from a single prompt.

    Parameters
    ----------
    repo : Path
        The project repo path (NOT the johnstudio repo). The arc folder
        is created under `<repo>/.johnstudio/arcs/<arc_name>/`.
    project_name : str
        Registered project name in JohnStudio's DB.
    prompt : str
        One-line English description. Used as the seed_text and to
        derive the arc name.
    arc_name : str | None
        Override the auto-derived name. Useful for tests / scripted
        relaunches. If omitted, derived from the prompt.
    max_iterations : int
        Hard cap on how many iterations the arc will spawn before giving
        up. Default 10 matches the existing arcs.
    webhook_url : str | None
        If set, POSTed to with arc-completion details when the arc
        terminates. Delivered best-effort; see arc_webhook.py.
    auto_advance : bool
        If True (default), spawn iteration 1 immediately. Tests / dry
        runs pass False to inspect the arc folder without launching.
    approve_func : callable | None
        Override for iteration_arc.step_arc's approval func — tests
        inject a stub so they don't actually spawn claude subprocesses.

    Returns
    -------
    dict
        {
          "arc_name": str,
          "arc_folder": str,
          "status": "spawned" | "created" | ...
          "iter": int,                # 1 if spawned, 0 if not
          "task_number": int | None,
        }
    """
    if not prompt or not prompt.strip():
        raise ValueError("prompt must be non-empty")

    name = arc_name or derive_arc_name(prompt, now=now)
    af = repo / ".johnstudio" / "arcs" / name
    af.mkdir(parents=True, exist_ok=True)

    # Materialize default predicate + plan template inside the arc folder
    # so subsequent ticks find them at the same absolute paths the
    # ArcConfig records. Don't overwrite if user has hand-edited them
    # (idempotent re-launch).
    pred_path = af / "predicate.py"
    if not pred_path.exists():
        pred_path.write_text(_DEFAULT_PREDICATE_PY, encoding="utf-8")

    artifact_basename = _slugify(prompt, max_len=16) or "arc"
    artifact_glob = f"artifacts/{artifact_basename}_v{{iter}}.json"

    plan_path = af / "plan_template.md"
    if not plan_path.exists():
        plan_path.write_text(
            _DEFAULT_PLAN_TEMPLATE.format(
                seed_text=prompt.strip(),
                artifact_basename=artifact_basename,
            ),
            encoding="utf-8",
        )

    # Reuse the existing create_arc to write ARC.yaml + STATE.json.
    iteration_arc.create_arc(
        repo=repo,
        name=name,
        project_name=project_name,
        plan_template_path=str(plan_path),
        predicate_path=str(pred_path),
        artifact_glob=artifact_glob,
        seed_text=prompt.strip(),
        max_iterations=max_iterations,
    )

    # Persist the webhook URL onto ARC.yaml so it survives server
    # restarts — it's read back in iteration_arc.step_arc via ArcConfig.
    if webhook_url:
        _persist_webhook_url(af / "ARC.yaml", webhook_url)

    if not auto_advance:
        return {
            "arc_name": name,
            "arc_folder": str(af),
            "status": "created",
            "iter": 0,
            "task_number": None,
        }

    # Kick off iteration 1 right now so the user truly walks away.
    step = iteration_arc.step_arc(repo, name, approve_func=approve_func)
    return {
        "arc_name": name,
        "arc_folder": str(af),
        "status": step.get("status", "unknown"),
        "iter": step.get("iter", 0),
        "task_number": step.get("task_number"),
        "specialists_launched": step.get("specialists_launched"),
    }
