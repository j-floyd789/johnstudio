# JohnStudio

[![CI](https://github.com/j-floyd789/johnstudio/actions/workflows/ci.yml/badge.svg)](https://github.com/j-floyd789/johnstudio/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

> *What are we building today?*

JohnStudio is a **local-first AI software studio**. You pick a repo, type one high-level task, and JohnStudio coordinates locally-authenticated AI coding CLIs (Claude Code, Codex, Gemini CLI, …) as a bounded team of workers operating in isolated git worktrees.

There are two surfaces: the **CLI** (always works) and a **local desktop UI** (FastAPI backend + React frontend, structured for Tauri packaging).

## What it is

- A **CLI orchestrator** that drives already-authenticated local CLIs through tmux.
- A deterministic **skill registry + router** that picks a small, relevant set of skills per agent (no giant CLAUDE.md).
- A per-task **context-pack builder** with explicit rule precedence and an output contract.
- A **git-worktree-per-agent** model so workers never collide.
- An **Obsidian-compatible knowledge graph** memory layer with YAML frontmatter, wiki links, tags, entities, and relationships.
- A **review + merge** flow that always requires human approval.
- A **local FastAPI** server + **React/Vite** UI on top, structured for future Tauri packaging.

## What it is not

- It does **not** call Anthropic/OpenAI/Gemini APIs in the MVP. It uses your existing local subscriptions by launching the CLIs you already log into.
- It does **not** scrape web UIs.
- It does **not** auto-merge. You always confirm.
- It does **not** spawn unbounded agents. Workers can only emit `HANDOFF_REQUEST.md`; the orchestrator decides whether to spawn more.

## Quickstart (60 seconds, no model usage)

```bash
pip install -e .
johnstudio init                                   # creates ~/.johnstudio, imports seeds
johnstudio add-project demo /path/to/your/repo
johnstudio run demo "add a hello endpoint" --stub-only   # runs the full pipeline offline
johnstudio status demo 1
```

`--stub-only` drives the entire orchestrate → collect → review → merge flow with the
local `terminal_stub` worker, so you can exercise everything before wiring up real CLIs.
For the desktop UI, see [UI quick start](#ui-quick-start) below.

## Status

MVP. CLI is end-to-end working. UI runs against the local API. See:
- `docs/research/repo_research_report.md` — Phase 0 research grounding the importer/router.
- `docs/ui_backend_readiness.md` — what's wired vs. coming-soon for the UI.
- `docs/architecture.md`, `docs/safety.md`, `docs/roadmap.md`.

---

## CLI quick start

```bash
pip install -e .
johnstudio init                                  # creates ~/.johnstudio, auto-imports seeds
johnstudio research                              # writes docs/research/repo_research_report.md
johnstudio add-project demo /path/to/your/repo
johnstudio run demo "add a hello endpoint" --stub-only
johnstudio status demo 1
johnstudio collect demo 1
johnstudio review demo 1
johnstudio merge demo 1 terminal_stub            # prompts y/N
```

`--stub-only` uses the local `terminal_stub` worker so the full pipeline runs offline with no model usage.

## UI quick start

You need two processes: the backend and the UI.

```bash
# 1. Install backend (once)
pip install -e .

# 2. Start the backend
johnstudio server                               # binds 127.0.0.1:8765
# or:  bash scripts/start_backend.sh

# 3. Start the UI (separate terminal)
bash scripts/start_ui.sh                        # runs npm install then npm run dev
```

Then open **http://localhost:5173**.

To start both at once (uses tmux if available):

```bash
bash scripts/dev_all.sh
```

To bootstrap a demo project + temp JohnStudio home:

```bash
bash scripts/demo_ui_flow.sh
```

### What the UI can do (real backend wiring)

- Greeting, project picker, system health badges (git/tmux/claude/codex/gemini/DB/workers).
- Add a project (real `add-project`).
- Run a task (real `orchestrator.run`), with stub-only toggle and max-agents control.
- Watch task status; collect/review/stop/cleanup/resume buttons hit real routes.
- Read context packs, results, diffs, logs, FINAL_REVIEW.md, MERGE_PLAN.md.
- Confirm merge in a modal that calls the real merger (returns 409 if working tree is dirty or confirmation is missing).
- Browse, enable/disable, pin skills. Manage skill sources (add/scan).
- Browse memory vault notes (with markdown rendering) and the knowledge-graph entity/relationship tables.
- Test the terminal_stub worker end-to-end.
- View safety rules (protected paths, dangerous commands, approval-required commands).

If a feature isn't backed by a real route, the UI shows a clearly-disabled state. No fake spinners.

### Tauri packaging (next step)

The UI is structured to wrap with Tauri without code changes. See `desktop/TAURI.md` for the exact `npm create tauri-app` command, config tweaks, and notes about running the Python backend alongside the bundled app.

---

## Team mode (planner → specialists → autonomous loops)

The newest and most ambitious mode. One prompt; the lead planner
(Gemini) reads the task + project memory + role catalog and writes a
structured `TEAM_PLAN.md`. An auto-spawned plan critic (product-manager)
scores it. After you approve, **8–20 specialists** spawn in parallel
under their VPs (Claude=Engineering, Codex=Quality, Gemini=Research).
A background ticker drives the state machine without human polling:
tests run in every editor worktree; failing tests spawn the debugger
and a revision pass; cross-VP review fires after specialists DONE;
`MERGE_PLAN.md` consolidates everything; you approve the merge.

```bash
# UI: Home page → Team radio → type prompt → Run
# CLI / API:
curl -X POST -H "Authorization: Bearer $(cat ~/.johnstudio/server_token)" \
  -H "content-type: application/json" \
  -d '{"task":"Add a /api/health endpoint","budget_usd":1.0}' \
  http://127.0.0.1:8765/api/projects/1/team/run
```

Full guide: [`docs/team-mode.md`](docs/team-mode.md). Architecture spec:
[`docs/rfc/0001-multi-vp-team.md`](docs/rfc/0001-multi-vp-team.md).
Role catalog: `seeds/roles/<claude_vp|codex_vp|gemini_vp>/<role>.md`.

## Chain mode (RFC → implement → review → merge)

The default `run` mode launches workers in parallel and the deterministic
reviewer picks a winner. **Chain mode** is closer to a real dev team: a
sequenced RFC → implement → code review → revise → merge loop with explicit
human gates and bounded revise rounds.

```bash
# launch a chain task — kicks off the RFC drafting phase
johnstudio chain run demo "Add /api/ping returning {pong: true}"

# drive it forward; each `advance` polls the current phase's artifacts and
# starts the next phase if no human gate is required
johnstudio chain advance demo <task_n>
johnstudio chain status  demo <task_n>

# human gates:
johnstudio chain approve-rfc demo <task_n>     # after rfc_review finishes
johnstudio chain reject-rfc  demo <task_n>
johnstudio chain merge       demo <task_n>     # after reviewing → pending_merge
johnstudio chain reject      demo <task_n>     # at conflict gate
```

Same flow in the UI: tick the **Chain mode** checkbox on the Home page when
launching a task. The Chain page shows the phase timeline + the RFC, RFC
review, code review, and implementer artifacts, plus the approval/merge
buttons.

State machine:

```
rfc_drafting → rfc_review → rfc_pending_approval ⏸
                                    ↓ approve
                            implementing → reviewing(1)
                                    ↓ verdict=approve  → pending_merge ⏸ → merged
                                    ↓ needs-changes    → revising → reviewing(2) → …
                                    ↓ (max 2 revise rounds)
                                                       → conflict ⏸ → merge | reject
```

Worker assignment per role (defaults to `claude_backend` for all):

```bash
johnstudio chain run demo "..." \
  --architect    claude_backend \
  --rfc-reviewer claude_backend \
  --implementer  claude_backend \
  --reviewer     claude_backend
```

You can mix providers (e.g. `--reviewer gemini_review`) once those CLIs are on
your PATH.

## Tests

```bash
pytest -q                # 129 tests, 1 skipped (tmux test if tmux absent)
cd desktop && npm run build   # tsc + vite build
```

## Layout

```
johnstudio/                  # backend package
  cli.py                     # Typer CLI
  server.py                  # FastAPI app (johnstudio server)
  api/                       # route modules + helpers
  ... orchestrator/collector/reviewer/merger/...
desktop/                     # React + Vite + TS + Tailwind UI
  src/
    api/client.ts            # talks to FastAPI
    components/              # Shell, Markdown, ui primitives
    pages/                   # Home, Project, Task, Skills, Agents, Safety, Settings
  TAURI.md                   # how to wrap with Tauri later
scripts/
  start_backend.sh
  start_ui.sh
  dev_all.sh
  demo_ui_flow.sh
seeds/                       # bundled seed skills + research report
docs/
```

## Troubleshooting

- **UI shows "Backend not running"**: start `johnstudio server` (or `bash scripts/start_backend.sh`). The Home page auto-reconnects every 5 s.
- **`tmux` test skipped**: tmux is optional; the orchestrator falls back to subprocess launching when tmux is absent.
- **Real CLI workers not available**: they only show as "available" if their binary is on `PATH` and you're logged in. The `terminal_stub` worker is always available so the pipeline can be exercised offline.
- **Auto-imported seeds aren't categorized as expected**: re-run `johnstudio init` (idempotent). If you've added external sources via `skill source add`, scan them and explicitly `skill enable` what you want active — imports default to `unreviewed` + `enabled: false`.
