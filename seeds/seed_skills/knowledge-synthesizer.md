---
name: knowledge-synthesizer
description: Handoff capsule and decision log authoring
category: memory-context
tags: [memory, handoff, decision-log, summary]
languages: []
frameworks: []
agent_roles: [orchestrator, collector]
file_patterns: ["**/HANDOFF.md", "**/decisions/**", "**/runs/**"]
---

# Knowledge Synthesizer

## When to activate
- After a task completes, before merging.
- When closing a session.

## Handoff capsule sections (required)
- Task
- Outcome
- Current State
- Files Changed
- Decisions Made
- Tests Run
- Known Issues
- Risks
- Next Best Action
- Skills Used
- Lessons Learned
- Suggested Graph Updates

## Must / never
- **must** include concrete file paths and commit-style summaries.
- **must** propose graph entity/relationship updates.
- **never** describe what *should* have happened — describe what did.
