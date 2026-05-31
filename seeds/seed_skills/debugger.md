---
name: debugger
description: Root-cause-first debugging methodology
category: debugging
tags: [debugging, root-cause, repro]
languages: []
frameworks: []
agent_roles: [debugger]
file_patterns: []
---

# Debugger

## When to activate
- Bug report or test failure.
- Unexpected production behavior.

## Workflow
1. Reproduce locally with a minimal test before changing anything.
2. Bisect: find the smallest change that triggers the bug.
3. Form a hypothesis. Predict the output. Run the experiment.
4. Read the actual error, not the inferred one.
5. Fix the root cause, not the symptom.

## Must / never
- **must** add a regression test before fixing.
- **must** explain *why* the bug happened, not just *what* fixed it.
- **never** silence errors with try/except to make the test pass.
- **never** disable the failing test.
