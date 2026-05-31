# UI/Backend Readiness Audit

Date: 2026-05-28. Backend pytest state: **86 tests, 85 passing, 1 skipped (tmux not installed)**.

This audit maps every spec'd API route to the backing module function. Used to decide
which UI buttons are real vs. "coming soon" before the FastAPI server and UI start.

---

## A. Mapping table — spec API route → backend function

| Route | Backing function | Ready? | Notes |
|---|---|:-:|---|
| `GET /api/health` | (new) trivial | ✅ | Synthesize in `routes_system.py`. |
| `GET /api/doctor` | `init.run_init()` + `shutil.which` | ✅ | Re-use availability checks. |
| `GET /api/projects` | `project.list_projects()` | ✅ | |
| `POST /api/projects` | `project.add_project(name, repo_path)` | ✅ | Raises `NotAGitRepoError` / `FileNotFoundError`. |
| `GET /api/projects/{id}` | `project.get_project(name)` + `config.load_project_config` | ⚠ | Need wrapper that looks up by integer ID. |
| `GET /api/projects/{id}/memory` | `memory.memory_root` + dir listing | ✅ | Wrap as JSON listing. |
| `GET /api/projects/{id}/graph` | `knowledge_graph.list_entities/list_relationships` | ✅ | |
| `GET /api/projects/{id}/tasks` | (new) SELECT from `tasks` table | ⚠ | Add `project.list_tasks(project_id)`. |
| `POST /api/projects/{id}/tasks/run` | `orchestrator.run(project_name, task_text, ...)` | ✅ | `KeyError` if project missing. |
| `GET /api/projects/{id}/tasks/{n}` | `orchestrator.status(n, project_name)` | ✅ | |
| `POST .../collect` | `collector.collect(n, project_name)` | ✅ | |
| `POST .../review` | `reviewer.review(n, project_name)` | ✅ | Re-collects internally. |
| `POST .../merge` | `merger.merge(n, project_name, worker, confirm=True)` | ✅ | Raises `MergeAborted` if dirty / unconfirmed. |
| `POST .../stop` | `orchestrator.stop(n, project_name)` | ✅ | |
| `POST .../cleanup` | `orchestrator.cleanup(n, project_name, prune_worktrees=True)` | ✅ | |
| `POST .../resume` | `orchestrator.resume(n, project_name, worker)` | ✅ | |
| `GET .../context-packs` | (new) read `tasks/task-NNNN/prompts/*` | ⚠ | Pure dir listing/read; wrap. |
| `GET .../results` | (new) read `tasks/task-NNNN/results/*` | ⚠ | Same. |
| `GET .../diffs` | (new) read `tasks/task-NNNN/diffs/*` | ⚠ | Same. |
| `GET .../review` | (new) read `FINAL_REVIEW.md` | ⚠ | Same. |
| `GET .../merge-plan` | (new) read `MERGE_PLAN.md` | ⚠ | Same. |
| `GET .../safety-report` | derived from `collector.collect()` summary | ⚠ | Wrap last collect result into JSON safety report. |
| `GET .../logs` | (new) read `tasks/task-NNNN/logs/*` | ⚠ | Same as artifacts. |
| `GET /api/skills` | `skill_registry.list_skills(...)` | ✅ | |
| `GET /api/skills/{id}` | `skill_registry.show_skill(id)` + read on-disk metadata.yaml + distilled.md + summary.md | ⚠ | Add `read_skill_files(id)` helper. |
| `POST /api/skills/{id}/enable` | `skill_registry.set_enabled(id, True)` | ✅ | |
| `POST /api/skills/{id}/disable` | `skill_registry.set_enabled(id, False)` | ✅ | |
| `POST /api/projects/{id}/skills/{sid}/pin` | `skill_registry.pin_skill(repo, sid)` | ✅ | |
| `POST /api/projects/{id}/skills/{sid}/unpin` | `skill_registry.unpin_skill(repo, sid)` | ✅ | |
| `POST /api/skills/source` | `skill_source.add_source(uri)` | ✅ | |
| `GET /api/skills/sources` | `skill_source.list_sources()` | ✅ | |
| `POST /api/skills/sources/scan` | `skill_source.scan_sources()` | ✅ | |
| `POST /api/projects/{id}/skills/discover` | `skill_router.route(...)` + project config | ✅ | UI passes a hypothetical task string to preview routing. |
| `GET /api/workers` | (new) iterate `global_cfg.workers` + `make_worker(...).is_available()` | ⚠ | Wrap. |
| `GET /api/workers/doctor` | Same as `/api/doctor` filtered to worker section. | ✅ | |
| `POST /api/workers/{name}/test` | (new) launch the worker against a throwaway prompt and check `RESULT.md`/`DONE.md` | ⚠ | For terminal_stub: easy. For real CLIs: launch and return "started"; full verification is off-MVP. |
| `GET /api/projects/{id}/memory/files` | walk `memory.memory_root(repo)` | ⚠ | New small helper. |
| `GET /api/projects/{id}/memory/file?path=` | read scoped path (with traversal guard) | ⚠ | Must validate `path` is under the vault. |
| `GET /api/projects/{id}/memory/entities` | `knowledge_graph.list_entities(pid)` | ✅ | |
| `GET /api/projects/{id}/memory/relationships` | `knowledge_graph.list_relationships(pid)` | ✅ | |
| `GET /api/projects/{id}/memory/backlinks?note=` | `knowledge_graph.build_backlink_index(repo)[note]` | ✅ | |
| `POST /api/projects/{id}/memory/validate` | (new) check vault layout matches expected files/dirs | ⚠ | Easy wrapper. |
| `POST /api/projects/{id}/memory/repair` | call `memory.init_vault(repo)` (idempotent) | ✅ | |

