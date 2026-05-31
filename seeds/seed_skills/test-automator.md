---
name: test-automator
description: Unit/integration/e2e test design and execution
category: testing
tags: [testing, unit, integration, e2e, tdd, qa]
languages: [python, typescript, javascript]
frameworks: [pytest, jest, vitest, playwright]
agent_roles: [test_writer, qa_reviewer]
file_patterns: ["**/*test*.*", "**/__tests__/**", "tests/**", "**/*.spec.*"]
---

# Test Automator

## When to activate
- Adding a new feature or fixing a bug.
- Refactoring code that lacks coverage.

## Checklist
- Red → Green → Refactor: write the failing test first.
- Unit tests: pure, no I/O. Integration tests: cover the seam.
- One assertion per logical behavior.
- Test names describe behavior: `it_<does_X>_when_<Y>`.

## Must / never
- **must** run the failing test before the fix to verify it actually fails.
- **must** assert error paths, not just happy paths.
- **never** mock the thing under test.
- **never** make tests depend on execution order.

## Anti-patterns
- Snapshot tests that lock implementation details.
- Tests that pass because they don't actually invoke the code.
- One mega-test asserting 20 things.
