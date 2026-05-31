// Pure parsing helpers for the live tree.
//
// These take the normalized worker_event log (see ./types) and reconstruct
// the things buildGraph.ts needs that aren't first-class rows in the DB:
//  - the subagent forest (Task-tool spawns + their results, nested),
//  - a one-line "current activity" summary per agent,
//  - a tool-call count for the "🔧 N" badge,
//  - the VP color palette for a node's left stripe.
//
// Everything here is side-effect free so it can be unit-tested in isolation
// and re-run cheaply on every SSE flush.

import type { Subagent, WorkerEvent } from "./types";

// ---------------------------------------------------------------------------
// VP palettes (consumed by StatusNode.tsx)
// ---------------------------------------------------------------------------

// Color scheme for a status node, keyed off its VP (vice-president provider).
// `stripe` is the 4px left border; `cardBg` / `cardBgHover` are the body fill.
export type VpPalette = {
  stripe: string;
  cardBg: string;
  cardBgHover: string;
};

// Root/scaffolding nodes (project, task) — intentionally gray so they read
// as structure, not AI work.
export const NEUTRAL_PALETTE: VpPalette = {
  stripe: "#52525b",
  cardBg: "#171717",
  cardBgHover: "#1f1f1f",
};

// Per-VP palettes. Claude=amber, Codex=emerald, Gemini=sky — matches the
// StatusNode.tsx design note ("LEFT STRIPE color = VP").
// RECONSTRUCTED: exact hex values are a best-effort match to the documented
// amber/emerald/sky scheme; the original constants weren't recoverable.
const VP_PALETTES: Record<string, VpPalette> = {
  claude_vp: { stripe: "#f59e0b", cardBg: "#1c1505", cardBgHover: "#241a06" }, // amber
  codex_vp: { stripe: "#10b981", cardBg: "#06160f", cardBgHover: "#081d14" }, // emerald
  gemini_vp: { stripe: "#0ea5e9", cardBg: "#04141f", cardBgHover: "#061b29" }, // sky
};

// Resolve a node's palette from its VP. Unknown / falsy VP → neutral.
export function paletteForVp(vp: string | null | undefined): VpPalette {
  if (!vp) return NEUTRAL_PALETTE;
  return VP_PALETTES[vp] || NEUTRAL_PALETTE;
}

// ---------------------------------------------------------------------------
// raw_json parsing
// ---------------------------------------------------------------------------

