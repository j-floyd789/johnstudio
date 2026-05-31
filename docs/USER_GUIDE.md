# JohnStudio User Guide

> Local-first AI dev-team orchestrator.
> One prompt → a planner → a real specialist team → tested, reviewed, merge-ready code.

---

## Table of contents

1. What JohnStudio is (and isn't)
2. Architecture at one glance
3. First-time setup (one machine, ever)
4. Adding a project (one per repo)
5. Your first task — start to merge
6. Three modes — when to use which
7. Team mode in depth
8. Parallel mode in depth
9. Chain mode in depth
10. The CLI, every command
11. The UI, every page
12. The 20-role catalog
13. Skills system
14. Memory vault
15. Standing rules
16. Budget + cost control
17. Safety model
18. Common project patterns + prompts that work
19. Troubleshooting
20. Appendix A — repo file layout
21. Appendix B — REST API
22. Appendix C — configuration

---

## 1. What JohnStudio is (and isn't)

JohnStudio runs **already-authenticated local AI coding CLIs** (Claude Code, OpenAI Codex CLI, Gemini CLI) as a bounded team of specialists, each in its own git worktree, with explicit roles and a deterministic state machine driving the work end-to-end. You type one prompt; the system plans, executes, tests, reviews, and presents a merge plan for human approval.

**It is:**
- A **local orchestrator** that drives `claude --print`, `codex exec`, `gemini -p` via subprocess + tmux.
- A **role catalog** with 20 specialized agents distributed across three "VPs" (Claude = Engineering, Codex = Quality, Gemini = Research/Strategy).
- A **planner-first workflow**: Gemini's lead-planner decomposes your task; an auto-critic scores the plan; then humans approve before any worker spawns.
- A **live observability layer** showing every tool call across every agent in a single graph, with full transcripts on demand.
- **Local-first**: no telemetry, no SaaS, your code never leaves your machine.

**It is NOT:**
- An API wrapper. It uses your existing local Anthropic/OpenAI/Google CLI subscriptions.
- A package manager for agents. Roles are markdown files you can copy/edit.
- An autopilot. The merge step is always human.
- A web scraper or browser automator.

---

## 2. Architecture at one glance

```
        ┌─────────────────────────────────────────────────┐
        │  Your prompt (UI radio or `johnstudio team run`)│
        └────────────────────┬────────────────────────────┘
                             ▼
              ┌────────────────────────────┐
              │ Gemini lead-planner         │ writes TEAM_PLAN.md
              └────────────────────────────┘
                             ▼  auto
              ┌────────────────────────────┐
              │ Plan critic (product-mgr)   │ writes PLAN_CRITIQUE.md
              └────────────────────────────┘
                             ▼  human approve
              ┌────────────────────────────┐
              │ N specialists in parallel   │
              │  Claude VP  Codex VP        │ each in its own git worktree
              │  Gemini VP                  │
              └────────────────────────────┘
                             ▼
              ┌────────────────────────────┐
              │ pcfg.test_commands run      │ inside every editor worktree
              └────────────────────────────┘
              tests pass                tests fail
                  ▼                          ▼
       ┌────────────────────┐    ┌────────────────────────┐
       │ Cross-VP review    │    │ Debugger spawn + auto  │
       │ each VP reads      │    │ revision pass          │
       │ another VP's output│    │ (≤ MAX_REVISE_ROUNDS)  │
       └─────────┬──────────┘    └───────────┬────────────┘
                 ▼                           │
       ┌────────────────────┐                │
       │ Architect arbiter  │◀───────────────┘  if conflicts
       │ (only if branches  │
       │  overlap files)    │
       └─────────┬──────────┘
                 ▼
       ┌────────────────────┐
       │ MERGE_PLAN.md      │ + per-role lessons appended to memory
       └─────────┬──────────┘
                 ▼  human merge
       ┌────────────────────┐
       │ git merge --no-ff  │
       │ branches → main    │
       └────────────────────┘
```

Background, in addition:
- A **5-second ticker** drives `advance_team_task` on every non-terminal team task; no human polling required.
- A **stream-json event capture** pipeline reads every Claude/Codex/Gemini tool call live and renders it on the graph.
- An **on-disk transcript surface** (`~/.claude/projects/...`) gives full post-hoc replay of any agent including subagents.

---

## 3. First-time setup (one machine, ever)

Requirements:
- macOS or Linux
- Python 3.11+
- git
- tmux (optional, used for live pane attaching)
- The CLI(s) you want JohnStudio to drive — at least one of:
  - `claude` (Anthropic's Claude Code)
  - `codex` (OpenAI's Codex CLI)
  - `gemini` (Google's Gemini CLI)

```bash
# 1. Clone JohnStudio and install
git clone <your fork or https://github.com/...>/johnstudio
cd johnstudio
pip install -e .

# 2. Initialize the home directory (~/.johnstudio). Idempotent.
johnstudio init

# 3. Install the backend as a launchd agent so it survives reboot
#    and respawns on crash. Generates the bearer token at
#    ~/.johnstudio/server_token.
bash scripts/install_launch_agent.sh

# Backend now at http://127.0.0.1:8765. Confirm:
curl -sf http://127.0.0.1:8765/api/health
```

To launch the UI (optional — CLI is fully featured):

```bash
bash scripts/start_ui.sh
# Open http://localhost:5173
```

The UI script reads `~/.johnstudio/server_token` and exports it to Vite as `VITE_JOHNSTUDIO_TOKEN`, so the client authenticates automatically.

---

## 4. Adding a project (one per repo)

A "project" in JohnStudio is a registration that points at a git repo on your disk.

```bash
# Any folder with a git repo works
cd ~/Desktop/myrepo
git init -q -b main && echo "# myrepo" > README.md && git add . && git commit -q -m init

# Register it. The name is arbitrary; it's what you'll reference in commands.
johnstudio add-project myrepo .
```

This writes:
- `myrepo/.johnstudio/project.yaml` — project-specific config (test commands, protected paths, etc.).
- `myrepo/.johnstudio/memory/` — the per-project memory vault.
- A row in the JohnStudio DB tying `myrepo` to the absolute repo path.

You're done. The project shows up in the UI sidebar and in `johnstudio team catalog`.

---

## 5. Your first task — start to merge

The shortest possible end-to-end:

```bash
# 1. Start a task (any wording works; the planner decomposes it)
johnstudio team run myrepo "Add a /api/health endpoint that returns {ok: true}" --budget 1.0

# 2. Wait ~60 seconds for the planner. Check progress:
johnstudio team status myrepo 1

# 3. Once the plan + critique are visible, approve:
johnstudio team approve myrepo 1

# 4. The background ticker drives everything from here. Watch in the UI:
#    http://localhost:5173/p/1/team/1     ← task page (plan, status, budget)
#    http://localhost:5173/p/1/graph      ← live tree of every worker

# 5. Eventually status hits `pending_merge`. Review the merge plan:
cat ~/Desktop/myrepo/.johnstudio/tasks/task-0001/MERGE_PLAN.md

# 6. Merge from the UI or via the merger:
johnstudio merge myrepo 1 <winning-role>      # picks one branch
```

You can monitor cost any time:

```bash
johnstudio team budget myrepo 1
```

---

## 6. Three modes — when to use which

JohnStudio ships three execution shapes. They share workers, skills, and the live tree; they differ in how work is decomposed.

| Mode | Best for | How |
|---|---|---|
| **Parallel** | Quick task that one or two implementers can finish in a single shot | N implementers race in their own worktrees; the deterministic reviewer scores the diffs and picks a winner. |
| **Chain** | Tasks where you want explicit RFC → review → impl → review → merge phases for one focused change | Sequential phases gated by human approval at RFC and merge. Best for risky or contested decisions. |
| **Team** | Anything else — most real work | The lead-planner decides the team. 8–20 specialists fan out under three VPs. Auto-tests, auto-revise, auto-merge-plan. This is the recommended default. |

Mental shortcut: **parallel** is "race three drafts," **chain** is "RFC then code," **team** is "decompose and execute."

---

## 7. Team mode in depth

This is the headline mode and where most of JohnStudio's recent investment lives.

### Lifecycle

```
planning → (auto critique) → human approve → running → (auto tests) →
  reviewing → (auto revise if needs-changes) → pending_merge → human merge → merged
```

Each transition is gated by SQL-rowcount idempotency — two concurrent ticks can't both advance the state.

### The lead-planner

A read-only Gemini role (`seeds/roles/gemini_vp/lead-planner.md`) reads:
- Your task verbatim.
- The role catalog (it can only assign roles that exist).
- The project memory vault (`project_brief.md`, `current_state.md`, `architecture.md`).

It writes `TEAM_PLAN.md` with YAML inside fenced markdown so the orchestrator can parse it deterministically. The plan names a team, gives each member a one-sentence brief and an expected output path, and lists acceptance criteria.

### The plan critic

Immediately after the planner writes `TEAM_PLAN.md`, JohnStudio spawns the `product-manager` role (Sonnet) to score the plan. It writes `PLAN_CRITIQUE.md` with `## Verdict: approve | revise`, strengths, required changes, and coverage gaps. The UI auto-triggers this and shows both files side by side before you click Approve. Anthropic's Evaluator-Optimizer pattern applied to plan quality.

### Standing rules

Before the team spawns, the orchestrator augments the plan with `seeds/standing_rules.yaml` — deterministic always-on additions:

| Trigger | Added role |
|---|---|
| Any `*.py` in expected outputs | `test-automator` |
| Task mentions "add", "new endpoint", "new feature" | `technical-writer` |
| Task mentions auth/payment/sql/password keywords | `security-auditor` |
| Any `*.html`, `*.css`, `*.tsx`, `*.jsx` | `accessibility-auditor` |
| Any dependency manifest changed | `dependency-reviewer` |
| Unconditional | `code-reviewer` |

This is the "checklist that disappears into the runtime" pattern. Edit `seeds/standing_rules.yaml` to add your own triggers.

### The fan-out

Editor specialists get their own git worktrees:

```
<repo>/.johnstudio/worktrees/task-NNNN-team-<role>-<i>/
```

Read-only specialists run in the task folder and write artifacts there.

Every specialist receives a context pack containing:
- The role's system prompt (markdown body from the catalog).
- The full `context_builder` output: skills selected by the skill router for this role, project memory excerpts, scope, rule precedence.
- The plan's summary AND its `acceptance_criteria` — specialists know what they're being measured against.
- The role's accumulated lessons (`memory/agent_lessons/<role>.md`) and recent project decisions (`memory/decisions/*.md`).
- Their specific brief from the plan.

Per-role `model:` and `tools:` from frontmatter are honored: `--model` is plumbed through every adapter; `--allowed-tools` on Claude, with `Task` blocked at catalog-load time so specialists can't recursively spawn LLM subagents (RFC 0001 §Non-goals).

### Auto-tests as a signal

When all editor specialists DONE, the orchestrator runs `pcfg.test_commands` inside each editor's worktree (you set these in `<repo>/.johnstudio/project.yaml`; e.g. `pytest -q`).

If any fail and the task isn't over budget and `test_round < 2`:
1. The `debugger` role spawns in the failing worktree with the test output inlined. It writes `DEBUG_REPORT.md` (symptom / root cause / proposed fix / confidence).
2. The original implementer is re-spawned for a revision pass with the failing output (and the debug report if it's ready) inlined as feedback. It commits and writes a new DONE.md.
3. The next ticker tick re-runs tests. Loop ends when tests pass or the round/budget cap is hit.

### Cross-VP review

Once tests pass (or are skipped), the cross-VP block from the plan kicks in. Each VP's nominated reviewer reads artifacts from another VP — structural defense against single-VP groupthink. Reviewers write `CROSS_REVIEW_<role>_<i>.md` with `## Verdict: approve | needs-changes | reject`.

If any verdict is `needs-changes` (and within revise + budget caps), the orchestrator spawns one revision pass per editor with the review inlined. Then waits for the post-revision review. Mirrors chain mode's REVISING phase.

### Conflict arbiter

If two or more editor specialists touched the same file, `_generate_merge_plan` notices the overlap. It auto-spawns the `architect` role with the conflicting versions inlined; the architect writes `CONFLICT_RESOLUTION.md` with `Winner: <role>` per file. Anthropic's "lead synthesizer" pattern.

### Merge plan

The orchestrator writes `MERGE_PLAN.md` listing:
- Every branch and the files it touched.
- Stat summary per branch (`+N -M`).
- A preview of each `RESULT.md`.
- Conflict list with the arbiter's path if spawned.
- All cross-VP review findings inlined.
- A suggested `git merge --no-ff` sequence.

You read it, then merge from the UI or CLI.

### Memory + retros

At the same time, the orchestrator writes a per-task retro to `memory/runs/task-NNNN.md` and appends one durable lesson per role to `memory/agent_lessons/<role>.md`. **Next task's planner and specialists read those.** The "feels smarter every week" property.

---

## 8. Parallel mode in depth

The original mode. Useful when:
- The task is small enough for a single implementer.
- You want the deterministic reviewer to score 3–5 candidate diffs against each other.
- You want to A/B different worker configs (e.g. `claude_backend` vs `claude_frontend` on the same task).

```bash
johnstudio run myrepo "Add a /api/ping endpoint returning {pong: true}"
# Or with explicit workers:
johnstudio run myrepo "..." --workers claude_backend,claude_frontend --max-agents 4
```

Then:

```bash
johnstudio status myrepo 1
johnstudio collect myrepo 1     # gathers artifacts + diffs from all workers
johnstudio review myrepo 1      # deterministic reviewer picks a winner
johnstudio merge myrepo 1 claude_backend     # confirms the merge of the chosen branch
```

The `stub_only` flag uses the bundled `terminal_stub` worker — exercises the full pipeline without invoking real Claude:

```bash
johnstudio run myrepo "anything" --stub-only
```

---

## 9. Chain mode in depth

Use when you want explicit phases with a human gate after the architect's RFC.

```bash
johnstudio chain run myrepo "Migrate the sqlite schema to add a users.deleted_at column"
johnstudio chain advance myrepo 1     # ticks the state machine
johnstudio chain status myrepo 1
johnstudio chain approve-rfc myrepo 1     # after rfc_review phase
# ... implementing, reviewing(1), reviewing(2), pending_merge
johnstudio chain merge myrepo 1     # confirms the merge
```

The phases:

```
rfc_drafting → rfc_review → rfc_pending_approval ⏸
                                    ↓
                            implementing → reviewing(1)
                                    ↓ verdict=approve   → pending_merge ⏸ → merged
                                    ↓ needs-changes     → revising → reviewing(2)
                                                                          ↓
                                                            conflict ⏸ → merge | reject
```

Worker assignments are configurable:

```bash
johnstudio chain run myrepo "..." \
  --architect    claude_review \
  --rfc-reviewer claude_review \
  --implementer  claude_backend \
  --reviewer     gemini_review
```

---

## 10. The CLI, every command

### Setup

| Command | What it does |
|---|---|
| `johnstudio init` | Scaffold `~/.johnstudio`. Idempotent. |
| `johnstudio doctor` | Print system health (tools available, DB state). |
| `johnstudio server [--host --port]` | Start the FastAPI server in the foreground. Use `install_launch_agent.sh` for production. |

### Projects

| Command | What it does |
|---|---|
| `johnstudio add-project <name> <path>` | Register a git repo as a project. |
| `johnstudio list-projects` | Show all registered projects. |
| `johnstudio show-project <name>` | Print project config + stack + protected paths. |

### Team mode (recommended default)

| Command | What it does |
|---|---|
| `johnstudio team run <project> "<task>" [--budget USD]` | Start a team task; spawns the lead-planner. |
| `johnstudio team status <project> <task_n>` | Show current state, plan validity, assignments. |
| `johnstudio team plan-critic <project> <task_n>` | Manually trigger the plan critic (UI auto-triggers). |
| `johnstudio team approve <project> <task_n>` | Approve the plan, spawn every specialist. |
| `johnstudio team advance <project> <task_n>` | Manually tick the state machine. |
| `johnstudio team budget <project> <task_n>` | Cost + budget posture. |
| `johnstudio team catalog` | List all 20 roles grouped by VP. |

### Chain mode

| Command | What it does |
|---|---|
| `johnstudio chain run <project> "<task>" [--architect ...]` | Start a chain task at the RFC drafting phase. |
| `johnstudio chain advance <project> <task_n>` | Tick the state machine. |
| `johnstudio chain status <project> <task_n>` | Show phase table. |
| `johnstudio chain approve-rfc <project> <task_n>` | Approve the RFC; chain proceeds to implementing. |
| `johnstudio chain reject-rfc <project> <task_n>` | Reject; chain terminates. |
| `johnstudio chain merge <project> <task_n>` | Confirm the final merge. |
| `johnstudio chain reject <project> <task_n>` | Reject at the conflict gate. |

### Parallel mode

| Command | What it does |
|---|---|
| `johnstudio run <project> "<task>" [--workers --max-agents --stub-only]` | Spawn N parallel implementers. |
| `johnstudio status <project> <task_n>` | Show per-worker status. |
| `johnstudio collect <project> <task_n>` | Gather RESULT.md + diffs from all workers. |
| `johnstudio review <project> <task_n>` | Deterministic reviewer picks a winner. |
| `johnstudio merge <project> <task_n> <worker>` | Merge the chosen branch. |
| `johnstudio stop <project> <task_n>` | Kill all workers of a task. |
| `johnstudio cleanup <project> <task_n>` | Remove worktrees + reset state. |
| `johnstudio resume <project> <task_n> <worker>` | Re-send the prompt to a specific worker. |

### Memory

| Command | What it does |
|---|---|
| `johnstudio memory list <project>` | List notes in the memory vault. |
| `johnstudio memory show <project> <path>` | Print a memory note. |
| `johnstudio memory validate <project>` | Check vault structure. |
| `johnstudio memory repair <project>` | Re-create missing folders. |

### Skills

| Command | What it does |
|---|---|
| `johnstudio skill list [--enabled]` | List registered skills. |
| `johnstudio skill show <id>` | Print skill metadata + body. |
| `johnstudio skill enable <id>` / `disable <id>` | Toggle. |
| `johnstudio skill discover <project> "<task>"` | Preview which skills would route to a hypothetical task. |
| `johnstudio skill source add <uri>` | Register an external skill repo (git URL or local path). |
| `johnstudio skill source list` | Show registered sources. |
| `johnstudio skill source scan` | Re-scan all sources and import new skills. |

---

## 11. The UI, every page

UI runs at `http://localhost:5173` after `bash scripts/start_ui.sh`. It talks to the backend at `127.0.0.1:8765` with a Bearer token loaded from `~/.johnstudio/server_token` at startup.

### Sidebar

Always visible:
- **Home** → task launcher + health badges
- **Skills** → registered skills, enable/disable
- **Agents** → registered worker configs (for parallel/chain mode)
- **Safety** → protected paths, dangerous commands, approval-required commands
- **Settings** → JohnStudio configuration
- **Projects list** → click to jump to a project's pages

### Home page

- **System health** badges: tmux/git/claude/codex/gemini/DB/FTS5 + every configured worker. Green = available, orange = missing.
- **Project picker** + Add Project modal.
- **New task** card:
  - Textarea for the prompt.
  - **Mode** radios: Parallel | Chain | Team.
  - Stub-only checkbox (parallel mode only).
  - Max-agents dropdown (parallel mode only).
  - Run button → POST to `/api/projects/:id/{tasks,chain,team}/run`.

### Project page (`/p/:id`)

Tabbed view:
- **Overview** — stack info, test commands, pinned skills, base branch.
- **Tasks** — all tasks for this project, status badges.
- **Skills** — project-pinned skill list + pin/unpin.
- **Memory** — markdown vault browser with rendered notes.
- **Graph** — knowledge graph entities + relationships (different from live tree).
- **Safety** — per-project protected paths.
- **Settings** — edit project YAML.

Plus a **"● Live tree"** button in the top right that takes you to the live graph for this project.

### Live tree (`/p/:id/graph`)

The headline observability surface.

**Nodes:**
- Project root at top.
- One child per task.
- One child per worker run under each task (parallel + team).
- Phase chain visible vertically for chain mode tasks.
- Subagent child nodes (purple dashed edges) for every Claude `Task` tool call, with the brief in the side panel.

**Colors:**
- Status color (gray pending / blue pulsing running / green completed / orange human-gate / red failed / yellow stopped).
- **VP tint** on the border for team-mode roles: Claude blue, Codex amber, Gemini violet.
- **C/X/G chip** in the top-left corner of every VP-typed node.

**Click any node:**
- **Project node** → counts summary.
- **Task node** → task title + status.
- **Run node** → live event log (last 40 events) + "Open full transcript" button + (if subagents) "Subagent-only" button.
- **Subagent node** → brief sent + result received, with link to the full transcript.

**The transcript modal:**
- Reads `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl` from disk.
- One row per event with a one-line summary.
- `SIDE` chip marks sidechain (subagent) entries.
- Click any row to expand to its raw JSON.

### Team task page (`/p/:id/team/:n`)

For every team task you launched.

**Header:** task number + status + project + budget (cost spent vs cap).

**Planning state:**
- "Planner is thinking…" while waiting for `TEAM_PLAN.md`.
- Once the plan lands: formatted view grouped by VP (Claude Engineering / Codex Quality / Gemini Research), each row showing the role + brief + output path.
- Cross-VP review block underneath.
- Acceptance criteria list.
- **Plan critique** card (auto-triggered): the `PLAN_CRITIQUE.md` rendered.
- **"Approve & spawn N specialists"** button.
- Collapsible "Show raw TEAM_PLAN.md" details.

**Running state:**
- Assignments grouped by VP with status.
- Live updates via the same SSE stream as the graph.

### Chain task page (`/p/:id/c/:n`)

The phase timeline, the current phase's artifacts (RFC, RFC_REVIEW, RESULT, REVIEW_n), and the approval/merge buttons.

### Task page (`/p/:id/t/:n`)

Parallel-mode artifacts: per-worker status, context-pack viewer, results, diffs, logs, deterministic reviewer's verdict, merge confirmation.

### Skills page (`/skills`)

Browse, enable/disable, pin to project. Filter by category, by enabled-only.

### Agents page (`/agents`)

Registered worker configs (the `seeds/default_config.yaml` workers and any you've customized). Use this to verify `can_edit`, `worktree`, `model`, `provider` per worker.

### Safety page (`/safety`)

Read-only view of the safety policy: protected paths, dangerous commands, approval-required commands. Edit by changing `seeds/default_config.yaml` or `<repo>/.johnstudio/project.yaml`.

### Settings page (`/settings`)

JohnStudio's global config: max_active_agents, default_timeout_minutes, require_human_merge, memory toggles, FTS5 status.

---

## 12. The 20-role catalog

All roles live at `seeds/roles/<vp>/<role>.md`. Frontmatter declares `name`, `vp`, `provider`, `can_edit`, `model`, `tools`. The body becomes the system prompt.

### Claude VP (Engineering, 7 roles)

| Role | can_edit | What it does |
|---|---|---|
| `architect` | false | Reads codebase, writes ADRs and interface sketches. No code. |
| `backend-developer` | true | Server-side implementer: APIs, handlers, data layer. |
| `frontend-developer` | true | Client-side: UI, state, API client. |
| `fullstack-developer` | true | Small features spanning both halves. |
| `code-reviewer` | false | Reads diffs, writes structured REVIEW.md with verdict + required changes. |
| `debugger` | false | Reads failures, writes DEBUG_REPORT.md (root cause + proposed fix). |
| `database-administrator` | true | Schema, migrations, indexes. |

### Codex VP (Quality, 7 roles)

| Role | can_edit | What it does |
|---|---|---|
| `test-automator` | true | Writes pytest/jest tests. No production code edits. |
| `security-auditor` | false | OWASP-style threat model; SECURITY_REVIEW.md. |
| `performance-engineer` | false | Hot-path analysis; PERF_REPORT.md. |
| `refactoring-specialist` | true | Behavior-preserving cleanups only. |
| `devops-engineer` | true | CI configs, Dockerfiles, deploy scripts. |
| `accessibility-auditor` | false | WCAG check on UI changes; A11Y_REVIEW.md. |
| `dependency-reviewer` | false | Audits dep manifest changes; DEPS_REVIEW.md. |

### Gemini VP (Research / Strategy, 6 roles)

| Role | can_edit | What it does |
|---|---|---|
| `lead-planner` | false | **The keystone.** Decomposes the task into TEAM_PLAN.md. |
| `researcher` | false | Finds prior art / library options; RESEARCH.md. |
| `product-manager` | false | Acceptance criteria, scope; also runs as plan-critic. |
| `technical-writer` | true | Updates README, CHANGELOG, docs. |
| `ux-researcher` | false | User-flow notes; UX_NOTES.md. |
| `competitive-analyst` | false | How comparable products solve this; COMPETITIVE.md. |

### Customizing a role

Just edit the markdown:

```bash
# Example: change debugger to use Haiku for speed
vim seeds/roles/claude_vp/debugger.md
# In the frontmatter:
# model: claude-haiku-4-5
```

Or copy a role to create a new one:

```bash
cp seeds/roles/claude_vp/code-reviewer.md seeds/roles/claude_vp/security-code-reviewer.md
# edit the name + system prompt
# Restart the backend to pick up catalog changes.
```

---

## 13. Skills system

Skills are markdown files with frontmatter that get injected into a worker's context based on a deterministic skill router.

The router scores skills against a worker's:
- Task text (TF-IDF/BM25)
- Worker role (`agent_roles:` in skill frontmatter)
- Project stack (`languages:`, `frameworks:`)
- Project pinned skills

Top-N skills (by `max_skills_per_agent`) are injected, capped by token budget (`max_skill_tokens_per_agent`). All defaults live in global config under `skill_registry`.

```bash
# Browse skills
johnstudio skill list
johnstudio skill list --enabled
johnstudio skill list --category general-guidance

# Inspect
johnstudio skill show <skill-id>

# Toggle
johnstudio skill enable <skill-id>
johnstudio skill disable <skill-id>

# Preview routing for a hypothetical task
johnstudio skill discover myrepo "Add user authentication" --agent-role backend_implementer
```

### Adding external skill libraries

```bash
# Register a git repo (e.g., wshobson/agents or anthropics/skills)
johnstudio skill source add https://github.com/wshobson/agents

# Or a local path
johnstudio skill source add /path/to/my/skill-library

# Scan + import. Imported skills land disabled + trust=unreviewed.
johnstudio skill source scan
johnstudio skill list --imported

# Enable selectively after reviewing
johnstudio skill enable <skill-id>
```

---

## 14. Memory vault

Lives at `<repo>/.johnstudio/memory/`. Obsidian-compatible: wiki links, YAML frontmatter, tags. Use any Obsidian-flavored editor to browse.

```
<repo>/.johnstudio/memory/
  project_brief.md         ← describe project
  current_state.md         ← what's in flight
  architecture.md          ← high-level design notes
  coding_standards.md      ← conventions
  database_schema.md       ← schema reference
  api_contracts.md         ← API shape
  decisions/               ← ADRs and significant choices
    2026-05-28-x.md
  runs/                    ← per-task retros (auto-written by team mode)
    task-0001.md
  agent_lessons/           ← per-role accumulated lessons
    backend-developer.md
    code-reviewer.md
  handoffs/                ← cross-task handoffs
  graph/                   ← knowledge graph entities + relations
```

Specialists read:
- `project_brief.md` + `current_state.md` + `architecture.md` (via `context_builder`).
- Their own `agent_lessons/<role>.md` (via `_memory_injection_for`).
- The 5 most recent `decisions/*.md`.

You should edit `project_brief.md` and `current_state.md` manually as the project evolves. Specialists' context improves immediately.

---

## 15. Standing rules

`seeds/standing_rules.yaml` augments every team plan deterministically.

### Default rules (ship with JohnStudio)

| Rule | Trigger | Added role |
|---|---|---|
| `tests-for-python` | Any `*.py` in plan outputs | `test-automator` |
| `docs-for-new-feature` | Task mentions "add" / "new endpoint" | `technical-writer` |
| `sec-on-sensitive-surfaces` | Task mentions auth/payment/SQL keywords | `security-auditor` |
| `a11y-on-frontend` | Any HTML/CSS/TSX/JSX output | `accessibility-auditor` |
| `deps-review-on-manifest-change` | Any `requirements.txt`/`package.json`/etc. | `dependency-reviewer` |
| `always-have-code-reviewer` | Unconditional | `code-reviewer` |

### Adding your own

```yaml
# seeds/standing_rules.yaml
rules:
  - id: my-rule
    trigger:
      mentions_in_task: ["graphql", "websocket"]
    add:
      role: architect
      brief: "Write an ADR for the new protocol choice."
      output: "ADR.md"
```

Triggers: `any_file_pattern: [glob]`, `mentions_in_task: [keyword]`, `always: true`. Match the role name exactly. The system refuses to add a role that's already in the plan (idempotent).

---

## 16. Budget + cost control

Every Claude turn reports `total_cost_usd`. JohnStudio sums those per run and per task in O(1).

### Per-task budget

```bash
johnstudio team run myrepo "<task>" --budget 1.00
```

The budget is a **hard cap**. When the rolling sum crosses it:
- `approve_plan_and_run` refuses with `reason: "budget_exceeded"`.
- The auto-revise loop refuses to spawn further revision rounds.
- The tests-as-signal loop refuses to spawn debugger + revision.

### Reading current spend

```bash
johnstudio team budget myrepo 1
# cost: $0.0234   budget: $1.00   ok

# or via API
curl -H "Authorization: Bearer $(cat ~/.johnstudio/server_token)" \
  http://127.0.0.1:8765/api/projects/1/team/1/budget
```

The UI shows budget in the team task page header.

### Note on Max plan

Under a Claude Max plan, `total_cost_usd` is **notional** — what the same usage would cost via the API. It doesn't bill your account; you're metered against your Max plan's rolling 5-hour and weekly request limits instead. JohnStudio still tracks the notional figure so you can compare task costs.

---

## 17. Safety model

### What protects you

1. **Worktree isolation.** Every editor specialist gets its own git worktree. Bad work stays on its branch until you merge.
2. **Human merge gate.** Nothing lands on `main` without you running `merge`.
3. **`Task` tool blocked at catalog load.** No specialist can recursively spawn LLM subagents (would create a third LLM-supervised layer the research says collapses).
4. **`allowed_tools` per role.** Read-only roles get `--allowed-tools Read,Grep,Glob` so they literally can't `Edit` or `Bash`.
5. **Plan validation.** TEAM_PLAN.md's role names are checked against the catalog; output paths can't collide; the `_resolve_artifact_path` traversal guard refuses absolute paths and `../` escapes.
6. **Loopback bearer-token auth.** Only processes with the token at `~/.johnstudio/server_token` can hit `/api/*`. Constant-time `hmac.compare_digest` check.

### What doesn't protect you

- **Workers have full host access.** `--dangerously-skip-permissions` on Claude (and equivalents on Codex/Gemini) means the worker can read your `~/.aws/credentials`, your `~/.ssh/*`, etc. The worktree is a git boundary, not an OS boundary. **Don't run JohnStudio on prompts you don't trust.**
- **Anthropic/OpenAI/Google CLIs make network calls.** Your code goes to the model providers via their CLIs. Local-first means orchestration is local — the model itself is not.
- **Shell injection.** JohnStudio replaced `shell=True` with `shlex.split` in collector/merger, but if you let the lead-planner write a `pcfg.test_command` that includes attacker-controlled text, you can re-introduce risk.

---

## 18. Common project patterns + prompts that work

The planner is competent but you'll get better results with concrete prompts.

### Adding a feature

Bad: "Add user accounts"

Better: "Add a /signup endpoint that accepts {email, password}, validates email format, bcrypt-hashes the password, persists to a `users` table (create the migration), and returns a JWT signed with the existing `JWT_SECRET` env var. Include pytest coverage of happy path + duplicate email + invalid email."

### Adding an API endpoint

Good: "Add a GET /api/stats endpoint to app.py that returns the current uptime in seconds, total requests since startup, and the timestamp of the most recent request. Update the README with the new endpoint. Keep it minimal."

### Bug fix from a stack trace

Good: "Fix the AttributeError on line 89 of `src/transformer.py`. Trace below: [paste]. Tests should cover the regression."

### Refactor

Good: "Refactor `src/handlers/orders.py` to extract the validation logic into a separate `_validate_order()` helper. Keep all observable behavior; tests must still pass."

### Performance

Good: "Profile `process_batch()` in `src/etl/loader.py` for the test fixture in `tests/fixtures/large_batch.json`. Identify the dominant hot path. Propose the smallest change that would cut runtime by ~50%."

### Documentation only

Good: "Update README.md to document the new authentication endpoints we shipped in task-0007. Add a 'Getting Started' section showing the curl sequence. Don't touch any code."

### Security audit only

Good: "Read the diff between origin/main and HEAD. Threat-model the new endpoints against OWASP top 10. Don't fix anything; write SECURITY_REVIEW.md with findings."

### Cross-cutting feature

Good: "Add WebSocket support to the chat app: backend handles `/ws/chat` with per-room rooms, frontend connects and renders incoming messages. Persist to existing SQLite. Add pytest for the WebSocket protocol round-trip. Document the new endpoint."

---

## 19. Troubleshooting

### "Backend not running" badge in the UI

The launchd agent crashed or never started. Check:

```bash
launchctl print "gui/$(id -u)/com.johnstudio.server" | grep -E "state|pid|last exit"
tail ~/.johnstudio/server.err.log
```

Restart:

```bash
launchctl kickstart -k "gui/$(id -u)/com.johnstudio.server"
```

### UI shows "● offline" on the graph

The Vite dev server's env was stale (token rotated between vite startup and now). Restart it:

```bash
pkill -f "node.*vite"
bash scripts/start_ui.sh
```

### "Team task stuck at running forever"

The background ticker should drive it. If it doesn't:

```bash
johnstudio team status myrepo 1     # confirm DONE.md files exist
johnstudio team advance myrepo 1    # force a tick
```

If DONE.md is missing in a worktree, the worker probably crashed. Check the worker's log:

```bash
ls myrepo/.johnstudio/tasks/task-0001/logs/
cat myrepo/.johnstudio/tasks/task-0001/logs/team_<role>_<i>.log
```

### "no space for new pane" from tmux

Stale tmux sessions ate the pane budget. Kill the server:

```bash
tmux kill-server
```

### Worker pasted to the wrong file / broke main

You shouldn't see this — workers run in worktrees. But if a merge introduced damage:

```bash
cd myrepo
git log --oneline -5
git revert <bad merge>
```

### Database locked

WAL mode + `busy_timeout=5000` should prevent this. If it happens:

```bash
# Restart the backend; WAL fully recovers from a clean restart.
launchctl kickstart -k "gui/$(id -u)/com.johnstudio.server"
```

### Live tree empty / "0 live nodes"

SSE connection failed. Check the browser console; common cause is a stale `VITE_JOHNSTUDIO_TOKEN`. Restart the UI.

### Budget exceeded but I want to continue

Either raise the budget or clear it:

```bash
sqlite3 ~/.johnstudio/johnstudio.db "UPDATE tasks SET budget_usd = NULL WHERE id = <task_id>"
```

---

## 20. Appendix A — repo file layout

```
johnstudio/                              ← Python package
  cli.py                                 ← typer CLI entry
  server.py                              ← FastAPI app
  api/                                   ← REST + SSE routes
    routes_team.py, routes_chain.py, routes_tasks.py,
    routes_stream.py, routes_transcripts.py, routes_workers.py,
    routes_skills.py, routes_memory.py, routes_projects.py,
    routes_system.py, _helpers.py
  spawner.py                             ← shared launch seam (all 3 modes)
  orchestrator.py                        ← parallel-mode runner
  chain.py                               ← chain-mode state machine
  team.py                                ← role catalog + plan parser
  team_orchestrator.py                   ← team-mode state machine
  workers/                               ← per-provider adapters
    base.py, claude.py, codex.py, gemini.py, terminal.py, stub.py
  worker_events.py                       ← stream-json parsing + tailer + cost rollup
  transcripts.py                         ← Claude Code transcript discovery
  context_builder.py                     ← per-worker context-pack builder
  skill_router.py, skill_registry.py     ← skill matching + loading
  skill_importer.py, skill_source.py     ← skill ingestion
  memory.py                              ← memory vault writers
  knowledge_graph.py                     ← entity/relation persistence
  safety.py                              ← protected-path + dangerous-cmd scans
  collector.py, reviewer.py, merger.py   ← parallel-mode collect/review/merge
  project.py                             ← project registration
  config.py, init.py, models.py          ← global config, init, pydantic models
  db.py                                  ← SQLite schema + WAL config
  tmux_controller.py, git_worktree.py    ← OS interactions
  utils.py
  auth.py                                ← loopback bearer token

seeds/                                   ← shipped defaults
  default_config.yaml                    ← global config
  standing_rules.yaml                    ← team-mode plan augmentation
  roles/
    claude_vp/                           ← 7 roles
    codex_vp/                            ← 7 roles
    gemini_vp/                           ← 6 roles
  seed_skills/                           ← 10 bundled skills

desktop/                                 ← React + Vite + TS UI
  src/
    api/client.ts                        ← REST client
    lib/stream.ts                        ← SSE client with Bearer header
    pages/
      HomePage.tsx, ProjectPage.tsx, TaskPage.tsx,
      ChainPage.tsx, TeamPage.tsx, GraphPage.tsx,
      SkillsPage.tsx, AgentsPage.tsx, SafetyPage.tsx, SettingsPage.tsx
    components/
      Shell.tsx, Markdown.tsx, ui.tsx

scripts/
  install_launch_agent.sh                ← create + load launchd plist
  start_backend.sh                       ← convenience for foreground server
  start_ui.sh                            ← Vite dev with nvm-node selection
  dev_all.sh                             ← start both, side by side via tmux
  demo_ui_flow.sh                        ← scripted end-to-end demo

docs/
  team-mode.md                           ← team-mode user guide
  USER_GUIDE.md                          ← this document
  architecture.md, safety.md, roadmap.md
  ui_backend_readiness.md
  rfc/0001-multi-vp-team.md              ← team-mode design spec

tests/                                   ← pytest suite
~/.johnstudio/                           ← user state (not in repo)
  config.yaml                            ← global config
  server_token                           ← bearer (0600)
  johnstudio.db                          ← SQLite (WAL)
  server.{out,err}.log                   ← launchd logs
  skill-registry/                        ← imported skills cache

<your_repo>/.johnstudio/                 ← per-project state
  project.yaml                           ← project config
  tasks/task-NNNN/                       ← per-task artifacts
    TASK.md, prompts/, logs/, results/, diffs/, test_results/,
    team_notes/, TEAM_PLAN.md, PLAN_CRITIQUE.md, MERGE_PLAN.md,
    TEAM_STATE.json
  worktrees/task-NNNN-team-<role>-<i>/   ← per-editor git worktree
  memory/                                ← Obsidian-compatible vault
```

---

## 21. Appendix B — REST API

All authed routes require `Authorization: Bearer <token>` where token is in `~/.johnstudio/server_token`. `/api/health` is public.

### Health + system

| Method | Path | Returns |
|---|---|---|
| GET | `/api/health` | `{ok, version, service}` |
| GET | `/api/doctor` | full env health |

### Projects

| Method | Path | Returns |
|---|---|---|
| GET | `/api/projects` | list of projects |
| POST | `/api/projects` `{name, repo_path}` | registers project |
| GET | `/api/projects/{id}` | project detail |
| GET | `/api/projects/{id}/memory` | memory file list |
| GET | `/api/projects/{id}/memory/files` | same as above |
| GET | `/api/projects/{id}/memory/file?path=...` | one note's content |
| GET | `/api/projects/{id}/memory/entities` | knowledge graph entities |
| GET | `/api/projects/{id}/memory/relationships` | knowledge graph edges |

### Parallel-mode tasks

| Method | Path | Returns |
|---|---|---|
| GET | `/api/projects/{id}/tasks` | list |
| POST | `/api/projects/{id}/tasks/run` `{task, stub_only?, max_agents?}` | launches N implementers |
| GET | `/api/projects/{id}/tasks/{n}` | per-worker status |
| POST | `/api/projects/{id}/tasks/{n}/collect` | gathers artifacts |
| POST | `/api/projects/{id}/tasks/{n}/review` | reviewer picks winner |
| POST | `/api/projects/{id}/tasks/{n}/merge` `{worker_name, confirm: true, dry_run?}` | merges branch |
| POST | `/api/projects/{id}/tasks/{n}/stop` | kills workers |
| POST | `/api/projects/{id}/tasks/{n}/cleanup` | prunes worktrees |
| POST | `/api/projects/{id}/tasks/{n}/resume` `{worker_name}` | re-sends prompt |
| GET | `/api/projects/{id}/tasks/{n}/context-packs` | per-worker prompts |
| GET | `/api/projects/{id}/tasks/{n}/results` | RESULT.md per worker |
| GET | `/api/projects/{id}/tasks/{n}/diffs` | diff per worker |
| GET | `/api/projects/{id}/tasks/{n}/logs` | log per worker |
| GET | `/api/projects/{id}/tasks/{n}/review` | reviewer's FINAL_REVIEW.md |
| GET | `/api/projects/{id}/tasks/{n}/merge-plan` | MERGE_PLAN.md |
| GET | `/api/projects/{id}/tasks/{n}/safety-report` | safety scan output |

### Chain mode

| Method | Path | Returns |
|---|---|---|
| POST | `/api/projects/{id}/chain/run` `{task, architect?, rfc_reviewer?, implementer?, reviewer?}` | starts at rfc_drafting |
| POST | `/api/projects/{id}/chain/{n}/advance` | state-machine tick |
| GET | `/api/projects/{id}/chain/{n}` | phases + current |
| GET | `/api/projects/{id}/chain/{n}/artifact?kind=rfc\|rfc_review\|result\|review_N` | artifact content |
| POST | `/api/projects/{id}/chain/{n}/approve-rfc` `{note?}` | human gate pass |
| POST | `/api/projects/{id}/chain/{n}/reject-rfc` `{reason?}` | rejects, terminates |
| POST | `/api/projects/{id}/chain/{n}/merge` `{confirm: true}` | final merge |
| POST | `/api/projects/{id}/chain/{n}/reject` `{reason?}` | at conflict gate |

### Team mode

| Method | Path | Returns |
|---|---|---|
| GET | `/api/projects/{id}/team/catalog` | full role catalog |
| POST | `/api/projects/{id}/team/run` `{task, budget_usd?}` | spawns lead-planner |
| GET | `/api/projects/{id}/team/{n}` | TeamState |
| GET | `/api/projects/{id}/team/{n}/plan` | raw TEAM_PLAN.md + parsed plan |
| POST | `/api/projects/{id}/team/{n}/plan-critic` | spawn the critic |
| GET | `/api/projects/{id}/team/{n}/plan-critique` | PLAN_CRITIQUE.md content |
| POST | `/api/projects/{id}/team/{n}/approve` | spawns all specialists |
| POST | `/api/projects/{id}/team/{n}/advance` | manual tick |
| GET | `/api/projects/{id}/team/{n}/budget` | cost posture |
| GET | `/api/projects/{id}/team/{n}/merge-plan` | MERGE_PLAN.md |

### Transcripts

| Method | Path | Returns |
|---|---|---|
| GET | `/api/projects/{id}/runs/{run_id}/transcript?limit&only_sidechain` | parsed Claude Code session JSONL |
| GET | `/api/projects/{id}/runs/{run_id}/transcript/list` | recent transcripts for this run's cwd |

### Skills

| Method | Path | Returns |
|---|---|---|
| GET | `/api/skills?enabled_only&category` | list |
| GET | `/api/skills/{id}` | detail |
| POST | `/api/skills/{id}/enable` / `disable` | toggle |
| POST | `/api/projects/{id}/skills/{skill_id}/pin` / `unpin` | per-project pin |
| POST | `/api/projects/{id}/skills/discover` `{task, agent_role?}` | preview routing |
| POST | `/api/skills/source` `{uri}` | register skill source |
| GET | `/api/skills/sources` | list |
| POST | `/api/skills/sources/scan` | re-scan all |

### Live event stream (SSE)

| Method | Path | Returns |
|---|---|---|
| GET | `/api/projects/{id}/stream` | SSE: `snapshot`, `task_state`, `phase_state`, `worker_event` events |

Auth via header OR `?token=` query param (browser EventSource fallback).

### Workers

| Method | Path | Returns |
|---|---|---|
| GET | `/api/workers` | registered worker configs |
| GET | `/api/workers/doctor` | per-worker availability |
| POST | `/api/workers/{name}/test` | smoke test |

---

## 22. Appendix C — configuration

### Global config (`~/.johnstudio/config.yaml`)

Initialized from `seeds/default_config.yaml`. Key sections:

```yaml
user:
  name: John

runtime:
  max_active_agents: 6
  max_agent_depth: 1
  default_timeout_minutes: 45
  require_human_merge: true
  allow_worker_spawn: false           # forbids agents spawning sub-agents
  default_stub_only: false

tools:
  tmux: { command: tmux }
  git:  { command: git }

workers:
  claude_backend:
    provider: claude
    command: claude
    role: backend_implementer
    can_edit: true
    worktree: true
    max_runtime_minutes: 45
  # ... others ...

safety:
  blocked_paths:
    - ".env"
    - ".env.*"
    - "**/*.pem"
    - "**/*.key"
    - "~/.ssh/**"
    - "~/.aws/**"
    - "~/.config/gcloud/**"
  dangerous_commands:
    - "rm -rf"
    - "sudo"
    - "git push --force"
    - "chmod -R 777"
  require_approval_commands:
    - "npm install"
    - "pip install"
    - "brew install"
    - "git push"

skill_registry:
  max_skills_per_agent: 6
  max_skill_tokens_per_agent: 8000
  max_single_skill_tokens: 2500
  use_distilled_skills: true
  imported_skills_default_enabled: false

memory:
  use_markdown_vault: true
  use_knowledge_graph: true
  auto_tag_after_collect: true
  auto_link_after_collect: true
```

### Per-project config (`<repo>/.johnstudio/project.yaml`)

```yaml
version: 1
name: myrepo
repo_path: ~/Desktop/myrepo
base_branch: main
test_commands:
  - pytest -q
  - npm test
stack:
  languages: [python, typescript]
  frameworks: [fastapi, react]
  package_managers: [pip, npm]
pinned_skills:
  - python-test-style
  - fastapi-routing
rules:
  require_tests_before_merge: true
  max_files_changed_per_worker: 40
  protected_paths:
    - "infra/"
    - "secrets/"
memory:
  graph_enabled: true
  obsidian_compatible: true
```

### Role frontmatter

```yaml
---
name: backend-developer
description: Server-side implementer.
vp: claude_vp
provider: claude
can_edit: true
model: claude-opus-4-7        # optional per-role override
tools: [Read, Edit, Write, Bash, Grep, Glob]
---

You are a **senior backend developer** on the Claude VP team.
...
```

### Standing rule

```yaml
- id: my-rule
  trigger:
    any_file_pattern: ["**/*.go"]
    mentions_in_task: ["go", "golang"]
    # always: true
  add:
    role: code-reviewer
    brief: "Review the Go diff for idiom issues."
    output: "GO_REVIEW.md"
```

---

*End of guide. JohnStudio is open source and local-first. Edit anything, fork everything, send PRs at your option.*
