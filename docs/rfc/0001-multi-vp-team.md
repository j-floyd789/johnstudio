# RFC 0001 — Multi-VP team orchestration

**Status:** implemented (Phase 1 + Phase 2 + autonomous loops + spawner seam, all shipped 2026-05-28)
**Date:** 2026-05-28
**Owner:** John Floyd
**Implements:** the next major shift in JohnStudio — from "5 parallel implementers" to a real AI dev team.

> **Status update (post-implementation).** All 15 items called out in the
> deep code review are shipped: the 20-role catalog is live, the planner
> writes TEAM_PLAN.md, the auto-spawned plan critic writes
> PLAN_CRITIQUE.md, the auto-revise loop reacts to `needs-changes`
> verdicts, the test-signal loop spawns debugger + revision on failing
> tests, the background ticker drives advance_team_task without human
> input, cross-VP review and MERGE_PLAN generation are wired, cost +
> budget tracking enforces a per-task ceiling, traversal-guarded plan
> paths, SQL-rowcount idempotency on every state transition, and the
> shared spawner seam now serves all three modes (parallel / chain /
> team). User guide: `docs/team-mode.md`.

---

## Background

JohnStudio today ships **7 worker configs** and caps active agents at **6**. The
implicit model is "spawn N implementers, each takes a stab at the same task,
deterministic reviewer picks a winner, human approves merge." That model is:

1. **Wrong-shape.** A real dev team isn't 5 implementers. It's a planner, a few
   implementers, several reviewers/researchers/writers/auditors, and a lot of
   non-coding work.
2. **Underpowered.** Every worker is interchangeable. We don't get an
   architect's perspective on architecture, a security auditor's perspective on
   security, a doc writer's perspective on docs.
3. **Underused.** Even on the user's Max plan, 5 workers leaves rate-limit
   budget on the table. The right ceiling is ~15–25 specialists per task.

The community has settled — across `affaan-m/ecc`, `VoltAgent/awesome-claude-code-subagents`,
`contains-studio/agents`, `wshobson/agents`, `ruvnet/claude-flow` — on a clear
shape: one **markdown file per role** with YAML frontmatter, dynamic dispatch
based on task → role matching, role-per-agent decomposition (not file-per-agent
or team-of-teams). We adopt that.

## Goals

1. **Three-VP structure** mapping the user's mental model: Claude = VP
   Engineering, Codex = VP Quality, Gemini = VP Research/Strategy. Each VP runs
   a team of specialists from its own provider.
2. **20–25 specialist role configs** with focused responsibilities, most of
   them read-only / no-commit. Implementers are a minority.
3. **Two-level orchestration only** (Planner → Specialists; no nested LLM
   supervisors). Grounded in the SOTA research — Anthropic's Multi-Agent
   Research System, MetaGPT, LangGraph nested supervisors, Magentic-One — all
   of which collapse past 2 levels of LLM supervision.
4. **Per-VP parallel teams.** VPs work concurrently; their teams work
   concurrently within each VP. No agent-to-agent debate.
5. **Deterministic consolidation.** Lead-synthesizes for non-code outputs;
   execution-filter for code outputs. No voting tournaments until candidates < 5.

## Non-goals

1. **Multi-Agent Debate.** Explicitly avoided — published evidence (arXiv
   2502.08788, 2503.12029, 2509.11035) shows MAD often *loses* to single-agent
   CoT + self-consistency.
2. **Blackboard architectures.** Demo-grade in code-gen; control unit
   degenerates into a supervisor anyway.
3. **3+ level LLM supervisor hierarchies.** AutoGen issues #3215 and #1400 are
   explicit: speaker-selection LLMs collapse past ~10 agents. Two levels max.
