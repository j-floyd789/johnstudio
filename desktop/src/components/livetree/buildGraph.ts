// Build ReactFlow nodes/edges from the normalized live-tree state.
//
// Post-pivot rules:
//  - One task in view at a time. The caller passes `focusTaskId` and the
//    builder only emits nodes for that task's subtree (task → phases → runs
//    → subagent forest). Other tasks are hidden — the TaskPicker switches
//    focus.
//  - NO tool-call leaves. Bash/Edit/Read/Write tool kinds are folded into
//    each agent node as a one-line activity summary + a "🔧 N" badge.
//    Detail belongs in the side panel.
//  - Dagre layout: rankdir=LR. With recursion, depth is the dominant axis;
//    laying out left→right gives one column per level and lets siblings
//    stack vertically — fits a wide monitor far better than top→bottom.
//
// Status/text-only changes don't bump the topology key, so the caller can
// `useMemo` and skip the dagre relayout for mere status updates.

import dagre from "dagre";
import type { Edge, Node } from "reactflow";

import {
  buildSubagentTree,
  countToolCalls,
  extractSubagents,
  extractTaskToolSubagents,
  mergeSubagents,
  pickActivitySummary,
} from "./extractors";
import type { Phase, Run, Subagent, Task, WorkerEvent } from "./types";
import type { LiveTreeState } from "./useLiveTreeState";

const CHAIN_ORDER = [
  "rfc_drafting",
  "rfc_review",
  "rfc_pending_approval",
  "implementing",
  "reviewing",
  "pending_merge",
  "merged",
];

const HUMAN_GATES = new Set([
  "rfc_pending_approval",
  "pending_merge",
  "conflict",
]);

export type Selection =
  | { kind: "project" }
  | { kind: "task"; id: number }
  | { kind: "run"; id: number }
  | { kind: "phase"; id: number }
  | { kind: "subagent"; runId: number; spawnId: number };

function layout(nodes: Node[], edges: Edge[]): Node[] {
  const g = new dagre.graphlib.Graph();
  // LR: depth runs left→right, siblings stack vertically. Much friendlier
  // on a wide monitor for the recursive task → run → subagent → subagent…
  // chain. nodesep=32 / ranksep=80 gives the tree breathing room.
  g.setGraph({ rankdir: "LR", nodesep: 32, ranksep: 80, marginx: 32, marginy: 32 });
  g.setDefaultEdgeLabel(() => ({}));
  for (const n of nodes) g.setNode(n.id, { width: 220, height: 64 });
  for (const e of edges) g.setEdge(e.source, e.target);
  dagre.layout(g);
  return nodes.map((n) => {
    const pos = g.node(n.id);
    return { ...n, position: { x: pos.x - 110, y: pos.y - 32 } };
  });
}

function subagentStatus(sa: Subagent): string {
  if (sa.result_event_id == null) return "running";
  return sa.result_is_error ? "failed" : "completed";
}

function addSubagentTree(
  parentNodeId: string,
  forest: Subagent[],
  runId: number,
  parentVp: string | undefined,
  roleToVp: Map<string, string>,
  byParentEvents: Map<string, WorkerEvent[]>,
  nodes: Node[],
  edges: Edge[],
  onSelect: (s: Selection) => void,
) {
  for (const sa of forest) {
    const sid = `sub-${sa.spawn_event_id}`;
    const status = subagentStatus(sa);
    const myEvents = byParentEvents.get(sa.tool_use_id) || [];
    const summary = myEvents.length > 0
      ? pickActivitySummary(myEvents)
      : (status === "completed" ? "Done" : status === "failed" ? "Errored" : "Starting…");
    const toolCount = countToolCalls(myEvents);
    // VP resolution: prefer the role catalog (subagent_type → vp); fall
    // back to inheriting the parent's VP (subagents run inside the
    // parent's CLI session, so they share its provider).
    const vp = roleToVp.get(sa.subagent_type) || parentVp;
    nodes.push({
      id: sid,
      type: "status",
      position: { x: 0, y: 0 },
      data: {
        label: sa.subagent_type,
        sub: summary,
        status,
        vp,
        toolCount,
        onClick: () => onSelect({ kind: "subagent", runId, spawnId: sa.spawn_event_id }),
        onToolBadgeClick: () => onSelect({ kind: "subagent", runId, spawnId: sa.spawn_event_id }),
      },
    });
    edges.push({
      id: `${parentNodeId}-${sid}`,
      source: parentNodeId,
      target: sid,
      animated: status === "running",
      style: { stroke: status === "running" ? "#a855f7" : "#52525b", strokeDasharray: "4 2" },
    });
    if (sa.children.length > 0) {
      addSubagentTree(sid, sa.children, runId, vp, roleToVp, byParentEvents, nodes, edges, onSelect);
    }
  }
}

