// Shared types for the live-tree view.
//
// These mirror the SSE feed shapes emitted by the backend
// (`johnstudio/worker_events.py` + the project stream endpoint). The live
// tree normalizes the raw stream into tasks / runs / phases and a flat
// `worker_events` log; the extractors then reconstruct the subagent forest
// from that log.
//
// Kept deliberately loose where the backend returns dict[str, Any] — the
// extractors parse `raw_json` defensively and tolerate missing fields.

// A single run (one specialist working on a task). Mirrors RunStatus in
// lib/types.ts but trimmed to what the tree needs.
export type Run = {
  id: number;
  task_id?: number;
  status: string;
  worker: string | null;
  worktree_path?: string | null;
  tmux_pane?: string | null;
};

// A task in a project.
export type Task = {
  id: number;
  task_number: number;
  title: string;
  status: string;
  runs?: Run[];
};

// One phase of a phased (DAG) team task. See johnstudio/dag.py.
export type Phase = {
  id: number;
  task_id: number;
  phase: string;
  round: number;
  status: string;
  verdict: string | null;
  notes: string | null;
};

// One normalized worker_event row. `raw_json` is the original stream-json
// line (Claude / Codex / Gemini) the backend stored, used by the extractors
// to recover tool_use ids, subagent spawns, and parent_tool_use_id links.
export type WorkerEvent = {
  id: number;
  run_id: number | null;
  task_id: number | null;
  phase_id: number | null;
  seq: number;
  ts: string;
  kind: string;
  summary: string;
  raw_json?: string;
};

// A subagent (Task-tool spawn) reconstructed from the event log. The spawn
// frame (`spawn:subagent`, or a raw `tool:Task`) carries the brief; the
// matching `tool_result` (by tool_use_id) carries the outcome.
//
// `children` is the recursive forest: subagents can themselves spawn
// subagents. buildSubagentTree() nests by parent_tool_use_id; the flat
// extractors leave it empty.
export type Subagent = {
  spawn_event_id: number; // worker_event.id of the spawn frame
  tool_use_id: string; // Task tool_use id — links spawn → result + children
  parent_tool_use_id?: string; // emitter's tool_use_id, if this spawn nested
  subagent_type: string; // role / subagent_type from the Task input
  brief: string; // prompt/description sent to the subagent
  spawn_ts: string;
  result_event_id?: number; // worker_event.id of the matching tool_result
  result_content?: string;
  result_ts?: string;
  result_is_error?: boolean;
  children: Subagent[];
};