Legend: ✅ usable as-is. ⚠ needs a thin wrapper (no architectural work).

---

## B. New helper functions to add

Compact list — these are all small adapters; **none** change existing behavior. They live under `johnstudio/api/_helpers.py` so the route modules stay declarative.

1. `_helpers.get_project_by_id(project_id) -> dict | None` — wraps `db.connect()` + `SELECT * FROM projects WHERE id = ?`.
2. `_helpers.list_tasks_for_project(project_id) -> list[dict]` — SELECT from `tasks` ordered by `task_number DESC`.
3. `_helpers.task_folder(repo_path, task_number) -> Path`.
4. `_helpers.read_skill_files(skill_id) -> dict` — assemble `metadata.yaml`, `distilled.md`, `summary.md`, `original.md`.
5. `_helpers.list_workers(global_cfg) -> list[dict]` — iterate `cfg.workers`, include `is_available`.
6. `_helpers.list_memory_files(repo_path) -> list[dict]` — recursive walk of `memory_root`, return relative paths + sizes.
7. `_helpers.read_memory_file(repo_path, rel_path) -> str` — **with traversal guard**: `Path(repo/memory/rel).resolve()` must be under `memory_root(repo).resolve()`.
8. `_helpers.read_text_safely(path, max_bytes=200_000) -> str` — utility for diffs/logs/results.
9. `_helpers.safety_report(collect_summary) -> dict` — extract protected/dangerous/approval hits from collector output.
10. `_helpers.run_doctor() -> dict` — extends `init.run_init()` availability checks with per-worker availability via `make_worker(name, cfg).is_available()`.

---

## C. Commands that need JSON output for UI consumption

The existing `orchestrator.run/status/collect/review/merge` functions already return dicts. No CLI shelling needed — the API will call Python functions directly, so the CLI's stdout format is irrelevant to the UI.

---

## D. Unsafe or incomplete (UI marks disabled / coming-soon)

| Feature | Why disabled in UI | What unblocks it |
|---|---|---|
| Real Claude/Codex/Gemini worker launches via UI | CLI adapters exist but interactive prompt flow needs send-keys orchestration we have not validated against real binaries | Validate against the actual CLIs locally with tmux; then enable. |
| Graph visualization | Spec explicitly says "do not overbuild" — table view in MVP | Add a Cytoscape/d3 visualizer later. |
| Memory `validate` reporting individual fix proposals | We can validate layout; full repair recommendations need an LLM pass | Off-MVP. |
| Live log streaming over WebSocket | MVP uses polling on `/api/.../logs` | Add SSE/WS in a later phase. |
| Auth on the API | Spec says local-only is fine for MVP | Add token in `~/.johnstudio/config.yaml` later. |
| Tauri native shell | No Rust toolchain in this environment | Document Cargo install + `npm run tauri init` as next step. |

---

## E. Decisions for this phase

1. **API talks to backend functions directly** (no subprocess to `johnstudio` CLI), so we avoid shell escaping, JSON parsing, and the cost of re-importing the package per call.
2. **Local-only**: bind to `127.0.0.1` only. No CORS open beyond `localhost:5173` (the Vite dev origin).
3. **Tauri-shaped layout**: code under `desktop/` is plain Vite-React-TS. A `src-tauri/` is **not** included; instead we ship a `desktop/TAURI.md` with the precise `npm create tauri-app@latest` instructions for a later session.
4. **UI honesty**: every action button maps to a real API call, OR is rendered disabled with a tooltip `"coming soon — backend route not implemented"`. No fake spinners.
5. **No vendored shadcn**: we ship a small set of Tailwind primitive components in-repo (`Button`, `Card`, `Badge`, `Input`, `Table`, `Tabs`, `Modal`, `Toast`) — shadcn-shaped but local-owned.