// Group worker events by their emitting subagent's tool_use_id (the
// `parent_tool_use_id` on assistant_text / tool_use / tool_result frames).
// Events with no parent belong to the run itself.
function groupEventsByParent(events: WorkerEvent[]): {
  runEvents: WorkerEvent[];
  byParent: Map<string, WorkerEvent[]>;
} {
  const runEvents: WorkerEvent[] = [];
  const byParent = new Map<string, WorkerEvent[]>();
  for (const e of events) {
    let parent: string | undefined;
    if (e.raw_json) {
      try {
        const r = JSON.parse(e.raw_json);
        const candidates = [
          r?.parent_tool_use_id,
          r?.parentToolUseId,
          r?.message?.parent_tool_use_id,
        ];
        for (const c of candidates) {
          if (typeof c === "string" && c) { parent = c; break; }
        }
        if (!parent && Array.isArray(r?.message?.content)) {
          for (const block of r.message.content) {
            const p = block?.parent_tool_use_id || block?.parentToolUseId;
            if (typeof p === "string" && p) { parent = p; break; }
          }
        }
      } catch {
        // ignore, treat as run-level
      }
    }
    if (parent) {
      const arr = byParent.get(parent) || [];
      arr.push(e);
      byParent.set(parent, arr);
    } else {
      runEvents.push(e);
    }
  }
  return { runEvents, byParent };
}

export function buildGraph(
  projectId: number,
  state: LiveTreeState,
  roleToVp: Map<string, string>,
  onSelect: (sel: Selection) => void,
  focusTaskId: number | null,
): { nodes: Node[]; edges: Edge[]; topologyKey: string } {
  const { tasks, runs, phases, byRun, byPhase } = state;
  const nodes: Node[] = [];
  const edges: Edge[] = [];

  if (focusTaskId == null) {
    // No task selected (project has zero tasks). Show a single placeholder.
    nodes.push({
      id: `proj-${projectId}`,
      type: "status",
      position: { x: 0, y: 0 },
      data: {
        label: `project ${projectId}`,
        sub: "No tasks yet",
        status: "idle",
        isRoot: true,
        onClick: () => onSelect({ kind: "project" }),
      },
    });
    return { nodes, edges, topologyKey: `empty-${projectId}` };
  }

  const t = tasks.find((x) => x.id === focusTaskId);
  if (!t) {
    nodes.push({
      id: `task-missing-${focusTaskId}`,
      type: "status",
      position: { x: 0, y: 0 },
      data: { label: `Task ${focusTaskId}`, sub: "Loading…", status: "idle" },
    });
    return { nodes, edges, topologyKey: `missing-${focusTaskId}` };
  }

  const tid = `task-${t.id}`;
  nodes.push({
    id: tid,
    type: "status",
    position: { x: 0, y: 0 },
    data: {
      label: `Task ${t.task_number}`,
      sub: t.title,
      status: t.status,
      isRoot: true,
      onClick: () => onSelect({ kind: "task", id: t.id }),
    },
  });

  const taskPhases = phases.filter((p) => p.task_id === t.id);
  const taskRuns = runs.filter((r) => r.task_id === t.id);

  if (taskPhases.length > 0) {
    const ordered = [...taskPhases].sort((a, b) => {
      const ai = CHAIN_ORDER.indexOf(a.phase);
      const bi = CHAIN_ORDER.indexOf(b.phase);
      if (ai !== bi) return ai - bi;
      return a.id - b.id;
    });
    let prevId = tid;
    for (const p of ordered) {
      const pid = `phase-${p.id}`;
      const ps = byPhase.get(p.id);
      const latest = ps?.latest;
      const sub = latest
        ? latest.summary
        : p.notes || (p.verdict ? `verdict: ${p.verdict}` : "");
      nodes.push({
        id: pid,
        type: "status",
        position: { x: 0, y: 0 },
        data: {
          label: `${p.phase}${p.round ? ` (r${p.round})` : ""}`,
          sub: (sub || "").slice(0, 60),
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
  }

  for (const r of taskRuns) {
    const rid = `run-${r.id}`;
    const rs = byRun.get(r.id);
    const allEvents: WorkerEvent[] = rs
      ? [...rs.spawnEvents, ...rs.toolEvents, ...rs.otherEvents]
      : [];
    const { runEvents, byParent } = groupEventsByParent(allEvents);
    const summary = pickActivitySummary(runEvents);
    const toolCount = countToolCalls(runEvents);
    const runVp = r.worker ? roleToVp.get(r.worker) : undefined;
    nodes.push({
      id: rid,
      type: "status",
      position: { x: 0, y: 0 },
      data: {
        label: r.worker || `run ${r.id}`,
        sub: summary,
        status: r.status,
        vp: runVp,
        toolCount,
        onClick: () => onSelect({ kind: "run", id: r.id }),
        onToolBadgeClick: () => onSelect({ kind: "run", id: r.id }),
      },
    });
    edges.push({
      id: `${tid}-${rid}`,
      source: tid,
      target: rid,
      animated: r.status === "running" || r.status === "launched",
      style: { stroke: r.status === "running" || r.status === "launched" ? "#3b82f6" : "#52525b" },
    });

    // Subagent forest — recursive, with per-subagent activity summaries.
    // We pass byParent (events grouped by emitter) so each subagent node
    // can compute its OWN activity summary from its OWN events, not the
    // run's overall stream. We pull subagents from BOTH the canonical
    // `spawn:subagent` kind and the fallback `tool:Task` kind — the
    // server emits the former today, but other worker harnesses (or
    // future ones) might emit the latter directly.
    const spawnFlat = extractSubagents(rs?.spawnEvents || []);
    const toolFlat = extractTaskToolSubagents(rs?.toolEvents || []);
    const tree = buildSubagentTree(mergeSubagents(spawnFlat, toolFlat));
    addSubagentTree(rid, tree, r.id, runVp, roleToVp, byParent, nodes, edges, onSelect);
  }

  const topologyKey = nodes.map((n) => n.id).join("|") + "::" + edges.map((e) => e.id).join("|");

  return { nodes: layout(nodes, edges), edges, topologyKey };
}
