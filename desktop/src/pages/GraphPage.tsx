// Live tree view for one project.
//
// One SSE connection per page (`/api/projects/:id/stream`) drives every
// node update. The initial `snapshot` event seeds the topology;
// subsequent `task_state`, `phase_state`, and `worker_event` events
// mutate it in place. Auto-layout via dagre keeps the tree readable as
// nodes come and go. Click any node to open a side panel with the last
// ~30 events for that node.

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams, Link } from "react-router-dom";
import ReactFlow, {
  Background,
  Controls,
  MiniMap,
  Handle,
  Position,
  useReactFlow,
  ReactFlowProvider,
  type Node,
  type Edge,
} from "reactflow";
import "reactflow/dist/style.css";
import dagre from "dagre";

import { connectStream } from "../lib/stream";
import { teamCatalog, runTranscript, type TranscriptEntry, type TranscriptResponse } from "../api/client";

// ---------------------------------------------------------------------------
// Types from the SSE feed
// ---------------------------------------------------------------------------

type Run = {
  id: number;
  task_id?: number;
  status: string;
  worker: string | null;
  worktree_path?: string | null;
  tmux_pane?: string | null;
};

type Task = {
  id: number;
  task_number: number;
  title: string;
  status: string;
  runs?: Run[];
};

type Phase = {
  id: number;
  task_id: number;
  phase: string;
  round: number;
  status: string;
  verdict: string | null;
  notes: string | null;
};

