// Live-tree state hook.
//
// Owns the single SSE connection per project and normalizes the raw stream
// into the shape buildGraph.ts consumes: tasks / runs / phases plus two
// per-id event indexes (`byRun`, `byPhase`).
//
// The raw SSE stream fires once per worker_event (one per tool call, per
// assistant text, per tool_result). A busy team task does 10-30 events/sec
// across several live specialists, so we coalesce: ingestEvent pushes onto a
// ref and a 250ms timer flushes the batch into React state. One render per
// quarter-second regardless of event rate. Per-run history is capped to
// MAX_EVENTS_PER_RUN (we only need the most-recent slice for subagent
// extraction + activity summaries).

import { useCallback, useEffect, useRef, useState } from "react";

import { connectStream } from "../../lib/stream";
import type { Phase, Run, Task, WorkerEvent } from "./types";

const MAX_EVENTS_PER_RUN = 80;

// Events for a single run, pre-bucketed by category so buildGraph.ts can pull
// just the spawns (subagent forest) or just the tool calls (badge count)
// without re-scanning. `latest` is the newest event by seq, used for the
// node's "current step" text.
export type RunEvents = {
  spawnEvents: WorkerEvent[]; // kind === "spawn:subagent"
  toolEvents: WorkerEvent[]; // kind startsWith "tool:" (includes tool:Task)
  otherEvents: WorkerEvent[]; // everything else (assistant_text, tool_result, …)
  latest?: WorkerEvent;
};

export type PhaseEvents = {
  events: WorkerEvent[];
  latest?: WorkerEvent;
};

// The normalized live-tree state buildGraph.ts builds nodes/edges from.
export type LiveTreeState = {
  projectId: number;
  tasks: Task[];
  runs: Run[];
  phases: Phase[];
  byRun: Map<number, RunEvents>;
  byPhase: Map<number, PhaseEvents>;
  connected: boolean;
  lastEventTs: string;
};

type Snapshot = {
  project_id: number;
  tasks: Task[];
  runs: Run[];
  phases: Phase[];
  recent_events: WorkerEvent[];
};

function bucketFor(kind: string): "spawn" | "tool" | "other" {
  if (kind === "spawn:subagent") return "spawn";
  if (kind.startsWith("tool:")) return "tool";
  return "other";
}

function pushCapped(arr: WorkerEvent[], ev: WorkerEvent): WorkerEvent[] {
  if (arr.some((x) => x.id === ev.id)) return arr; // dedupe
  const next = [...arr, ev];
  return next.length > MAX_EVENTS_PER_RUN
    ? next.slice(next.length - MAX_EVENTS_PER_RUN)
    : next;
}

// Add one event into a cloned RunEvents map (mutates the clone's entry).
function ingestIntoRun(map: Map<number, RunEvents>, ev: WorkerEvent): void {
  if (ev.run_id == null) return;
  const cur = map.get(ev.run_id) || {
    spawnEvents: [],
    toolEvents: [],
    otherEvents: [],
  };
  const next: RunEvents = {
    spawnEvents: cur.spawnEvents,
    toolEvents: cur.toolEvents,
    otherEvents: cur.otherEvents,
    latest: cur.latest,
  };
  const b = bucketFor(ev.kind);
  if (b === "spawn") next.spawnEvents = pushCapped(cur.spawnEvents, ev);
  else if (b === "tool") next.toolEvents = pushCapped(cur.toolEvents, ev);
  else next.otherEvents = pushCapped(cur.otherEvents, ev);
  if (!next.latest || next.latest.seq <= ev.seq) next.latest = ev;
  map.set(ev.run_id, next);
}

function ingestIntoPhase(map: Map<number, PhaseEvents>, ev: WorkerEvent): void {
  if (ev.phase_id == null) return;
  const cur = map.get(ev.phase_id) || { events: [] };
  const events = pushCapped(cur.events, ev);
  let latest = cur.latest;
  if (!latest || latest.seq <= ev.seq) latest = ev;
  map.set(ev.phase_id, { events, latest });
}

export function useLiveTreeState(projectId: number): LiveTreeState {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [phases, setPhases] = useState<Phase[]>([]);
  const [byRun, setByRun] = useState<Map<number, RunEvents>>(new Map());
  const [byPhase, setByPhase] = useState<Map<number, PhaseEvents>>(new Map());
  const [connected, setConnected] = useState(false);
  const [lastEventTs, setLastEventTs] = useState("");

  const pendingRef = useRef<WorkerEvent[]>([]);
  const flushTimerRef = useRef<number | null>(null);

  const flushPending = useCallback(() => {
    flushTimerRef.current = null;
    const batch = pendingRef.current;
    if (batch.length === 0) return;
    pendingRef.current = [];

    setByRun((m) => {
      const n = new Map(m);
      for (const ev of batch) ingestIntoRun(n, ev);
      return n;
    });
    setByPhase((m) => {
      const n = new Map(m);
      for (const ev of batch) ingestIntoPhase(n, ev);
      return n;
    });
    const last = batch[batch.length - 1];
    if (last) setLastEventTs(last.ts);
  }, []);

  const ingestEvent = useCallback(
    (ev: WorkerEvent) => {
      pendingRef.current.push(ev);
      if (flushTimerRef.current == null) {
        flushTimerRef.current = window.setTimeout(flushPending, 250);
      }
    },
    [flushPending],
  );

  // Drain pending events on unmount.
  useEffect(
    () => () => {
      if (flushTimerRef.current != null) {
        clearTimeout(flushTimerRef.current);
        flushPending();
      }
    },
    [flushPending],
  );

  useEffect(() => {
    if (!projectId) return;
    const ac = connectStream(
      `/api/projects/${projectId}/stream`,
      (e) => {
        setConnected(true);
        if (e.event === "snapshot") {
          const s = e.data as Snapshot;
          setTasks(s.tasks || []);
          setRuns(s.runs || []);
          setPhases(s.phases || []);
          for (const ev of s.recent_events || []) ingestEvent(ev);
        } else if (e.event === "task_state") {
          const t = e.data as Task & { task_id: number };
          setTasks((prev) => {
            const i = prev.findIndex((x) => x.id === t.task_id);
            const next: Task = {
              id: t.task_id,
              task_number: t.task_number,
              title: t.title,
              status: t.status,
            };
            if (i === -1) return [...prev, next];
            const c = prev.slice();
            c[i] = next;
            return c;
          });
          // Refresh runs from the embedded payload, if present.
          const incomingRuns = (e.data as any).runs;
          if (incomingRuns) {
            setRuns((prev) => {
              const others = prev.filter((r) => r.task_id !== t.task_id);
              const incoming: Run[] = (incomingRuns || [])
                .map((r: any) => ({
                  id: r.id,
                  task_id: t.task_id,
                  status: r.status,
                  worker: r.worker,
                  tmux_pane: r.pane,
                  worktree_path: r.worktree,
                }))
                .filter((r: Run) => r.id != null);
              return [...others, ...incoming];
            });
          }
        } else if (e.event === "phase_state") {
          const p = e.data as Phase;
          setPhases((prev) => {
            const i = prev.findIndex((x) => x.id === p.id);
            if (i === -1) return [...prev, p];
            const c = prev.slice();
            c[i] = p;
            return c;
          });
        } else if (e.event === "worker_event") {
          ingestEvent(e.data as WorkerEvent);
        }
      },
      (err) => {
        console.warn("live-tree stream error", err);
        setConnected(false);
      },
    );
    return () => ac.abort();
  }, [projectId, ingestEvent]);

  return { projectId, tasks, runs, phases, byRun, byPhase, connected, lastEventTs };
}
