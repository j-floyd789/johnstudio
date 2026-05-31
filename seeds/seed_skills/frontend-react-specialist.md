---
name: frontend-react-specialist
description: React 18+/Next.js modern patterns expert for client UI work
category: frontend
tags: [react, frontend, components, hooks, nextjs, typescript]
languages: [typescript, javascript]
frameworks: [react, nextjs]
agent_roles: [frontend_implementer, ui_reviewer]
file_patterns: ["**/*.tsx", "**/*.jsx", "components/**", "app/**", "pages/**"]
---

# Frontend - React Specialist

## When to activate
- Task touches React components, hooks, Next.js routes, or client state.
- Files matching `**/*.tsx`, `app/**`, `components/**` are in scope.

## Checklist
- Prefer Server Components by default; opt into `"use client"` only when needed.
- Co-locate component, styles, and tests.
- Use TypeScript strict mode. No `any`.
- Avoid prop-drilling beyond 2 levels — lift state or use context.

## Must / never
- **must** name event handlers `handle<Event>` and props `on<Event>`.
- **must** memoize expensive renders with `useMemo`/`memo` when justified by data.
- **never** mutate state directly; always return new objects/arrays.
- **never** use `dangerouslySetInnerHTML` without sanitization.

## Anti-patterns
- `useEffect` for derived state — compute it inline.
- Multiple `useState` calls that move together — collapse into one object or `useReducer`.
- Routing with `window.location` inside Next.js — use `useRouter`.
