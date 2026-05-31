# JohnStudio Architecture (MVP)

## Goals
- Local-first: no model APIs in MVP. Use already-authenticated local CLIs.
- Bounded multi-agent execution: hundreds of available skill/agent definitions, a small active team per task, strict max-agents and depth limits, human-in-the-loop merge.
- Per-agent context packs, not one giant CLAUDE.md.
- Obsidian-compatible knowledge-graph memory.

## Subsystems

```
johnstudio/
  cli.py              # Typer entrypoint — thin wrappers only
  config.py           # JOHNSTUDIO_HOME, global+project config I/O
  db.py               # SQLite (stdlib), idempotent schema, FTS5/LIKE fallback
  models.py           # Pydantic contracts
  utils.py            # frontmatter, slug, subprocess, token approx
  project.py          # add-project, stack detection, list/get
  memory.py           # Markdown vault init + writes
  knowledge_graph.py  # Obsidian-compatible entities + relationships
  skill_importer.py   # parse, distill, summarize, normalize from upstream layouts
  skill_registry.py   # CRUD, enable/disable, trust, pin
  skill_router.py     # deterministic scoring, token budget caps
  skill_source.py     # source registry + scan
  context_builder.py  # per-agent prompts with rule precedence + output contract
  git_worktree.py     # transparent git CLI wrappers
  tmux_controller.py  # tmux + subprocess fallback
  workers/
    base.py           # BaseWorker, LaunchHandle
    stub.py           # terminal_stub — always available, offline
    terminal.py       # generic shell-cmd worker
    claude.py codex.py gemini.py   # CLI adapters
  orchestrator.py     # run / status / resume / stop / cleanup
  collector.py        # RESULT.md, tmux capture, diff, tests, safety
  reviewer.py         # deterministic scoring, FINAL_REVIEW.md, MERGE_PLAN.md
  merger.py           # gated merge, decision log, graph updates
  safety.py           # pure scans
  init.py             # init + research commands
```

## State layout

Global: `~/.johnstudio/` (overridable via `JOHNSTUDIO_HOME` for tests).
Per-project: `<repo>/.johnstudio/` (config + memory + tasks + worktrees + logs).

## Data flow for one task

1. `johnstudio run demo "build X"` → orchestrator
2. Insert `tasks` row, scaffold `task-NNNN/` folder.
3. `choose_team()` based on flags + worker availability (stub always available).
4. For each edit-capable worker: `git worktree add` + branch `ai/task-NNNN/<worker>`.
5. `context_builder.build_context_pack()` per worker → `prompts/<worker>.md`.
6. tmux pane per worker (or subprocess fallback). For interactive CLIs, send `Read <prompt>` instruction.
7. Worker writes `RESULT.md` + `DONE.md` in its worktree.
8. `collect` reads results, diffs, runs tests, surfaces safety hits.
9. `review` scores deterministically, writes `FINAL_REVIEW.md` + `MERGE_PLAN.md`.
10. `merge <worker>` requires explicit human confirmation; writes decision + updates knowledge graph.

## Invariants
- Workers never spawn workers. They may write `HANDOFF_REQUEST.md`; orchestrator decides.
- Imported skills default to `enabled: false` + `trust_level: unreviewed`.
- Merge requires `confirm=True` (CLI prompts y/N).
- `.johnstudio/` is intentionally untracked in the user's repo (untracked files don't block merge).
