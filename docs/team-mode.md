# Team mode

Team mode is JohnStudio's third execution shape, alongside **parallel**
(N implementers race; deterministic reviewer picks a winner) and
**chain** (RFC → impl → review → merge sequence). Use team mode for
work where the right team isn't a fixed list — let the planner decide.

```
            ┌─────────────────────────┐
            │   You (one prompt)      │
            └────────────┬────────────┘
                         ▼
            ┌─────────────────────────┐
            │ Gemini lead-planner     │  writes TEAM_PLAN.md
            └────────────┬────────────┘
                         ▼
            ┌─────────────────────────┐
            │ Auto plan-critic        │  product-manager scores plan
            │ (Sonnet)                │  → PLAN_CRITIQUE.md
            └────────────┬────────────┘
                         ▼  human approves plan
            ┌─────────────────────────┐
            │ N specialists in        │  spawn_and_track, in parallel
            │ parallel under their VPs│  one worktree per editor
            └────────────┬────────────┘
                         ▼
            ┌─────────────────────────┐
            │ Project tests run       │  inside every worktree
            │ inside every worktree   │  (pcfg.test_commands)
            └────────────┬────────────┘
              ┌──────────┴──────────┐
        tests pass            tests fail
              │                     │
              ▼                     ▼
   ┌──────────────────┐  ┌──────────────────────┐
   │ Cross-VP review  │  │ debugger writes      │
   │ (each VP reads   │  │ DEBUG_REPORT.md      │
   │ another VP's     │  │ +                    │
   │ output)          │  │ revision pass        │
   └────────┬─────────┘  │ (≤ MAX_REVISE_ROUNDS)│
            ▼            └──────────┬───────────┘
   ┌──────────────────┐             │
   │ MERGE_PLAN.md    │◀────────────┘
   │ generated        │
   └────────┬─────────┘
            ▼  human approves merge
   ┌──────────────────┐
   │ Branches merged  │
   │ + per-role       │
   │ lessons appended │
   │ to memory vault  │
   └──────────────────┘
```

## How to drive it

### Via the UI
1. Home page → check **Team (planner → specialists)**.
2. Type one prompt.
3. Optionally set a budget (`budget_usd`) on the request payload (not yet in the radio UI but the API accepts it).
4. Hit **Run**. You land on `/p/:id/team/:n`.
5. ~60 seconds later, `TEAM_PLAN.md` appears, formatted by VP. The
   plan-critic runs automatically and its `PLAN_CRITIQUE.md` shows
   below.
6. Hit **Approve & spawn N specialists**.
7. Open `/p/:id/graph` to watch the live tree. Specialist nodes are
   tinted by VP (Claude=blue, Codex=amber, Gemini=violet). Subagents
   spawn as purple-dashed children.
8. When all specialists finish, the background ticker advances the task
   through tests-as-signal → cross-VP review → MERGE_PLAN.md.
9. Approve the merge from the task page.

### Via the API
```bash
TOKEN=$(cat ~/.johnstudio/server_token)
curl -X POST -H "Authorization: Bearer $TOKEN" -H "content-type: application/json" \
  -d '{"task":"Add a /api/health endpoint","budget_usd":1.0}' \
  http://127.0.0.1:8765/api/projects/1/team/run
# wait ~60s
curl -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8765/api/projects/1/team/7/plan
# review, then approve:
curl -X POST -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8765/api/projects/1/team/7/approve
```

### Via the CLI
```bash
johnstudio team run demo "Add a /api/health endpoint"
# (CLI commands forthcoming — for now use curl as above)
```

## What's running where

| File | Role |
|---|---|
| `seeds/roles/<vp>/<role>.md` | 20 role definitions, YAML frontmatter + system prompt body |
| `johnstudio/team.py` | Role catalog loader; TEAM_PLAN parser |
| `johnstudio/team_orchestrator.py` | State machine, plan critic, auto-revise loop, tests-as-signal, cross-VP review spawner, MERGE_PLAN generator |
| `johnstudio/spawner.py` | Single launch seam used by all three modes |
| `johnstudio/worker_events.py` | Per-provider stream-json parsers; cost roll-up |
| `johnstudio/api/routes_team.py` | `/api/projects/:id/team/...` endpoints |
| `desktop/src/pages/TeamPage.tsx` | UI: plan display, auto-critique, budget readout, approve button |
| `desktop/src/pages/GraphPage.tsx` | Live tree with VP coloring + subagent child nodes |

## Operational notes

- **The backend writes `~/.johnstudio/server_token`** on startup. The
  UI's `bash scripts/start_ui.sh` reads it and exports it as
  `VITE_JOHNSTUDIO_TOKEN`. If the UI shows "offline", restart it.
- **Background ticker** runs every 5 seconds, walking
  `tasks WHERE status IN ('planning','running','reviewing')` and
  calling `advance_team_task`. No human polling required.
- **Startup recovery** scans `runs WHERE status IN ('launched','running')`
  and re-attaches event tailers for live PIDs, marks dead PIDs as
  `stopped`. Closes the "ghost spinner forever after backend crash"
  case.
- **Cost tracking**: Claude's `total_cost_usd` accumulates into
  `runs.cost_usd` and `tasks.cost_usd` per turn. `tasks.budget_usd`
  is a hard cap — when crossed, `approve_plan_and_run` and the
  auto-revise loops refuse to spawn further workers.

## Tuning levers

- `seeds/roles/<vp>/<role>.md` — change the `model:` field to swap
  models (e.g. `claude-haiku-4-5` for trivia roles); change the
  `tools:` list to widen or narrow what the CLI can call. **`Task` is
  blocked at catalog-load time** so specialists can't recursively
  spawn LLM subagents.
- `johnstudio/team_orchestrator.py:MAX_REVISE_ROUNDS` — currently 2.
  Higher values let the system iterate more before escalating to the
  human; doubles cost in the worst case.
- `johnstudio/team_orchestrator.py:start_ticker(interval_seconds=…)` —
  default 5s. Lower for tighter latency; higher for fewer DB writes.

## Known limits

- The plan-critic and auto-revise loops are scoped to **Anthropic
  Evaluator-Optimizer at two LLM-supervised layers** (planner + critic;
  implementer + reviewer). The research is clear that going past 2
  collapses on context bloat. RFC 0001 §Non-goals explicitly forbids
  that and `team.load_role_catalog()` blocks the `Task` tool to enforce
  it.
- N=20 specialists is the documented ceiling. Beyond ~15, git worktree
  index-lock contention starts dominating spawn time; the inter-launch
  stagger (0.5s) helps but doesn't eliminate it.
- The `model:` field is honored by Claude; Codex and Gemini accept it
  via their own flags but the adapters don't plumb it through yet (a
  follow-up commit).
