---
name: context-manager
description: Token budgeting and per-agent context pack guidance
category: memory-context
tags: [context, tokens, memory, distillation]
languages: []
frameworks: []
agent_roles: [orchestrator]
file_patterns: []
---

# Context Manager

## When to activate
- Composing a per-agent prompt.
- Receiving a HANDOFF_REQUEST.

## Checklist
- Per-agent prompt has at most 6 skills loaded.
- Single skill stays under 2,500 tokens.
- Total skill budget per agent stays under 8,000 tokens.
- Prefer `distilled.md` over `original.md`. Fall back to `summary.md` when over budget.

## Rule precedence (always state this in the prompt)
1. Explicit user instruction
2. Safety policy
3. Project-specific rules
4. Current task instructions
5. Loaded skill guidance
6. General best practices