4. **CrewAI-style manager-worker.** Documented production failures (CrewAI
   #4783, TDS post-mortem). We borrow LangGraph's shape instead.
5. **Agents spawning sub-agents.** `allow_worker_spawn` stays `false`. Only
   the orchestrator spawns. This prevents context-pollution loops.

---

## Architecture

```
                              Task (from human)
                                     │
                                     ▼
                         ┌──────────────────────┐
                         │   Orchestrator       │  ← deterministic state machine
                         │   (no LLM)           │     (Python; no token cost)
                         └─────────┬────────────┘
                                   │
                       ┌───────────┴────────────┐
                       │                        │
                       ▼                        │
                ┌─────────────┐                 │
                │  PLANNING   │  ← Gemini       │
                │  Phase      │     (LeadResearcher  │
                │             │      pattern)        │
                │ writes      │                 │
                │ TEAM_PLAN.md│                 │
                └──────┬──────┘                 │
                       │                        │
                       │  human approves?       │
                       └────────────┬───────────┘
                                    │ approve
                                    ▼
            ┌───────────────────────────────────────────────┐
            │           EXECUTION Phase (parallel)          │
            ├───────────────────────────────────────────────┤
            │                                               │
            │   Claude VP             Codex VP              │
            │   (Engineering)         (Quality)             │
            │   ─────────────         ──────────            │
            │   architect *           test-author           │
            │   backend-dev           security-auditor *    │
            │   frontend-dev          perf-analyst *        │
            │   code-reviewer *       refactor-scout        │
            │                                               │
            │              Gemini VP                        │
            │              (Research)                       │
            │              ────────                         │
            │              researcher *                     │
            │              tech-writer                      │
            │              pm *                             │
            │              accessibility *                  │
            │                                               │
            │   ( * = read-only, can_edit=false )           │
            └─────────────────────┬─────────────────────────┘
                                  │
                                  ▼
                     ┌─────────────────────────┐
                     │   CROSS-VP REVIEW       │
                     │   (parallel)            │
                     │   Each VP's reviewer    │
                     │   reads other VPs'      │
                     │   outputs.              │
                     └────────────┬────────────┘
                                  │
                                  ▼
                     ┌─────────────────────────┐
                     │   CONSOLIDATION         │
                     │   (deterministic)       │
                     │   Orchestrator merges   │
                     │   artifacts, prepares   │
                     │   MERGE_PLAN.md         │
                     └────────────┬────────────┘
                                  │
                                  ▼
                            human → merge
```

**The shape, in one paragraph:** an LLM planner (Gemini, acting as VP Research)
decomposes the task into a `TEAM_PLAN.md` that names which specialists from
each VP's team will run, what each specialist's brief is, and where their
output goes. The orchestrator (Python, deterministic, no LLM) reads that plan,
spawns the named specialists in parallel — siblings within each VP's team, VPs
in parallel with each other — captures each specialist's structured artifact,
then runs a cross-VP review pass where each VP's designated reviewer reads
artifacts from the other two VPs. Finally the orchestrator consolidates
everything into a `MERGE_PLAN.md` and a unified diff for human approval.

There are exactly **two LLM-supervised layers**: the planner and the
specialists. No specialist is a "supervisor" of other specialists. This is the
ceiling the research is clear about.

---

## Role taxonomy (the catalog)

`seeds/roles/` will contain one markdown file per role, with YAML frontmatter
matching the community convention (`name`, `description`, `tools`, `model`,
plus our domain-specific fields). Each role declares which VP owns it.

### Claude VP (Engineering) — implementer-heavy

| Role | can_edit | Provider | Notes |
|---|---|---|---|
| `architect` | false | claude | writes ADR/RFC, no code |
| `backend-developer` | true | claude | server, APIs, data |
| `frontend-developer` | true | claude | UI, client |
| `fullstack-developer` | true | claude | small/cross-cutting work |
| `code-reviewer` | false | claude | reads diffs, writes reviews |
| `debugger` | false | claude | reads logs, writes hypotheses |
| `database-administrator` | true | claude | migrations, schema |

### Codex VP (Quality & Operations) — verification-heavy

| Role | can_edit | Provider | Notes |
|---|---|---|---|
| `test-automator` | true | codex | writes unit + integration tests |
| `security-auditor` | false | codex | OWASP scan, threat model |
| `performance-engineer` | false | codex | profile, propose, no impl |
| `refactoring-specialist` | true | codex | scoped cleanups |
| `devops-engineer` | true | codex | CI, Dockerfile, deploy |
| `accessibility-auditor` | false | codex | WCAG checks |
| `dependency-reviewer` | false | codex | reads requirements, flags risk |

### Gemini VP (Research & Strategy) — read-heavy

| Role | can_edit | Provider | Notes |
|---|---|---|---|
| `lead-planner` | false | gemini | **the planner** — writes TEAM_PLAN.md |
| `researcher` | false | gemini | finds prior art, writes RESEARCH.md |
| `product-manager` | false | gemini | scope, acceptance criteria |
| `technical-writer` | true | gemini | README, CHANGELOG, docs |
| `ux-researcher` | false | gemini | user-flow notes |
| `competitive-analyst` | false | gemini | "how do other tools do this" |

**Total:** 20 roles at launch. Implementer count: 8. Read-only/non-coding count: 12.

The role markdown body is the system prompt the worker receives. Following the
VoltAgent convention precisely so we can later import their catalog wholesale
if we want.

---

## TEAM_PLAN.md format

Output of the planning phase. Structured YAML inside fenced blocks so the
orchestrator can parse it deterministically:

````markdown
# Team plan for task-0007

## Summary
One paragraph describing the goal.

## Team
```yaml
claude_vp:
  - role: backend-developer
    brief: "Implement /api/rooms CRUD against the existing FastAPI app."
    output: "RESULT.md + commits on ai/task-0007/backend"
  - role: code-reviewer
    brief: "Review the backend developer's diff. Focus on input validation."
    output: "REVIEW_backend.md"

codex_vp:
  - role: test-automator
    brief: "Write pytest tests for /api/rooms covering happy path + 422s."
    output: "tests/test_rooms.py + RESULT.md"
  - role: security-auditor
    brief: "Threat-model the /api/rooms surface. Flag XSS/SQLi/auth."
    output: "SECURITY_REVIEW.md"

gemini_vp:
  - role: technical-writer
    brief: "Update README with the new endpoints."
    output: "README diff"
```

## Cross-team review
```yaml
- reviewer: code-reviewer (claude_vp)
  reads: [SECURITY_REVIEW.md, tests/test_rooms.py]
- reviewer: security-auditor (codex_vp)
  reads: [backend's RESULT.md]
```

## Acceptance criteria
- pytest passes
- README mentions new endpoints
- no Sev-1 findings in SECURITY_REVIEW.md
````

The orchestrator validates the YAML against a schema. Invalid plan → planner
gets one revise round → if still invalid → human gate.

---

## Coordination model

1. **Strategy phase (1 LLM call).** Lead planner reads task + project memory,
   writes `TEAM_PLAN.md`. Includes its own self-critique against acceptance
   criteria (Evaluator-Optimizer pattern from Anthropic's "Building Effective
   Agents").
2. **Human approval gate.** Same shape as the existing RFC approval gate. The
   user reads the plan, approves or sends back for revision.
3. **Execution phase (N parallel LLM calls).** Orchestrator spawns every named
   specialist. Each gets a context pack containing: their brief from the plan,
   the relevant skill markdown, project memory, and a fresh git worktree (if
   `can_edit: true`) or read-only access (if `false`).
4. **Cross-VP review phase (M parallel LLM calls).** Pure read-only round.
   Each VP's reviewer reads the *other* VPs' outputs. This is the structural
   defense against single-VP groupthink.
5. **Consolidation (deterministic).** Orchestrator:
   - Collects all RESULT.md files into one unified `EXECUTION_REPORT.md`.
   - Collects all REVIEW.md files into one `CROSS_REVIEW.md`.
   - Picks the implementer's diff via existing `reviewer.py` logic (still
     applies — but now over <5 candidates, exactly the regime where tournament
     comparison works).
   - Writes `MERGE_PLAN.md` listing files, expected conflicts, test commands.
6. **Human merge gate.** Existing flow.

---

## Reviewer at N=20

The current reviewer scores one winner across N implementer diffs. With this
architecture:

- **Implementer diffs:** still N=2–4 (number of implementers per VP × VPs that
  spawned an implementer). Stays in the regime where the existing reviewer
  works.
- **Non-code outputs (RESEARCH.md, RECOMMENDATIONS.md, SECURITY_REVIEW.md, etc.):**
  there's no winner to pick — they're complementary. The orchestrator just
  *concatenates* them into the EXECUTION_REPORT with section dividers.

So scaling the reviewer isn't actually required for this RFC. We get there for
free.

---

## What changes (code)

### Phase 1 — MVP (this PR)

- New `seeds/roles/<vp>/<role>.md` catalog with all 20 roles.
- New `johnstudio/team.py` module: parses `TEAM_PLAN.md`, validates against
  schema, exposes a `TeamPlan` dataclass.
- `johnstudio/chain.py` gains a new phase: `planning` (and renames
  `rfc_drafting` → `architect_rfc` since RFC writing is now a specialist role,
  not a phase).
- `johnstudio/orchestrator.py`: new `run_team_phase` that reads the plan and
  spawns N specialists in parallel under their VPs.
- `johnstudio/api/routes_team.py`: thin wrappers for plan approval/edit.
- DB: new `team_plans` and `team_assignments` tables; idempotent migration.

### Phase 2 — UX polish (follow-up PR)

- GraphPage gets a "team view" mode that groups nodes by VP, with implicit
  VP-level lanes.
- Plan editor in the UI — markdown editor with live YAML validation.
- Role catalog browser at `/agents` (existing page gets reworked).

### Phase 3 — Skill packs per role (follow-up PR)

- Each role markdown body becomes the system prompt. The existing skill
  registry already supports per-role skill selection (`agent_roles` field in
  skill frontmatter). Auto-attach matching skills at context-pack build.

### Out of scope for now

- Tournament reviewer (not needed; candidate counts stay small).
- Agents spawning sub-agents (stays disabled).
- Multi-provider VPs (a VP runs only its provider's workers).

---

## Migration

1. Existing parallel mode (`/api/.../tasks/run`) stays — it's still useful for
   simple one-shot tasks. Becomes the "fast lane."
2. Existing chain mode stays — useful for sequential RFC → impl → merge.
3. New "team mode" is the recommended path for non-trivial work.
4. UI surfaces three buttons: "Run task" (parallel), "Chain mode", "Team mode".

No existing config breaks. All three modes coexist.

---

## Risks

| Risk | Mitigation |
|---|---|
| Planner writes a bad plan; specialists do unfocused work | Human gate after planning, before any specialist spawns. Same shape as RFC gate. |
| Specialists with overlapping briefs produce conflicting outputs | Briefs explicitly list output paths; orchestrator validates no two specialists write to the same path before spawning. |
| 20 concurrent Claude/Codex/Gemini processes exceed Max plan rate limit | Inter-launch stagger (already in place); plan can also declare `parallel_cap: 6` to keep batches reasonable. |
| Plan parsing too brittle | YAML inside markdown is the format; one revise round for the planner if validation fails. |
| Cross-VP review takes too long | Each cross-review brief is small (read 2 files, write 1). Parallel. Should add <90s. |
| Existing tests break | Phase 1 doesn't touch `orchestrator.run` (parallel mode) or the chain phases; only adds new code paths. Test surface is additive. |

---

## Open questions

1. **Should the planner run as a single agent or as a small RFC team (architect + PM + researcher → consolidated plan)?** Leaning single agent for v1; revisit if plans are weak.
2. **Should each VP have a "VP-level reviewer" that reads its team's outputs before cross-VP review?** Adds another LLM call per VP. Not in v1; add if outputs feel scattershot.
3. **Skill-pack scope.** Auto-attach by role tag or have the planner explicitly list skills per specialist? Latter is more deterministic; former is less work for the planner. Going with auto-attach for v1.
4. **How does the user EDIT the plan?** Markdown editor in the UI vs CLI edit + re-validate. Plan editor in UI is in Phase 2; for Phase 1 just open the file in `$EDITOR`.

---

## What success looks like

A user types `johnstudio team run coolsite "Build a real-time multiplayer
whiteboard"` and:

1. Within ~60s, sees a `TEAM_PLAN.md` listing 12–15 specialists with focused
   briefs.
2. Approves the plan (one click in the UI, or `johnstudio team approve`).
3. Watches the live graph: 12+ specialist nodes streaming events for ~10
   minutes. Architects writing ADRs, security auditors writing threat models,
   technical writers updating the README — all in parallel.
4. Gets a unified `EXECUTION_REPORT.md` with a section per specialist, a
   `MERGE_PLAN.md` describing the proposed diff, and a "merge?" gate.
5. Merges. Repo has the feature, tests, docs, security notes, and architecture
   notes — written by the right specialist in each case.

That's the product.
