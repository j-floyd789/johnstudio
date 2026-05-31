# JohnStudio Phase 0 — Repository Research Report

Generated: 2026-05-27
Mode: live WebFetch of GitHub repositories at research time. This file is the **baked-in copy** used by `johnstudio research` so the command works offline.

The report grounds JohnStudio's importer/router in the actual file formats used by widely-adopted Claude-skills/agents repos, rather than assumed schemas.

---

## A. Repo table

| Repo | Purpose | Useful formats found | Useful modules to import | Risks / quality concerns | Priority (1–10) |
|---|---|---|---|---|---|
| `affaan-m/ECC` | Cross-harness operator system (Claude/Cursor/Codex/OpenCode/Zed). 246 skills, 61 agents, 76 commands, hooks runtime, MCP configs, multi-harness adapters, alpha Rust control-plane (ECC2). | `skills/<name>/SKILL.md` with YAML frontmatter (`name`, `description`, `tags`, `confidence`, `agent`, `tools`, `model`); `agents/*.md`; `rules/<lang>/*.md` with `description`/`globs`/`alwaysApply`; `hooks/hooks.json`; `mcp-configs/mcp-servers.json`; `.claude/`, `.codex/`, `.cursor/`, `.gemini/`, `.opencode/`, `.zed/` adapters; `install.sh`/`install.ps1` with `--profile`/`--target`/`--modules`/`--with capability:` flags; `manifests/` plugin/marketplace JSON. | Skill categories (tdd-workflow, security-review, verification-loop, deployment-patterns, continuous-learning-v2). Agent set (planner, architect, code-reviewer, security-reviewer, build-error-resolver, language reviewers). ECC2 control-plane concepts: sessions / delegation / worktree status / merge queue / decision log / messages — JohnStudio's own ops model maps almost 1:1. | Single-maintainer cadence, 1,994 commits, heavy/opinionated. We must default imports to `enabled: false` + `trust_level: unreviewed` until reviewed. Continuous-learning v2 introduces dynamic "instinct" data we do not want to import wholesale. | 10 |
| `alirezarezvani/claude-skills` | 338 production skills across 16 domains, multi-tool conversion (Cursor/Aider/Kilo/Windsurf/OpenCode/Augment/Antigravity/Hermes/Vibe), agent personas, orchestration patterns. | `SKILL.md` with `name`/`description`/`domain`/`tier` frontmatter; `agents/personas/*.md` with `TEMPLATE.md`; `scripts/install.sh`, `scripts/convert.sh`, `scripts/gemini-install.sh`, `scripts/codex-install.sh`; `orchestration/ORCHESTRATION.md` with four patterns (Solo Sprint, Domain Deep-Dive, Multi-Agent Handoff, Skill Chain). | Persona format with curated skill loadouts (Startup CTO, Growth Marketer, Solo Founder). Skill domain taxonomy (engineering-core, engineering-powerful, product, marketing, research, regulatory, compliance, c-level, finance, ops). Convert-to-Cursor `.mdc` script. | Some skills are domain-heavy (regulatory, finance) and won't help typical app projects — must be optional/tagged so the router can ignore them on coding tasks. Some scripts assume Python stdlib but may write to paths we don't want them touching. | 9 |
| `VoltAgent/awesome-claude-code-subagents` | 100+ subagents in 10 numbered categories (`categories/01-core-development/`, `02-language-specialists/`, etc.). | Per-agent markdown with frontmatter `name`/`description`/`tools`/`model`; `tools` is comma-separated list (`Read, Write, Edit, Bash, Glob, Grep`); `model` is `opus`/`sonnet`/`haiku`/`inherit`. Plugin installer (`claude plugin marketplace add VoltAgent/awesome-claude-code-subagents`) and manual install via `~/.claude/agents/`. | Best source for **flat per-role agents** that map cleanly to JohnStudio worker roles: `api-designer`, `backend-developer`, `frontend-developer`, `fullstack-developer`, `react-specialist`, `nextjs-developer`, `typescript-pro`, `python-pro`, `golang-pro`, `rust-engineer`, `test-automator`, `code-reviewer`, `security-auditor`, `qa-expert`, `penetration-tester`, `accessibility-tester`, `devops-engineer`, `kubernetes-specialist`, `docker-expert`, `terraform-engineer`, `cloud-architect`, `database-administrator`, `postgres-pro`, `data-engineer`, `data-scientist`, `ml-engineer`, `prompt-engineer`. | Frontmatter `tools` is a free-form string list — must normalize via splitter/whitelist. `model` claims are advisory only; JohnStudio ignores them (we don't call APIs in MVP). | 10 |
| `rohitg00/awesome-claude-code-toolkit` | Index of tool projects: memory, worktrees, context reduction, hooks, security, session management. | Each entry is a project pointer with one-line description. Use as a **watchlist**, not an import source. | Identifies tools we may wrap or import later (claude-mem, claude-context, claude-recap, claude-code-sessions, ccpm, vibe-kanban, pro-workflow, claude-code-hooks, agento-patronum, VibeGuard, Bouncer, ccmanager, claude-scaffold). | No standardized format across linked repos. Quality varies wildly; do not treat as authoritative. | 6 |

---

## B. File format inventory

### `SKILL.md` (ECC style — primary)

```yaml
---
name: tdd-workflow
description: TDD methodology with red-green-refactor and verification loops
tags: [testing, tdd, quality]
confidence: high
agent: test-automator        # optional
tools: ["Read", "Edit", "Bash"]   # optional
model: opus                  # advisory only — JohnStudio ignores
---

# TDD Workflow
...
```

### `SKILL.md` (alirezarezvani style)

```yaml
---
name: Skill Name
description: One-line purpose
domain: engineering | product | research | marketing | compliance | finance
tier: basic | advanced | powerful
---

# Skill Title
## Overview
## When to Use
## Key Steps / Workflows
## Tools & References
```

### `agents/*.md` (VoltAgent style — flat)

```yaml
---
name: react-specialist
description: When this agent should be invoked
tools: Read, Write, Edit, Bash, Glob, Grep
model: sonnet
---

# React Specialist
...
```

### `agents/*.md` (ECC style)

```yaml
---
name: code-reviewer
description: ...
agent: code-reviewer
tools: ["Read", "Grep", "Glob"]
---

# Code Reviewer
...
```

### `agents/personas/*.md` (alirezarezvani style)

```yaml
---
name: Startup CTO
description: ...
skills:
  - architecture
  - aws-solution-architect
  - senior-frontend
workflow: ...
---
```

### `CLAUDE.md` (root)
Project- or user-level long-form guidance, plain markdown with optional headings. No formal frontmatter required, but headings like `## When to Use`, `## Coding Style`, `## Workflow` are common.

### `AGENTS.md`
Catalog of available agents, typically a markdown table or bulleted list with `name` and `description`. Used by ECC and Codex.

### `GEMINI.md`
Gemini-CLI variant of `CLAUDE.md` — same content shape, often produced by conversion.

### `.mdc` (Cursor rules)
Markdown with frontmatter:

```yaml
---
description: ...
globs: ["**/*.tsx", "components/**"]
alwaysApply: false
---
```

### `commands/*.md` (slash commands)
Free-form markdown, typically begins with `# /command-name` and contains the prompt body, arguments, and example invocations.

### `hooks/hooks.json`
Centralized hook configuration with event matchers:

```json
{
  "PreToolUse": [
    { "matcher": "tool == \"Edit\" && tool_input.file_path matches \".env\"",
      "command": "scripts/hooks/block-env-edits.js" }
  ],
  "PostToolUse": [...],
  "SessionStart": [...],
  "Stop": [...]
}
```

### `mcp-configs/mcp-servers.json`
MCP server registry — name, command, args, env vars, transport. JohnStudio does not require MCP for MVP but should be able to read these for future plugin support.

### Plugin manifest (`.claude-plugin/plugin.json`)

```json
{
  "name": "voltagent-core-dev",
  "version": "...",
  "agents": ["categories/01-core-development/*.md"],
  "skills": [...],
  "commands": [...]
}
```

### Install scripts
- ECC: `install.sh --profile {full,core,minimal} --target {claude,cursor,codex,...} --modules hooks-runtime --with capability:machine-learning`
- alirezarezvani: `scripts/install.sh --tool {cursor,aider,kilo,windsurf,opencode,augment,antigravity}`; `scripts/convert.sh` for format conversion.

---

## C. Import strategy

For every source artifact, JohnStudio writes a normalized directory under `~/.johnstudio/skill-registry/skills/<skill_id>/`:

```
original.md         # exact bytes of the source file (immutable; never overwritten on re-import)
distilled.md        # deterministic distillation: headings, must/should/never bullets,
                    # checklists, code blocks, glob patterns, named sections (When to Use,
                    # Activation, Checklist, Anti-patterns, Examples, Rules, Workflow).
                    # Drops badges, sponsor blocks, marketing copy, install boilerplate.
summary.md          # first 200–400 useful words, lead with purpose + activation
metadata.yaml       # normalized schema (see below)
source.json         # {source_repo, source_path, source_sha (if known), imported_at}
score.json          # routing prior: {priority, last_useful_at, last_marked_useful_by, count}
```

`metadata.yaml` is the **single normalized schema** the router scores against. It is produced from whichever upstream frontmatter was present, plus deterministic enrichment from headings/filename/folder.

```yaml
id: react-specialist                      # kebab-case, unique
name: React Specialist
type: agent | skill | rule | command | hook | mcp
source_repo: VoltAgent/awesome-claude-code-subagents
source_path: categories/02-language-specialists/react-specialist.md
category: frontend                        # mapped from folder + tags
description: React 18+ modern patterns expert
tags: [react, frontend, components, hooks]
languages: [typescript, javascript]
frameworks: [react, nextjs]
agent_roles: [frontend_implementer, ui_reviewer]
file_patterns: ["**/*.tsx", "**/*.jsx", "components/**", "app/**"]
dependencies: [react, next]
priority: medium
max_context_tokens: 2500
trust_level: unreviewed                   # imported repos default to unreviewed
enabled: false                            # imported skills default to disabled
created_at: ...
updated_at: ...
```

### Per-upstream conversion rules

**VoltAgent `categories/NN-name/<skill>.md`** → `type: agent`, `category` from folder name (strip `01-`/`02-` prefix), `tags` from filename keywords + body headings, `tools` parsed as comma-split into JSON list, `model` discarded.

**ECC `skills/<name>/SKILL.md`** → `type: skill`, `category` from frontmatter `tags[0]` if present else parent folder, copy `confidence`/`tools` into metadata, ignore `model`.

**ECC `agents/<name>.md`** → `type: agent`, infer role from filename suffix (`-reviewer` → reviewer roles, `-resolver` → fix/repair roles).

**ECC `rules/<lang>/<name>.md`** → `type: rule`, `languages: [lang]`, `file_patterns` from frontmatter `globs`.

**alirezarezvani `<domain>/<skill>/SKILL.md`** → `type: skill`, `category` from `domain` field, tier translates to priority (`basic`→low, `advanced`→medium, `powerful`→high).

**alirezarezvani `agents/personas/<name>.md`** → `type: agent` with persona marker in metadata; persona's listed `skills` become `requires:` pointers to other registry entries.

**`commands/*.md`** → `type: command`, command name from filename, body kept verbatim in `distilled.md`.

**`hooks/*.{md,json}`** → `type: hook`. JohnStudio MVP imports but does not execute external hooks.

**`*.mdc` (Cursor)** → `type: rule`, `file_patterns` from `globs`, `description` copied.

**`CLAUDE.md` / `AGENTS.md` / `GEMINI.md`** → `type: rule` with `category: general-guidance`. Imported only when explicitly pointed at via `johnstudio skill source add` because root-level files tend to be generic.

### Trust & enablement defaults

| Source | trust_level default | enabled default |
|---|---|---|
| `seeds/seed_skills/` (this repo) | `local-curated` | `true` |
| Any external repo (ECC, VoltAgent, alirezarezvani, etc.) | `unreviewed` | `false` |

User must explicitly `johnstudio skill review <id>` (later, manually edits trust_level) or `johnstudio skill enable <id>` or `johnstudio skill pin <project> <id>` to activate an imported skill.

---

## D. Skill categories

Final taxonomy used by importer/router:

- `research` — search-first, market-research, product-research, ux-research, first-principles-thinking
- `frontend` — react, nextjs, vue, accessibility, ui-design, landing
- `backend` — api-design, fastapi, django, node, graphql, microservices
- `database` — postgres, sql, schema-design, migrations, query-tuning
- `testing` — unit, integration, e2e, tdd, qa, playwright, debugger, error-detective
- `debugging` — bug-detective, error-detective, debugger
- `security` — security-auditor, penetration-tester, secret-scanner, compliance, gdpr, hipaa, dangerous-command-blocker
- `devops` — docker, kubernetes, terraform, ci, deployment, sre
- `ui-ux` — ui-designer, ux-researcher, accessibility-tester
- `documentation` — technical-writer, api-docs
- `agent-orchestration` — multi-agent-coordinator, task-distributor, workflow-orchestrator, agent-organizer, handoff
- `memory-context` — context-manager, knowledge-synthesizer, context-compression
- `product-business` — product-manager, startup-cto, solo-founder
- `compliance-privacy` — gdpr-ccpa, hipaa, soc2, secret-handling
- `general-guidance` — root CLAUDE.md / AGENTS.md fallback

---

## E. Recommended initial seed skills (30–50)

JohnStudio ships these as **local-curated** seeds (enabled by default). They are short, role-shaped markdown files written by us, mirroring the conventions found above. External repos can be imported on top.

**Frontend (6)** — frontend-developer, react-specialist, nextjs-developer, ui-designer, accessibility-tester, landing-builder
**Backend (8)** — backend-developer, api-designer, fullstack-developer, node-specialist, fastapi-developer, django-developer, graphql-architect, microservices-architect
**Database (4)** — postgres-pro, database-optimizer, sql-pro, database-administrator
**Testing/debugging (8)** — test-automator, qa-expert, debugger, error-detective, bug-detective, playwright-pro, code-reviewer, architect-reviewer
**Security/compliance (7)** — security-auditor, security-engineer, penetration-tester, compliance-auditor, gdpr-ccpa-compliance, hipaa-compliance, secret-handling
**Infra/devops (8)** — docker-expert, deployment-engineer, devops-engineer, sre-engineer, cloud-architect, terraform-engineer, kubernetes-specialist, ci-debugger
**Research/product (8)** — research-analyst, search-specialist, first-principles-thinking, market-research, product-research, ux-researcher, product-manager, startup-cto
**Memory/orchestration (7)** — context-manager, knowledge-synthesizer, multi-agent-coordinator, task-distributor, workflow-orchestrator, agent-organizer, handoff

Total: **56 recommendations**. MVP ships ~10 as actual seed files (see `seeds/seed_skills/`); the rest are imported on demand from the upstream repos via `johnstudio skill source add` / `johnstudio skill import`.

### Tier-1 MVP seed shortlist (10) — actually shipped in `seeds/seed_skills/`

1. `frontend-react-specialist` — React 18+/Next.js patterns; tags react/frontend.
2. `backend-api-designer` — REST/OpenAPI patterns; tags backend/api.
3. `test-automator` — unit/integration/e2e structure; tags testing.
4. `security-auditor` — secret scanning, dangerous-command list; tags security.
5. `debugger` — root-cause, repro-first; tags debugging.
6. `context-manager` — token budgeting, distillation; tags memory.
7. `knowledge-synthesizer` — handoff capsule, decision log; tags memory.
8. `product-manager` — scope-bounded task framing; tags product.
9. `startup-cto` — pragmatic-first, ship-the-thing; tags product.
10. `terminal-stub` — instructions for the test/stub worker.

---

## F. Watchlist (future imports from `awesome-claude-code-toolkit`)

| Tool | One-liner | Why JohnStudio cares |
|---|---|---|
| ccpm | GitHub Issues + worktrees for parallel agents | Validates JohnStudio's worktree + per-agent model |
| vibe-kanban | Kanban board for 10+ agents with isolated worktrees + inline diff | UI inspiration for desktop UI phase |
| claude-mem | Cross-session memory with SQLite + FTS | Validates our memory layer; reference implementation |
| claude-context | MCP semantic code search, ~40% token reduction | Future skill `code-search` |
| claude-code-hooks | 15 production hooks (destructive command blocker, branch guard) | Direct safety rule import |
| agento-patronum | Hook-based protection for .env/SSH/AWS | Mirrors our `safety.blocked_paths` |
| VibeGuard | Anti-hallucination rules + hooks across 5 languages | Future reviewer skill |
| Bouncer | Independent quality gate via Gemini stop hook | Cross-model reviewer pattern |
| ccmanager | Multi-agent session manager (Claude, Gemini CLI, etc.) | Validates our tmux multi-CLI model |
| claude-scaffold | npx CLI to deploy CLAUDE.md + hooks + 18 skills | Reference for our `init` UX |
| pro-workflow | Self-correcting memory, parallel worktrees, 8 hook types, 5 agents | Validates entire JohnStudio design |
| claude-code-sessions | 11 skills for full-text search/token analytics/task management | Reference for `johnstudio skill list/search` UX |

---

## G. Concrete implementation implications for JohnStudio

1. **Importer must handle three frontmatter shapes** simultaneously (ECC, alirezarezvani, VoltAgent) plus none-at-all. Parser must be tolerant of YAML quoting quirks (lists as comma-strings, lists as JSON arrays, lists as YAML sequences).

2. **`category` is derived, never trusted from upstream alone.** Source folder + frontmatter `domain`/`tags` + filename keywords vote. JohnStudio's category taxonomy is canonical.

3. **`tools` field is informational only.** JohnStudio's permission model is governed by `workers.<name>.can_edit` and the per-task scope, not by the skill's claimed tool list.

4. **`model` field is discarded.** MVP does not call APIs.

5. **Trust defaults to `unreviewed` + `enabled: false` for all imported repos.** A user typing `johnstudio skill source add https://github.com/...` does not get random instructions injected into agent prompts. This is a critical safety property.

6. **Hooks are imported as records but not executed.** Future work can wire `hooks/hooks.json` parsing into the safety layer, but MVP does not run third-party hook scripts.

7. **The `terminal_stub` worker is first-class** — it lets the entire pipeline (init → add-project → import → route → context-pack → worktree → tmux → collect → review → merge) run offline with zero model usage. Every CLI command should be tested against the stub before any real CLI is wired up.

8. **Per-task context packs replace the giant CLAUDE.md pattern.** ECC ships a CLAUDE.md but explicitly markets "skills as the primary workflow surface" — confirming the direction.

9. **ECC2 Rust control-plane** provides the operational vocabulary JohnStudio reuses: session, delegation, worktree status, merge queue, decision log, messages. JohnStudio's SQLite schema (`tasks`, `runs`, `messages`, `decisions`, `diffs`, `reviews`) mirrors this directly.

10. **Persona = bundle of skills.** alirezarezvani's persona format (`skills: [list]`) maps to JohnStudio's "default agent team selection" — `lead_planner` + `claude_backend` + `claude_frontend` + `codex_tests` + `gemini_review` is itself a persona.
