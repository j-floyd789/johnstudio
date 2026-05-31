---
name: backend-api-designer
description: REST/OpenAPI API design and implementation guidance
category: backend
tags: [backend, api, rest, openapi, http]
languages: [python, typescript, javascript, go]
frameworks: [fastapi, express, nextjs]
agent_roles: [backend_implementer, api_reviewer]
file_patterns: ["api/**", "**/routes/**", "**/handlers/**", "**/*.controller.*"]
---

# Backend - API Designer

## When to activate
- Designing or modifying HTTP endpoints, request/response schemas, or auth flows.
- Adding a new resource, mutation, or webhook.

## Checklist
- Validate input at the boundary; trust internal callers.
- Return consistent error envelope: `{ error: { code, message, details? } }`.
- Idempotency keys for non-GET endpoints that may retry.
- Pagination: cursor-based for large lists.
- Status codes: 200/201/204/400/401/403/404/409/422/429/5xx.

## Must / never
- **must** version breaking changes via path (`/v1`, `/v2`) or accept-header.
- **must** validate webhook signatures before processing payload.
- **never** put secrets in URLs or query strings.
- **never** return stack traces in production responses.

## Anti-patterns
- "List all" without pagination.
- Cascading deletes without an undo window.
- Returning 200 with an error body.