type WorkerEvent = {
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

// Parsed shape of a Claude Code Task tool call (subagent spawn) and its
// eventual tool_result. Both are reconstructed from the raw stream-json
// line that the backend stored under raw_json.
type Subagent = {
  spawn_event_id: number;        // worker_event.id of the spawn:subagent event
  tool_use_id: string;
  subagent_type: string;
  brief: string;
  spawn_ts: string;
  result_event_id?: number;      // worker_event.id of the matching tool_result
  result_content?: string;
  result_ts?: string;
  result_is_error?: boolean;
};

function parseRaw(raw: string | undefined): any {
  if (!raw) return null;
  try { return JSON.parse(raw); } catch { return null; }
}

// From a list of worker_events for one run, extract every Task spawn +
// match its result by tool_use_id.
function extractSubagents(events: WorkerEvent[]): Subagent[] {
  const byToolUseId: Map<string, Subagent> = new Map();
  for (const e of events) {
    if (e.kind !== "spawn:subagent") continue;
    const r = parseRaw(e.raw_json);
    if (!r) continue;
    const content = (r.message?.content || []).find((c: any) => c?.type === "tool_use" && c?.name === "Task");
    if (!content) continue;
    const tool_use_id = content.id || "";
    const input = content.input || {};
    byToolUseId.set(tool_use_id, {
      spawn_event_id: e.id,
      tool_use_id,
      subagent_type: input.subagent_type || "subagent",
      brief: input.prompt || input.description || "",
      spawn_ts: e.ts,
    });
  }
  for (const e of events) {
    if (e.kind !== "tool_result") continue;
    const r = parseRaw(e.raw_json);
    if (!r) continue;
    const block = (r.message?.content || []).find((c: any) => c?.type === "tool_result");
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
  return Array.from(byToolUseId.values());
}

// One concrete tool call (Bash, Read, Write, Edit, Grep, ...) extracted
// from a specialist's event stream. We render these as a vertical chain
// under the specialist when the user expands its node.
type ToolCall = {
  event_id: number;
  seq: number;
  name: string;     // "Bash", "Read", "Write", ...
  hint: string;     // first command/path/etc, already trimmed by worker_events.py
  ts: string;
};

function extractToolCalls(events: WorkerEvent[]): ToolCall[] {
  const out: ToolCall[] = [];
  for (const e of events) {
    if (!e.kind.startsWith("tool:")) continue;
    const name = e.kind.slice("tool:".length);
    // summary is "Name · hint" — strip the "Name · " prefix if present.
    let hint = e.summary || "";
    const sep = " · ";
    const idx = hint.indexOf(sep);
    if (idx !== -1 && hint.slice(0, idx) === name) hint = hint.slice(idx + sep.length);
    out.push({ event_id: e.id, seq: e.seq, name, hint, ts: e.ts });
  }
  // Sort by seq to preserve order even if events arrived out of order via SSE.
  out.sort((a, b) => a.seq - b.seq);
  return out;
}

type Snapshot = {
  project_id: number;
  tasks: Task[];
  runs: Run[];
  phases: Phase[];
  recent_events: WorkerEvent[];
};

// ---------------------------------------------------------------------------
// Color / status helpers
// ---------------------------------------------------------------------------

function statusColor(status: string): { fg: string; bg: string; ring: string } {
  switch (status) {
    case "running":
    case "launched":
      return { fg: "#60a5fa", bg: "#0b1c33", ring: "#3b82f6" };
    case "completed":
    case "merged":
      return { fg: "#86efac", bg: "#0c2417", ring: "#16a34a" };
    case "failed":
    case "rejected":
    case "error":
      return { fg: "#fca5a5", bg: "#2a0d0d", ring: "#dc2626" };
    case "pending":
    case "pending_merge":
    case "rfc_pending_approval":
    case "conflict":
      return { fg: "#fcd34d", bg: "#2a1d05", ring: "#f59e0b" };
    case "stopped":
      return { fg: "#a3a3a3", bg: "#1f1f1f", ring: "#6b7280" };
    default:
      return { fg: "#d4d4d8", bg: "#171717", ring: "#3f3f46" };
  }
}

const HUMAN_GATES = new Set([
  "rfc_pending_approval",
  "pending_merge",
  "conflict",
]);

// Border tint per VP. Subtle — sits on top of the status color so a node's
// state (running/done/error) still dominates the eye.
const VP_TINT: Record<string, string> = {
  claude_vp: "#3b82f6",   // blue
  codex_vp: "#f59e0b",    // amber
  gemini_vp: "#a855f7",   // violet
};
const VP_LABEL: Record<string, string> = {
  claude_vp: "C",
  codex_vp: "X",
  gemini_vp: "G",
};

// ---------------------------------------------------------------------------
// Custom node — used for tasks, runs, and phases
// ---------------------------------------------------------------------------

type NodeData = {
  label: string;
  sub: string;
  status: string;
  isGate?: boolean;
  isRoot?: boolean;
  vp?: string;          // team-mode roles get a VP tint on the border
  onClick?: () => void;
};

function StatusNode({ data }: { data: NodeData }) {
  const c = statusColor(data.status);
  const pulsing = data.status === "running" || data.status === "launched";
  // VP tint replaces the status ring color for team-mode nodes, so the
  // VP is visually obvious; pulse + glow still come from status.
  const ring = data.vp && VP_TINT[data.vp] ? VP_TINT[data.vp] : c.ring;
  return (
    <div
      onClick={data.onClick}
      style={{
        background: c.bg,
        border: `1.5px solid ${ring}`,
        borderRadius: 10,
        padding: data.isRoot ? "10px 14px" : "8px 12px",
        minWidth: 180,
        maxWidth: 280,
        cursor: data.onClick ? "pointer" : "default",
        boxShadow: pulsing
          ? `0 0 0 3px ${ring}33, 0 0 14px ${ring}88`
          : `0 1px 2px rgba(0,0,0,.4)`,
        animation: pulsing ? "ps-pulse 1.6s ease-in-out infinite" : undefined,
        position: "relative",
      }}
    >
      {data.vp && VP_LABEL[data.vp] && (
        <span
          title={data.vp}
          style={{
            position: "absolute",
            top: -8,
            left: -8,
            background: ring,
            color: "#fff",
            fontSize: 9,
            fontWeight: 700,
            width: 16, height: 16,
            borderRadius: 8,
            display: "flex", alignItems: "center", justifyContent: "center",
            boxShadow: "0 0 0 2px #0a0a0a",
          }}
        >
          {VP_LABEL[data.vp]}
        </span>
      )}
      <Handle type="target" position={Position.Top} style={{ opacity: 0 }} />
      <div
        style={{
          color: c.fg,
          fontSize: data.isRoot ? 14 : 12,
          fontWeight: 600,
          letterSpacing: 0.2,
          display: "flex",
          alignItems: "center",
          gap: 6,
        }}
      >
        {data.isGate && (
          <span style={{ fontSize: 14, opacity: 0.85 }} title="human gate">⏸</span>
        )}
        {data.label}
        <span
          style={{
            marginLeft: "auto",
            fontSize: 10,
            color: c.ring,
            background: "rgba(255,255,255,0.04)",
            padding: "1px 6px",
            borderRadius: 6,
            textTransform: "uppercase",
          }}
        >
          {data.status}
        </span>
      </div>
      {data.sub && (
        <div
          style={{
            marginTop: 4,
            color: "#cbd5e1",
            fontSize: 11,
            lineHeight: 1.35,
            overflow: "hidden",
            textOverflow: "ellipsis",
            display: "-webkit-box",
            WebkitLineClamp: 2,
            WebkitBoxOrient: "vertical",
          }}
          title={data.sub}
        >
          {data.sub}
        </div>
      )}
      <Handle type="source" position={Position.Bottom} style={{ opacity: 0 }} />
    </div>
  );
}

const nodeTypes = { status: StatusNode };

// ---------------------------------------------------------------------------
// Transcript modal — Claude Code's on-disk session transcript ("deep view")
// ---------------------------------------------------------------------------

function TranscriptModal({
  projectId,
  runId,
  onlySidechain,
  onClose,
}: {
  projectId: number;
  runId: number;
  onlySidechain: boolean;
  onClose: () => void;
}) {
  const [data, setData] = useState<TranscriptResponse | null>(null);
  const [err, setErr] = useState<string>("");

  useEffect(() => {
    runTranscript(projectId, runId, { limit: 2000, only_sidechain: onlySidechain })
      .then(setData)
      .catch((e) => setErr(e?.detail || e?.message || String(e)));
  }, [projectId, runId, onlySidechain]);

  return (
    <div
      style={{
        position: "fixed", top: 0, left: 0, width: "100vw", height: "100vh",
        background: "rgba(0,0,0,0.7)", zIndex: 20,
        display: "flex", alignItems: "center", justifyContent: "center",
      }}
      onClick={onClose}
    >
      <div
        style={{
          background: "#0a0a0a", width: "min(1100px, 92vw)", height: "min(85vh, 800px)",
          border: "1px solid #27272a", borderRadius: 10, overflow: "hidden",
          display: "flex", flexDirection: "column",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <div style={{
          padding: "10px 14px", borderBottom: "1px solid #27272a",
          display: "flex", alignItems: "center", justifyContent: "space-between",
        }}>
          <div style={{ color: "#e4e4e7", fontSize: 13, fontWeight: 600 }}>
            {onlySidechain ? "Subagent transcript" : "Full transcript"} · run {runId}
          </div>
          <button onClick={onClose} style={{
            background: "transparent", border: "1px solid #3f3f46", color: "#a1a1aa",
            padding: "2px 10px", borderRadius: 6, cursor: "pointer", fontSize: 12,
          }}>close</button>
        </div>
        {err && (
          <div style={{ padding: 16, color: "#fca5a5", fontSize: 12 }}>
            Couldn't load transcript: {err}
          </div>
        )}
        {!err && !data && (
          <div style={{ padding: 16, color: "#71717a", fontSize: 12 }}>loading…</div>
        )}
        {data && (
          <>
            <div style={{
              padding: "8px 14px", borderBottom: "1px solid #27272a",
              color: "#71717a", fontSize: 10, fontFamily: "ui-monospace, monospace",
              wordBreak: "break-all",
            }}>
              <div>session: {data.session_id || "—"}</div>
              <div>path: {data.transcript_path}</div>
              <div>{data.entries.length} entries · {data.n_sidechain} sidechain (subagent)</div>
            </div>
            <div style={{ flex: 1, overflowY: "auto", padding: 12, fontSize: 11, color: "#e4e4e7" }}>
              {data.entries.map((e) => (
                <TranscriptRow key={e._index} entry={e} />
              ))}
              {data.entries.length === 0 && (
                <div style={{ color: "#71717a", padding: 12 }}>
                  No entries match this filter.
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function TranscriptRow({ entry }: { entry: TranscriptEntry }) {
  const [open, setOpen] = useState(false);
  const side = !!entry.isSidechain;
  const c = side ? "#a855f7" : "#3f3f46";
  return (
    <div style={{
      padding: "6px 10px", marginBottom: 4, background: "#111",
      borderLeft: `2px solid ${c}`, borderRadius: 4, fontFamily: "ui-monospace, monospace",
    }}>
      <div
        onClick={() => setOpen(!open)}
        style={{ cursor: "pointer", display: "flex", gap: 6, alignItems: "baseline" }}
      >
        <span style={{ color: "#71717a", fontSize: 10, minWidth: 32 }}>
          #{entry._index}
        </span>
        {side && (
          <span style={{
            fontSize: 9, color: "#fff", background: "#a855f7",
            padding: "1px 4px", borderRadius: 4,
          }}>SIDE</span>
        )}
        <span style={{ color: "#e4e4e7", wordBreak: "break-word" }}>{entry._kind_summary}</span>
        <span style={{ marginLeft: "auto", color: "#71717a", fontSize: 10 }}>
          {open ? "−" : "+"}
        </span>
      </div>
      {open && (
        <pre style={{
          marginTop: 6, padding: 8, background: "#0a0a0a",
          color: "#cbd5e1", fontSize: 10, overflowX: "auto",
          borderRadius: 4, whiteSpace: "pre-wrap", wordBreak: "break-word",
        }}>
          {JSON.stringify(entry, null, 2)}
        </pre>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Layout
// ---------------------------------------------------------------------------

// Node box used for BOTH dagre layout and the explicit width/height we stamp
// onto every node below. These are the canonical dimensions the StatusNode
// renders into (minWidth 180–maxWidth 280; we layout at a fixed 240×78).
const NODE_W = 240;
const NODE_H = 78;

function layout(nodes: Node[], edges: Edge[]): Node[] {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "TB", nodesep: 36, ranksep: 56, marginx: 24, marginy: 24 });
  g.setDefaultEdgeLabel(() => ({}));
  for (const n of nodes) g.setNode(n.id, { width: NODE_W, height: NODE_H });
  for (const e of edges) g.setEdge(e.source, e.target);
  dagre.layout(g);
  return nodes.map((n) => {
    const pos = g.node(n.id);
    // ITEM 12 FIX (root cause): ReactFlow keeps a node visually hidden
    // (visibility:hidden) until it has *measured* the node's DOM box. On a
    // fast-streaming team task, nodes are added/replaced ~4×/sec; the
    // measurement pass races the render and some nodes stay hidden until
    // the next layout effect — the flicker/“nodes don’t appear” bug. By
    // giving each node an explicit `width`/`height` (which ReactFlow v11
    // treats as already-measured dimensions) plus a matching `style`, the
    // measurement gate is satisfied synchronously at create time, so nodes
    // are visible on first paint. This removes the need for any
    // `.react-flow__node { visibility: visible !important }` CSS hack.
    return {
      ...n,
      position: { x: pos.x - NODE_W / 2, y: pos.y - NODE_H / 2 },
      width: NODE_W,
      height: NODE_H,
      style: { width: NODE_W, ...(n.style || {}) },
    };
  });
}

// ---------------------------------------------------------------------------
// Chain-mode handoff topology
// ---------------------------------------------------------------------------

const CHAIN_ORDER = [
  "rfc_drafting",
  "rfc_review",
  "rfc_pending_approval",
  "implementing",
  "reviewing",
  "pending_merge",
  "merged",
];

// ---------------------------------------------------------------------------
// Build nodes/edges from the model
// ---------------------------------------------------------------------------

function buildGraph(
  projectId: number,
  tasks: Task[],
  runs: Run[],
  phases: Phase[],
  latestByRun: Map<number, WorkerEvent>,
  latestByPhase: Map<number, WorkerEvent>,
  subagentsByRun: Map<number, Subagent[]>,
  roleToVp: Map<string, string>,
  onSelect: (sel: Selection) => void,
): { nodes: Node[]; edges: Edge[] } {
  const nodes: Node[] = [];
  const edges: Edge[] = [];

  // Root project node
  const rootId = `proj-${projectId}`;
  nodes.push({
    id: rootId,
    type: "status",
    position: { x: 0, y: 0 },
    data: {
      label: `project · ${tasks.length} task${tasks.length === 1 ? "" : "s"}`,
      sub: tasks.map((t) => `#${t.task_number} ${t.title.slice(0, 36)}…`).join(" · "),
      status: "running",
      isRoot: true,
      onClick: () => onSelect({ kind: "project" }),
    },
  });

  for (const t of tasks) {
    const tid = `task-${t.id}`;
    nodes.push({
      id: tid,
      type: "status",
      position: { x: 0, y: 0 },
      data: {
        label: `Task ${t.task_number}`,
        sub: t.title,
        status: t.status,
        onClick: () => onSelect({ kind: "task", id: t.id }),
      },
    });
    edges.push({ id: `${rootId}-${tid}`, source: rootId, target: tid, animated: t.status === "running" });

    const taskPhases = phases.filter((p) => p.task_id === t.id);
    const taskRuns = runs.filter((r) => r.task_id === t.id);

    if (taskPhases.length > 0) {
      // Chain mode: order phases by the canonical sequence, fall back to id.
      const ordered = [...taskPhases].sort((a, b) => {
        const ai = CHAIN_ORDER.indexOf(a.phase);
        const bi = CHAIN_ORDER.indexOf(b.phase);
        if (ai !== bi) return ai - bi;
        return a.id - b.id;
      });
      let prevId = tid;
      for (const p of ordered) {
        const pid = `phase-${p.id}`;
        const latest = latestByPhase.get(p.id);
        const sub = latest
          ? `${latest.kind}: ${latest.summary}`
          : p.notes || (p.verdict ? `verdict: ${p.verdict}` : "");
        nodes.push({
          id: pid,
          type: "status",
          position: { x: 0, y: 0 },
          data: {
            label: `${p.phase}${p.round ? ` (r${p.round})` : ""}`,
            sub,
            status: p.status === "completed" && p.verdict === "reject" ? "failed" : p.status,
            isGate: HUMAN_GATES.has(p.phase),
            onClick: () => onSelect({ kind: "phase", id: p.id }),
          },
        });
        edges.push({
          id: `${prevId}-${pid}`,
          source: prevId,
          target: pid,
          animated: p.status === "running",
          style: { stroke: p.status === "running" ? "#3b82f6" : "#52525b" },
        });
        prevId = pid;
      }
    } else if (taskRuns.length > 0) {
      // Parallel mode OR team mode: each run is a sibling under the task.
      for (const r of taskRuns) {
        const rid = `run-${r.id}`;
        const latest = latestByRun.get(r.id);
        const sub = latest ? `${latest.kind}: ${latest.summary}` : (r.worker || "");
        const vp = r.worker ? roleToVp.get(r.worker) : undefined;
        const subs = subagentsByRun.get(r.id) || [];
        const subLabel = subs.length > 0 ? `${r.worker || `run ${r.id}`} · ${subs.length} subagent${subs.length === 1 ? "" : "s"}` : (r.worker || `run ${r.id}`);
        nodes.push({
          id: rid,
          type: "status",
          position: { x: 0, y: 0 },
          data: {
            label: subLabel,
            sub,
            status: r.status,
            vp,
            onClick: () => onSelect({ kind: "run", id: r.id }),
          },
        });
        edges.push({
          id: `${tid}-${rid}`,
          source: tid,
          target: rid,
          animated: r.status === "running" || r.status === "launched",
          style: { stroke: r.status === "running" || r.status === "launched" ? "#3b82f6" : "#52525b" },
        });
        // Subagent child nodes — one per Task spawn. Status is derived
        // from whether the matching tool_result has landed.
        for (const sa of subs) {
          const sid = `sub-${sa.spawn_event_id}`;
          const saStatus = sa.result_event_id == null
            ? "running"
            : (sa.result_is_error ? "failed" : "completed");
          nodes.push({
            id: sid,
            type: "status",
            position: { x: 0, y: 0 },
            data: {
              label: sa.subagent_type,
              sub: sa.brief.slice(0, 140),
              status: saStatus,
              onClick: () => onSelect({ kind: "subagent", runId: r.id, spawnId: sa.spawn_event_id }),
            },
          });
          edges.push({
            id: `${rid}-${sid}`,
            source: rid,
            target: sid,
            animated: saStatus === "running",
            style: { stroke: saStatus === "running" ? "#a855f7" : "#52525b", strokeDasharray: "4 2" },
          });
        }
      }
    }
  }

  return { nodes: layout(nodes, edges), edges };
}

// ---------------------------------------------------------------------------
// Side panel
// ---------------------------------------------------------------------------

type Selection =
  | { kind: "project" }
  | { kind: "task"; id: number }
  | { kind: "run"; id: number }
  | { kind: "phase"; id: number }
  | { kind: "subagent"; runId: number; spawnId: number };

function SidePanel({
  sel,
  tasks,
  runs,
  phases,
  eventsByRun,
  eventsByPhase,
  subagentsByRun,
  onOpenTranscript,
  onClose,
}: {
  sel: Selection | null;
  tasks: Task[];
  runs: Run[];
  phases: Phase[];
  eventsByRun: Map<number, WorkerEvent[]>;
  eventsByPhase: Map<number, WorkerEvent[]>;
  subagentsByRun: Map<number, Subagent[]>;
  onOpenTranscript: (runId: number, onlySidechain: boolean) => void;
  onClose: () => void;
}) {
  if (!sel) return null;

  let title = "";
  let meta: string[] = [];
  let events: WorkerEvent[] = [];

  if (sel.kind === "project") {
    title = "Project overview";
    meta = [`${tasks.length} tasks · ${runs.length} runs · ${phases.length} phases`];
  } else if (sel.kind === "task") {
    const t = tasks.find((x) => x.id === sel.id);
    if (!t) return null;
    title = `Task ${t.task_number}`;
    meta = [t.title, `status: ${t.status}`];
  } else if (sel.kind === "run") {
    const r = runs.find((x) => x.id === sel.id);
    if (!r) return null;
    title = r.worker || `Run ${r.id}`;
    meta = [`status: ${r.status}`, r.worktree_path ? `worktree: ${r.worktree_path.replace(/^.*\//, "…/")}` : ""];
    events = (eventsByRun.get(sel.id) || []).slice(-40);
    const sub = (subagentsByRun.get(sel.id) || []).length;
    return (
      <div
        style={{
          position: "absolute", top: 0, right: 0, width: 420, height: "100%",
          background: "#0a0a0a", borderLeft: "1px solid #27272a",
          padding: 16, overflowY: "auto", zIndex: 10, fontSize: 12, color: "#e4e4e7",
        }}
      >
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div style={{ fontSize: 15, fontWeight: 600 }}>{title}</div>
          <button onClick={onClose} style={{
            background: "transparent", border: "1px solid #3f3f46", color: "#a1a1aa",
            padding: "2px 8px", borderRadius: 6, cursor: "pointer",
          }}>close</button>
        </div>
        {meta.filter(Boolean).map((m, i) => (
          <div key={i} style={{ color: "#a1a1aa", fontSize: 11, marginTop: 4 }}>{m}</div>
        ))}
        <div style={{ marginTop: 12, display: "flex", gap: 6, flexWrap: "wrap" }}>
          <button
            onClick={() => onOpenTranscript(sel.id, false)}
            style={{
              fontSize: 11, padding: "4px 8px", borderRadius: 6,
              background: "#1e1e1e", border: "1px solid #3f3f46",
              color: "#e4e4e7", cursor: "pointer",
            }}
            title="Read the full Claude Code transcript for this run (everything: tools, reasoning, subagents)"
          >
            Open full transcript
          </button>
          {sub > 0 && (
            <button
              onClick={() => onOpenTranscript(sel.id, true)}
              style={{
                fontSize: 11, padding: "4px 8px", borderRadius: 6,
                background: "#2a1d05", border: "1px solid #f59e0b",
                color: "#fcd34d", cursor: "pointer",
              }}
              title="Filter the transcript to just the sidechain entries — i.e. what the subagents did"
            >
              Subagent-only ({sub})
            </button>
          )}
        </div>
        {events.length > 0 && (
          <div style={{ marginTop: 16 }}>
            <div style={{ color: "#71717a", fontSize: 10, textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 6 }}>
              Last {events.length} live events
            </div>
            {events.map((e) => (
              <div key={e.id} style={{
                padding: "6px 8px", marginBottom: 4, background: "#111",
                borderLeft: e.kind === "spawn:subagent" ? "2px solid #a855f7" : "2px solid #3f3f46",
                borderRadius: 4, fontFamily: "ui-monospace, monospace",
              }}>
                <div style={{ color: "#71717a", fontSize: 10 }}>{e.ts} · {e.kind}</div>
                <div style={{ color: "#e4e4e7", marginTop: 2, wordBreak: "break-word" }}>{e.summary}</div>
              </div>
            ))}
          </div>
        )}
      </div>
    );
  } else if (sel.kind === "phase") {
    const p = phases.find((x) => x.id === sel.id);
    if (!p) return null;
    title = `${p.phase}${p.round ? ` (round ${p.round})` : ""}`;
    meta = [`status: ${p.status}`, p.verdict ? `verdict: ${p.verdict}` : "", p.notes || ""];
    events = (eventsByPhase.get(sel.id) || []).slice(-40);
  } else if (sel.kind === "subagent") {
    const subs = subagentsByRun.get(sel.runId) || [];
    const sa = subs.find((s) => s.spawn_event_id === sel.spawnId);
    if (!sa) return null;
    const parentRun = runs.find((r) => r.id === sel.runId);
    const status = sa.result_event_id == null
      ? "still running"
      : (sa.result_is_error ? "errored" : "completed");
    title = `subagent · ${sa.subagent_type}`;
    meta = [
      parentRun ? `parent: ${parentRun.worker} (run ${parentRun.id})` : "",
      `status: ${status}`,
      `spawned: ${sa.spawn_ts}`,
      sa.result_ts ? `returned: ${sa.result_ts}` : "",
    ];
    return (
      <div
        style={{
          position: "absolute", top: 0, right: 0, width: 480, height: "100%",
          background: "#0a0a0a", borderLeft: "1px solid #27272a",
          padding: 16, overflowY: "auto", zIndex: 10, fontSize: 12, color: "#e4e4e7",
        }}
      >
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div style={{ fontSize: 15, fontWeight: 600 }}>{title}</div>
          <button onClick={onClose} style={{
            background: "transparent", border: "1px solid #3f3f46", color: "#a1a1aa",
            padding: "2px 8px", borderRadius: 6, cursor: "pointer",
          }}>close</button>
        </div>
        {meta.filter(Boolean).map((m, i) => (
          <div key={i} style={{ color: "#a1a1aa", fontSize: 11, marginTop: 4 }}>{m}</div>
        ))}
        <div style={{ marginTop: 16 }}>
          <div style={{ color: "#71717a", fontSize: 10, textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 6 }}>
            Brief sent to subagent
          </div>
          <div style={{
            padding: 10, background: "#111", borderLeft: "2px solid #a855f7",
            borderRadius: 4, fontFamily: "ui-monospace, monospace",
            whiteSpace: "pre-wrap", wordBreak: "break-word", maxHeight: 280, overflowY: "auto",
          }}>
            {sa.brief}
          </div>
        </div>
        <div style={{ marginTop: 16 }}>
          <div style={{ color: "#71717a", fontSize: 10, textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 6 }}>
            Subagent's returned result
          </div>
          {sa.result_content != null ? (
            <div style={{
              padding: 10, background: "#111",
              borderLeft: `2px solid ${sa.result_is_error ? "#dc2626" : "#16a34a"}`,
              borderRadius: 4, fontFamily: "ui-monospace, monospace",
              whiteSpace: "pre-wrap", wordBreak: "break-word", maxHeight: 360, overflowY: "auto",
            }}>
              {sa.result_content}
            </div>
          ) : (
            <div style={{ color: "#71717a", fontSize: 11 }}>
              Subagent is still running — no result yet.
            </div>
          )}
        </div>
        <div style={{ marginTop: 12, display: "flex", gap: 6, flexWrap: "wrap" }}>
          <button
            onClick={() => onOpenTranscript(sel.runId, true)}
            style={{
              fontSize: 11, padding: "4px 8px", borderRadius: 6,
              background: "#2a1d05", border: "1px solid #f59e0b",
              color: "#fcd34d", cursor: "pointer",
            }}
            title="Open the parent run's transcript filtered to just subagent entries"
          >
            Read subagent's full transcript
          </button>
        </div>
        <div style={{ marginTop: 16, color: "#71717a", fontSize: 10 }}>
          <em>
            The above is what the parent saw — the subagent's brief and final reply.
            The button reads ~/.claude/projects/.../{`{session}.jsonl`} from disk so you
            can replay every tool call the subagent made.
          </em>
        </div>
      </div>
    );
  }

  return (
    <div
      style={{
        position: "absolute",
        top: 0,
        right: 0,
        width: 380,
        height: "100%",
        background: "#0a0a0a",
        borderLeft: "1px solid #27272a",
        padding: 16,
        overflowY: "auto",
        zIndex: 10,
        fontSize: 12,
        color: "#e4e4e7",
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ fontSize: 15, fontWeight: 600 }}>{title}</div>
        <button
          onClick={onClose}
          style={{
            background: "transparent",
            border: "1px solid #3f3f46",
            color: "#a1a1aa",
            padding: "2px 8px",
            borderRadius: 6,
            cursor: "pointer",
          }}
        >
          close
        </button>
      </div>
      {meta.filter(Boolean).map((m, i) => (
        <div key={i} style={{ color: "#a1a1aa", fontSize: 11, marginTop: 4 }}>{m}</div>
      ))}
      {events.length > 0 && (
        <div style={{ marginTop: 16 }}>
          <div style={{ color: "#71717a", fontSize: 10, textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 6 }}>
            Last {events.length} events
          </div>
          {events.map((e) => (
            <div
              key={e.id}
              style={{
                padding: "6px 8px",
                marginBottom: 4,
                background: "#111",
                borderLeft: "2px solid #3f3f46",
                borderRadius: 4,
                fontFamily: "ui-monospace, monospace",
              }}
            >
              <div style={{ color: "#71717a", fontSize: 10 }}>{e.ts} · {e.kind}</div>
              <div style={{ color: "#e4e4e7", marginTop: 2, wordBreak: "break-word" }}>{e.summary}</div>
            </div>
          ))}
        </div>
      )}
      {sel.kind === "phase" && events.length === 0 && (
        <div style={{ marginTop: 16, color: "#71717a", fontSize: 11 }}>
          No stream-json events recorded for this phase. Either it finished
          before event capture was wired or it doesn't emit a structured stream.
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export function GraphPage() {
  return (
    <ReactFlowProvider>
      <GraphInner />
    </ReactFlowProvider>
  );
}

function GraphInner() {
  const { id } = useParams<{ id: string }>();
  const projectId = Number(id);
  const rf = useReactFlow();
  const [transcriptFor, setTranscriptFor] = useState<{ runId: number; onlySidechain: boolean } | null>(null);

  const [tasks, setTasks] = useState<Task[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [phases, setPhases] = useState<Phase[]>([]);
  const [eventsByRun, setEventsByRun] = useState<Map<number, WorkerEvent[]>>(new Map());
  const [eventsByPhase, setEventsByPhase] = useState<Map<number, WorkerEvent[]>>(new Map());
  const [latestByRun, setLatestByRun] = useState<Map<number, WorkerEvent>>(new Map());
  const [latestByPhase, setLatestByPhase] = useState<Map<number, WorkerEvent>>(new Map());
  const [roleToVp, setRoleToVp] = useState<Map<string, string>>(new Map());
  const [sel, setSel] = useState<Selection | null>(null);
  const [connected, setConnected] = useState(false);
  const [lastEventTs, setLastEventTs] = useState<string>("");

  // One-shot fetch of the role catalog so we can color team-mode nodes by VP.
  useEffect(() => {
    if (!projectId) return;
    teamCatalog(projectId)
      .then((c) => {
        const m = new Map<string, string>();
        for (const [vp, roles] of Object.entries(c.by_vp)) {
          for (const r of roles) m.set(r.name, vp);
        }
        setRoleToVp(m);
      })
      .catch(() => { /* catalog optional — graph still works without it */ });
  }, [projectId]);

  // Performance: the raw SSE stream fires per worker_event (one per tool
  // call, per assistant text, per tool_result). A busy team task does
  // 10-30 events/sec across 5 live specialists — naively cloning four
  // Maps + re-running buildGraph + dagre + ReactFlow render on each is
  // what made the page glitchy / laggy.
  //
  // We coalesce: ingestEvent just pushes onto a ref, and a 250ms timer
  // flushes the batch into React state. One render per quarter-second
  // regardless of event rate. Also caps per-run history to MAX_EVENTS
  // (we only need ~50 most-recent for subagent extraction; older events
  // are dropped to keep memory + dependency-comparison cheap).
  const MAX_EVENTS_PER_RUN = 50;
  const pendingEventsRef = useRef<WorkerEvent[]>([]);
  const flushTimerRef = useRef<number | null>(null);

  const flushPending = useCallback(() => {
    flushTimerRef.current = null;
    const batch = pendingEventsRef.current;
    if (batch.length === 0) return;
    pendingEventsRef.current = [];

    // One Map clone per state, one mutation per event. Dedupe in-place.
    setEventsByRun((m) => {
      const n = new Map(m);
      for (const ev of batch) {
        if (ev.run_id == null) continue;
        const arr = n.get(ev.run_id) || [];
        if (arr.some((x) => x.id === ev.id)) continue;
        const next = arr.length >= MAX_EVENTS_PER_RUN
          ? [...arr.slice(arr.length - MAX_EVENTS_PER_RUN + 1), ev]
          : [...arr, ev];
        n.set(ev.run_id, next);
      }
      return n;
    });
    setEventsByPhase((m) => {
      const n = new Map(m);
      for (const ev of batch) {
        if (ev.phase_id == null) continue;
        const arr = n.get(ev.phase_id) || [];
        if (arr.some((x) => x.id === ev.id)) continue;
        const next = arr.length >= MAX_EVENTS_PER_RUN
          ? [...arr.slice(arr.length - MAX_EVENTS_PER_RUN + 1), ev]
          : [...arr, ev];
        n.set(ev.phase_id, next);
      }
      return n;
    });
    setLatestByRun((m) => {
      const n = new Map(m);
      for (const ev of batch) {
        if (ev.run_id == null) continue;
        const cur = n.get(ev.run_id);
        if (!cur || cur.id < ev.id) n.set(ev.run_id, ev);
      }
      return n;
    });
    setLatestByPhase((m) => {
      const n = new Map(m);
      for (const ev of batch) {
        if (ev.phase_id == null) continue;
        const cur = n.get(ev.phase_id);
        if (!cur || cur.id < ev.id) n.set(ev.phase_id, ev);
      }
      return n;
    });
    const last = batch[batch.length - 1];
    if (last) setLastEventTs(last.ts);
  }, []);

  const ingestEvent = useCallback((ev: WorkerEvent) => {
    pendingEventsRef.current.push(ev);
    if (flushTimerRef.current == null) {
      flushTimerRef.current = window.setTimeout(flushPending, 250);
    }
  }, [flushPending]);

  // Drain any pending events on unmount.
  useEffect(() => () => {
    if (flushTimerRef.current != null) {
      clearTimeout(flushTimerRef.current);
      flushPending();
    }
  }, [flushPending]);

  useEffect(() => {
    if (!projectId) return;
    const ac = connectStream(
      `/api/projects/${projectId}/stream`,
      (e) => {
        setConnected(true);
        if (e.event === "snapshot") {
          const s = e.data as Snapshot;
          setTasks(s.tasks);
          setRuns(s.runs);
          setPhases(s.phases);
          for (const ev of s.recent_events || []) ingestEvent(ev);
        } else if (e.event === "task_state") {
          const t = e.data as Task & { task_id: number };
          setTasks((prev) => {
            const i = prev.findIndex((x) => x.id === t.task_id);
            const next: Task = { id: t.task_id, task_number: t.task_number, title: t.title, status: t.status };
            if (i === -1) return [...prev, next];
            const c = prev.slice(); c[i] = next; return c;
          });
          // Refresh runs from the embedded payload.
          if ((e.data as any).runs) {
            setRuns((prev) => {
              const others = prev.filter((r) => r.task_id !== t.task_id);
              const incoming: Run[] = ((e.data as any).runs || []).map((r: any) => ({
                id: r.id, task_id: t.task_id, status: r.status, worker: r.worker,
                tmux_pane: r.pane, worktree_path: r.worktree,
              })).filter((r: Run) => r.id != null);
              return [...others, ...incoming];
            });
          }
        } else if (e.event === "phase_state") {
          const p = e.data as Phase;
          setPhases((prev) => {
            const i = prev.findIndex((x) => x.id === p.id);
            if (i === -1) return [...prev, p];
            const c = prev.slice(); c[i] = p; return c;
          });
        } else if (e.event === "worker_event") {
          ingestEvent(e.data as WorkerEvent);
        }
      },
      (err) => {
        console.warn("stream error", err);
        setConnected(false);
      },
    );
    return () => ac.abort();
  }, [projectId, ingestEvent]);

  // Derive subagents per run from the event log. Cheap — runs over what's
  // already in memory; updates as new events stream in.
  const subagentsByRun = useMemo(() => {
    const m = new Map<number, Subagent[]>();
    for (const [runId, evs] of eventsByRun.entries()) {
      const subs = extractSubagents(evs);
      if (subs.length > 0) m.set(runId, subs);
    }
    return m;
  }, [eventsByRun]);

  const { nodes, edges } = useMemo(
    () => buildGraph(projectId, tasks, runs, phases, latestByRun, latestByPhase, subagentsByRun, roleToVp, setSel),
    [projectId, tasks, runs, phases, latestByRun, latestByPhase, subagentsByRun, roleToVp],
  );

  // Re-fit the viewport whenever the topology changes. react-flow's own
  // fitView() races with its internal node-measurement pass, so we
  // compute the fit ourselves from the dagre positions (which we already
  // know are correct) and call setViewport directly. Padding = 8%.
  // Re-fit the viewport ONLY when topology actually changes (node count).
  // Previously we depended on `nodes` itself, but the nodes array gets a
  // fresh reference on every event flush (buildGraph allocates new node
  // objects to update sub-line text), causing the viewport to re-animate
  // 4× per second — that's the glitchy / drifting feel users see when
  // many specialists are streaming. The viewport's job is layout fit,
  // not text-change tracking, so node-count is the right gate.
  const nodeCount = nodes.length;
  const nodePositionsKey = useMemo(
    () => nodes.map((n) => `${n.id}@${Math.round(n.position.x)},${Math.round(n.position.y)}`).join("|"),
    [nodes],
  );
  useEffect(() => {
    if (nodes.length === 0) return;
    let minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
    for (const n of nodes) {
      const px = n.position.x, py = n.position.y;
      minx = Math.min(minx, px); maxx = Math.max(maxx, px + NODE_W);
      miny = Math.min(miny, py); maxy = Math.max(maxy, py + NODE_H);
    }
    const w = maxx - minx, h = maxy - miny;
    const pad = 0.08;
    const el = document.querySelector(".react-flow") as HTMLElement | null;
    const vw = el?.clientWidth || 1200;
    const vh = el?.clientHeight || 700;
    const zoom = Math.min(
      (vw * (1 - pad * 2)) / w,
      (vh * (1 - pad * 2)) / h,
      1.0,
    );
    const cx = (minx + maxx) / 2;
    const cy = (miny + maxy) / 2;
    const x = vw / 2 - cx * zoom;
    const y = vh / 2 - cy * zoom;
    const id = requestAnimationFrame(() => rf.setViewport({ x, y, zoom }, { duration: 250 }));
    return () => cancelAnimationFrame(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- intentional: we want
    // re-fit ONLY when the actual topology changes (count + positions), NOT
    // when nodes get fresh object identities from text-only updates.
  }, [nodeCount, nodePositionsKey, rf]);

  return (
    <div style={{ position: "relative", width: "100%", height: "calc(100vh - 64px)" }}>
      <style>{`@keyframes ps-pulse { 0%,100% { transform: scale(1); } 50% { transform: scale(1.025); } }`}</style>
      <div
        style={{
          position: "absolute",
          top: 12,
          left: 12,
          zIndex: 5,
          background: "rgba(0,0,0,.75)",
          border: "1px solid #27272a",
          borderRadius: 8,
          padding: "8px 12px",
          color: "#e4e4e7",
          fontSize: 12,
        }}
      >
        <div>
          <Link to={`/p/${projectId}`} style={{ color: "#60a5fa" }}>← project</Link>
          {" · "}
          <span style={{ color: connected ? "#86efac" : "#fca5a5" }}>
            {connected ? "● live" : "○ offline"}
          </span>
          {lastEventTs && <span style={{ color: "#71717a", marginLeft: 8 }}>last: {lastEventTs}</span>}
        </div>
        <div style={{ color: "#a1a1aa", fontSize: 11, marginTop: 4 }}>
          {tasks.length} task{tasks.length === 1 ? "" : "s"} · {runs.length} run{runs.length === 1 ? "" : "s"} · {phases.length} phase{phases.length === 1 ? "" : "s"} · {Array.from(latestByRun.values()).length + Array.from(latestByPhase.values()).length} live nodes
        </div>
      </div>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        fitView
        minZoom={0.2}
        maxZoom={1.5}
        proOptions={{ hideAttribution: true }}
        style={{ background: "#0a0a0a" }}
      >
        <Background gap={20} size={1} color="#1f1f1f" />
        <Controls position="bottom-right" />
        <MiniMap pannable zoomable style={{ background: "#111", border: "1px solid #27272a" }} />
      </ReactFlow>
      <SidePanel
        sel={sel}
        tasks={tasks}
        runs={runs}
        phases={phases}
        eventsByRun={eventsByRun}
        eventsByPhase={eventsByPhase}
        subagentsByRun={subagentsByRun}
        onOpenTranscript={(runId, only) => setTranscriptFor({ runId, onlySidechain: only })}
        onClose={() => setSel(null)}
      />
      {transcriptFor && (
        <TranscriptModal
          projectId={projectId}
          runId={transcriptFor.runId}
          onlySidechain={transcriptFor.onlySidechain}
          onClose={() => setTranscriptFor(null)}
        />
      )}
    </div>
  );
}