function parseRaw(raw: string | undefined): any {
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

// Pull the message.content array out of a parsed stream-json frame, tolerating
// the few shapes the different harnesses emit.
function messageContent(r: any): any[] {
  const c = r?.message?.content ?? r?.content;
  return Array.isArray(c) ? c : [];
}

// The emitter (parent) tool_use_id for a frame, if any. Subagents run as a
// Claude Code "sidechain"; their frames carry the spawning Task tool_use_id
// as parent_tool_use_id. Mirrors the candidate-walk in buildGraph.ts.
function parentToolUseId(r: any): string | undefined {
  const candidates = [
    r?.parent_tool_use_id,
    r?.parentToolUseId,
    r?.message?.parent_tool_use_id,
  ];
  for (const c of candidates) {
    if (typeof c === "string" && c) return c;
  }
  for (const block of messageContent(r)) {
    const p = block?.parent_tool_use_id || block?.parentToolUseId;
    if (typeof p === "string" && p) return p;
  }
  return undefined;
}

// ---------------------------------------------------------------------------
// Subagent extraction
// ---------------------------------------------------------------------------

function briefFirstLine(brief: string): string {
  return brief;
}

// Match `tool_result` frames back onto the spawns they belong to, by
// tool_use_id. Mutates the Subagent records in `byToolUseId` in place.
function attachResults(events: WorkerEvent[], byToolUseId: Map<string, Subagent>): void {
  for (const e of events) {
    if (e.kind !== "tool_result") continue;
    const r = parseRaw(e.raw_json);
    if (!r) continue;
    const block = messageContent(r).find((c: any) => c?.type === "tool_result");
    if (!block) continue;
    const sub = byToolUseId.get(block.tool_use_id || "");
    if (!sub) continue; // result for a non-Task tool
    let body = "";
    if (typeof block.content === "string") body = block.content;
    else if (Array.isArray(block.content)) {
      body = block.content.map((b: any) => b?.text || "").join("\n");
    }
    sub.result_event_id = e.id;
    sub.result_content = body;
    sub.result_ts = e.ts;
    sub.result_is_error = !!block.is_error;
  }
}

// Extract subagents from the canonical `spawn:subagent` events (what the
// backend's worker_events parser promotes Task tool_use blocks to). One
// Subagent per spawn; results matched by tool_use_id. `children` starts empty
// — buildSubagentTree() nests them later.
export function extractSubagents(events: WorkerEvent[]): Subagent[] {
  const byToolUseId = new Map<string, Subagent>();
  for (const e of events) {
    if (e.kind !== "spawn:subagent") continue;
    const r = parseRaw(e.raw_json);
    if (!r) continue;
    const content = messageContent(r).find(
      (c: any) => c?.type === "tool_use" && c?.name === "Task",
    );
    if (!content) continue;
    const tool_use_id = content.id || "";
    const input = content.input || {};
    const brief = input.prompt || input.description || "";
    byToolUseId.set(tool_use_id, {
      spawn_event_id: e.id,
      tool_use_id,
      parent_tool_use_id: parentToolUseId(r),
      subagent_type: input.subagent_type || "subagent",
      brief: briefFirstLine(typeof brief === "string" ? brief : JSON.stringify(brief)),
      spawn_ts: e.ts,
      children: [],
    });
  }
  attachResults(events, byToolUseId);
  return Array.from(byToolUseId.values());
}

// Fallback path: some harnesses emit the Task call as a plain `tool:Task`
// event rather than promoting it to `spawn:subagent`. Recover those too so
// the graph doesn't miss subagents on non-Claude workers.
export function extractTaskToolSubagents(events: WorkerEvent[]): Subagent[] {
  const byToolUseId = new Map<string, Subagent>();
  for (const e of events) {
    // tool:Task (and case variants like tool:task from Codex/Gemini parsers).
    if (!e.kind.toLowerCase().startsWith("tool:task")) continue;
    const r = parseRaw(e.raw_json);
    let tool_use_id = "";
    let input: any = {};
    if (r) {
      const content = messageContent(r).find(
        (c: any) => c?.type === "tool_use" && String(c?.name || "").toLowerCase() === "task",
      );
      if (content) {
        tool_use_id = content.id || "";
        input = content.input || {};
      }
    }
    // Without a tool_use_id we can't dedupe or match a result; fall back to
    // the event id so it still shows as a (resultless) node.
    const key = tool_use_id || `evt-${e.id}`;
    if (byToolUseId.has(key)) continue;
    const brief = input.prompt || input.description || "";
    byToolUseId.set(key, {
      spawn_event_id: e.id,
      tool_use_id,
      parent_tool_use_id: r ? parentToolUseId(r) : undefined,
      subagent_type: input.subagent_type || "subagent",
      brief: typeof brief === "string" ? brief : JSON.stringify(brief),
      spawn_ts: e.ts,
      children: [],
    });
  }
  attachResults(events, byToolUseId);
  return Array.from(byToolUseId.values());
}

// Union two subagent lists, preferring the first (canonical `spawn:subagent`)
// when both describe the same Task. Dedupe by tool_use_id when present,
// otherwise by spawn_event_id.
export function mergeSubagents(a: Subagent[], b: Subagent[]): Subagent[] {
  const out: Subagent[] = [];
  const seenTool = new Set<string>();
  const seenSpawn = new Set<number>();
  for (const sa of [...a, ...b]) {
    if (sa.tool_use_id && seenTool.has(sa.tool_use_id)) continue;
    if (!sa.tool_use_id && seenSpawn.has(sa.spawn_event_id)) continue;
    if (sa.tool_use_id) seenTool.add(sa.tool_use_id);
    seenSpawn.add(sa.spawn_event_id);
    out.push(sa);
  }
  return out;
}

// Nest a flat subagent list into a forest by parent_tool_use_id. A subagent
// whose parent_tool_use_id matches another subagent's tool_use_id becomes a
// child of that subagent; the rest are roots. Cycles / dangling parents are
// treated as roots so nothing is dropped.
export function buildSubagentTree(flat: Subagent[]): Subagent[] {
  // Fresh nodes with empty children so repeated calls are idempotent.
  const nodes = flat.map((s) => ({ ...s, children: [] as Subagent[] }));
  const byTool = new Map<string, Subagent>();
  for (const n of nodes) {
    if (n.tool_use_id) byTool.set(n.tool_use_id, n);
  }
  const roots: Subagent[] = [];
  for (const n of nodes) {
    const parent =
      n.parent_tool_use_id && n.parent_tool_use_id !== n.tool_use_id
        ? byTool.get(n.parent_tool_use_id)
        : undefined;
    if (parent) parent.children.push(n);
    else roots.push(n);
  }
  return roots;
}

// ---------------------------------------------------------------------------
// Activity summary + tool count (per agent node)
// ---------------------------------------------------------------------------

// Count concrete tool calls in an event stream — the `tool:*` kinds, but NOT
// the `spawn:subagent` (a Task spawn is a child node, not a tool badge) nor
// `tool_result` frames. Drives the "🔧 N" badge.
export function countToolCalls(events: WorkerEvent[]): number {
  let n = 0;
  for (const e of events) {
    if (e.kind.startsWith("tool:")) n++;
  }
  return n;
}

// Pick a single human-readable "current activity" line for an agent from its
// most recent meaningful event. Prefers the latest event by seq; skips empty
// summaries and bare tool_result acks where a richer prior frame exists.
// RECONSTRUCTED: the exact preference order wasn't recoverable; this favors
// the most recent non-empty summary, which matches how buildGraph.ts uses it.
export function pickActivitySummary(events: WorkerEvent[]): string {
  if (events.length === 0) return "";
  // Latest first by seq (SSE can deliver out of order).
  const sorted = [...events].sort((a, b) => b.seq - a.seq);
  for (const e of sorted) {
    const s = (e.summary || "").trim();
    if (s) return s.slice(0, 80);
  }
  return "";
}
