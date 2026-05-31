---
name: terminal-stub
description: Instructions for the offline test/stub worker (terminal_stub)
category: agent-orchestration
tags: [stub, test, terminal, offline]
languages: []
frameworks: []
agent_roles: [test_worker]
file_patterns: []
---

# Terminal Stub

## Purpose
The `terminal_stub` worker exists so the full JohnStudio pipeline can be exercised
end-to-end without spending any subscription/model usage.

## What it does
- Reads its assigned prompt file.
- Writes a minimal `RESULT.md` describing what it would have done.
- Optionally writes a tiny demo file inside its worktree to simulate a code change.
- Writes `DONE.md` with status `COMPLETE`.

## When to use
- Initial verification after `johnstudio init`.
- Test runs.
- Smoke checks before bringing up real workers.

## What it is not
- Not a model. Not a planner. Not a reviewer. Use real workers for those.
