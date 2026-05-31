# JohnStudio Roadmap

## Shipped (MVP, this branch)

- Phase 0 research report (live + baked-in)
- `johnstudio init` / `research` / `add-project` / `projects`
- Skill importer (VoltAgent + ECC + alirezarezvani layouts), 10 seed skills, normalized registry
- Skill registry CRUD + deterministic router + token-budgeted selection
- Obsidian-compatible knowledge graph (entities, relationships, backlinks, auto-tag/auto-link)
- Per-agent context-pack builder with explicit rule precedence and output contract
- Git worktree wrappers + tmux controller (with subprocess fallback)
- `terminal_stub` worker (always available, offline)
- Orchestrator: `run` / `status` / `resume` / `stop` / `cleanup`
- Collector: RESULT.md + tmux logs + diffs + tests + safety scans
- Reviewer: deterministic scoring, `FINAL_REVIEW.md`, `MERGE_PLAN.md`
- Merger: human-gated, decision log, memory + graph updates
- Safety scans (protected paths, dangerous commands, approval-required commands)

## Next (not in MVP)

- Real Claude Code / Codex / Gemini CLI adapters wired through tmux send-keys with proper prompt-injection
- `johnstudio skill source add <git-url>` with shallow clone
- LSP-aware "relevant files" detection
- Vector search across `~/.johnstudio/skill-registry` and per-project memory
- Optional reviewer-agent invocation (deterministic + agent review)
- Tauri + React desktop UI (terminal viewer, diff viewer, graph viewer, agent board)

## Watchlist (from research report §F)

Tools we may wrap or import: ccpm, vibe-kanban, claude-mem, claude-context, claude-code-hooks, agento-patronum, VibeGuard, Bouncer, ccmanager, claude-scaffold, pro-workflow, claude-code-sessions.
