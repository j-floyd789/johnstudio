// Team-mode task page: shows the planner's status, the plan once it's
// written, an Approve & Run button, and live status of every specialist.
// SSE stream gives us free updates because each specialist registers a
// `runs` row so the existing /api/projects/:id/stream feed picks them up.

import React from "react";
import { useParams, Link } from "react-router-dom";
import {
  cancelTeam,
  taskDiffs,
  teamActivity,
  teamApprove,
  teamBudget,
  teamCost,
  teamPlanCritic,
  teamPlanCritique,
  teamPlanRaw,
  teamProgress,
  teamStatus,
  type TaskDiffEntry,
  type TeamActivityEvent,
  type TeamCost,
  type TeamPlanDoc,
  type TeamProgress,
  type TeamState,
} from "../api/client";
import { connectStream } from "../lib/stream";
import { Badge, Button, Card, CodeBlock, Modal, useToast } from "../components/ui";
import { Markdown } from "../components/Markdown";

// A live status is one where the task still has killable specialists.
const LIVE_STATUSES = new Set(["planning", "running", "reviewing"]);

const VP_BADGE: Record<string, "accent" | "neutral" | "warn"> = {
  claude_vp: "accent",
  codex_vp: "warn",
  gemini_vp: "neutral",
};

const VP_LABEL: Record<string, string> = {
  claude_vp: "Claude VP (Engineering)",
  codex_vp: "Codex VP (Quality)",
  gemini_vp: "Gemini VP (Research)",
};

export function TeamPage() {
  const { id, n } = useParams<{ id: string; n: string }>();
  const pid = Number(id);
  const tn = Number(n);
  const toast = useToast();

  const [state, setState] = React.useState<TeamState | null>(null);
  const [rawPlan, setRawPlan] = React.useState<string>("");
  const [approving, setApproving] = React.useState(false);
  const [critique, setCritique] = React.useState<string>("");
  const [critiqueTriggered, setCritiqueTriggered] = React.useState(false);
  const [budget, setBudget] = React.useState<{ cost_usd: number; budget_usd: number | null; over_budget: boolean } | null>(null);
  const [progress, setProgress] = React.useState<TeamProgress | null>(null);
  const [activity, setActivity] = React.useState<TeamActivityEvent[]>([]);
  const [cost, setCost] = React.useState<TeamCost | null>(null);
  const [cancelOpen, setCancelOpen] = React.useState(false);
  const [cancelling, setCancelling] = React.useState(false);
  const [diffsOpen, setDiffsOpen] = React.useState(false);
  // Item 16 — live MCP-tool feed, fed from the project SSE `hook_event` frames.
  const [hookFeed, setHookFeed] = React.useState<HookFeedItem[]>([]);
  const hookSeqRef = React.useRef(0);

  const poll = React.useCallback(async () => {
    try {
      const s = await teamStatus(pid, tn);
      setState(s);
      if (s.plan_exists && !rawPlan) {
        try {
          const r = await teamPlanRaw(pid, tn);
          setRawPlan(r.content);
        } catch {
          /* not ready yet */
        }
      }
      // Auto-trigger the plan-critic exactly once when the plan first
      // appears. The user sees both the plan AND the critique before
      // approving — Anthropic's Evaluator-Optimizer pattern applied to
      // plan quality.
      if (s.plan_exists && !critiqueTriggered) {
        setCritiqueTriggered(true);
        teamPlanCritic(pid, tn).catch(() => { /* tolerated */ });
      }
      // Poll the critique file (it lands under task folder); we just
      // hit the raw-plan endpoint for the markdown payload — the
      // critique is a separate file so we fetch it through a similar
      // path. For v0, look for it in the task folder via the existing
      // plan endpoint shape — the team_orchestrator writes it next to
      // TEAM_PLAN.md.
      // The simplest path: include PLAN_CRITIQUE.md content via a
      // future endpoint. For now we just signal that the critic ran.
      try {
        const b = await teamBudget(pid, tn);
        setBudget(b);
      } catch {
        /* tolerated */
      }
      try {
        const pg = await teamProgress(pid, tn);
        setProgress(pg);
      } catch {
        /* tolerated */
      }
      try {
        const ac = await teamActivity(pid, tn, 40);
        setActivity(ac.events);
      } catch {
        /* tolerated */
      }
      // Item 13 — cost meter. Polled here every ~2s; also bumped early by
      // the cost.threshold_crossed hook_event in the SSE effect below.
      try {
        const c = await teamCost(pid, tn);
        setCost(c);
      } catch {
        /* tolerated */
      }
      // Pick up the critique once the plan-critic worker writes it.
      if (s.plan_exists && !critique) {
        try {
          const c = await teamPlanCritique(pid, tn);
          if (c.exists && c.content) setCritique(c.content);
        } catch {
          /* not ready yet */
        }
      }
    } catch (e: any) {
      console.warn("team status poll failed", e);
    }
  }, [pid, tn, rawPlan, critiqueTriggered]);

  React.useEffect(() => {
    poll();
    const t = setInterval(poll, 2000);
    return () => clearInterval(t);
  }, [poll]);

  // Item 16 — subscribe to the project SSE stream and keep only the
  // hook-bus frames (event === "hook_event"). We surface MCP tool calls
  // primarily, plus worker.exited / cost.threshold_crossed as context
  // lines. A cost.threshold_crossed also triggers an immediate cost
  // refetch so the meter (item 13) updates without waiting for the poll.
  //
  // NOTE: GraphPage owns its own EventSource on a different route/page, so
  // there's no shared subscription to hook into here — this is the single
  // stream this page opens, and it's the right place for the team feed.
  React.useEffect(() => {
    if (!pid) return;
    const ac = connectStream(
      `/api/projects/${pid}/stream`,
      (e) => {
        if (e.event !== "hook_event") return;
        const rec = e.data as { ts?: string; event?: string; payload?: any };
        const event = rec?.event || "";
        if (
          event !== "mcp.tool_called" &&
          event !== "worker.exited" &&
          event !== "cost.threshold_crossed"
        ) {
          return;
        }
        if (event === "cost.threshold_crossed") {
          teamCost(pid, tn).then(setCost).catch(() => { /* tolerated */ });
        }
        const item: HookFeedItem = {
          id: `${rec?.ts || ""}-${hookSeqRef.current++}`,
          ts: rec?.ts || "",
          event,
          summary: summarizeHook(event, rec?.payload || {}),
        };
        setHookFeed((prev) => {
          const next = [item, ...prev];
          return next.length > 60 ? next.slice(0, 60) : next;
        });
      },
      (err) => console.warn("team hook-feed stream error", err),
    );
    return () => ac.abort();
  }, [pid, tn]);

  async function onCancel() {
    setCancelling(true);
    try {
      const out = await cancelTeam(pid, tn);
      toast.push(
        out.count > 0 ? "ok" : "warn",
        out.count > 0
          ? `Cancelled ${out.count} specialist${out.count === 1 ? "" : "s"}.`
          : "No live specialists to cancel.",
      );
      setCancelOpen(false);
      await poll();
    } catch (e: any) {
      toast.push("bad", `Cancel failed: ${e.detail || e.message}`);
    } finally {
      setCancelling(false);
    }
  }

  async function onApprove() {
    setApproving(true);
    try {
      const out = await teamApprove(pid, tn);
      toast.push("ok", `Spawned ${out.launched.length} specialists.`);
      await poll();
    } catch (e: any) {
      toast.push("bad", `Approve failed: ${e.detail || e.message}`);
    } finally {
      setApproving(false);
    }
  }

  if (!state) {
    return (
      <div className="p-6">
        <Card>loading team task {tn}…</Card>
      </div>
    );
  }

  const status = state.status;
  const plan = state.plan as TeamPlanDoc | undefined;

  return (
    <div className="p-6 max-w-5xl mx-auto">
      <div className="mb-4 flex items-start justify-between gap-4">
        <div>
          <div className="text-xs text-ink-3">team task</div>
          <h1 className="text-2xl font-semibold tracking-tight">Task {tn}</h1>
          <div className="text-xs text-ink-2 mt-1">
            project <code className="bg-bg-1 px-1 py-0.5 rounded">{state.project_name}</code>
            {" · "}status <Badge tone={statusTone(status)}>{status}</Badge>
            {budget && (
              <>
                {" · "}cost <span className={budget.over_budget ? "text-bad" : "text-ink-2"}>
                  ${budget.cost_usd.toFixed(4)}
                  {budget.budget_usd != null && ` / $${budget.budget_usd.toFixed(2)}`}
                </span>
              </>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button onClick={() => setDiffsOpen(true)}>View diffs</Button>
          {LIVE_STATUSES.has(status) && (
            <Button variant="danger" onClick={() => setCancelOpen(true)}>
              Cancel
            </Button>
          )}
          <a
            href={`/p/${pid}/graph`}
            className="text-xs px-3 py-2 rounded border border-bg-2 bg-bg-1 hover:bg-bg-2 transition-colors"
          >
            ● Live tree
          </a>
        </div>
      </div>

      {cost && <CostMeter cost={cost} />}

      {progress && <ProgressPanel progress={progress} />}

      {/* Planning state: waiting for Gemini to produce TEAM_PLAN.md */}
      {status === "planning" && !state.plan_exists && (
        <Card>
          <div className="font-medium mb-1">Planner is thinking…</div>
          <div className="text-sm text-ink-2">
            Gemini's <code>lead-planner</code> is reading the task + project memory and writing
            <code className="ml-1">TEAM_PLAN.md</code>. Typically 30–60 seconds.
          </div>
        </Card>
      )}

      {/* Plan is ready; show it + Approve button */}
      {status === "planning" && state.plan_exists && plan && (
        <>
          <Card className="mb-4 border-accent/40">
            <div className="font-medium mb-2">Plan ready — review then approve</div>
            <div className="text-sm text-ink-2 mb-3">{plan.summary}</div>
            <div className="space-y-3">
              {(["claude_vp", "codex_vp", "gemini_vp"] as const).map((vp) => {
                const rows = plan.assignments.filter((a) => a.vp === vp);
                if (rows.length === 0) return (
                  <div key={vp} className="text-xs text-ink-3">
                    <Badge tone={VP_BADGE[vp]}>{VP_LABEL[vp]}</Badge>
                    <span className="ml-2">no assignments</span>
                  </div>
                );
                return (
                  <div key={vp}>
                    <Badge tone={VP_BADGE[vp]}>{VP_LABEL[vp]}</Badge>
                    <ul className="mt-2 ml-2 space-y-1">
                      {rows.map((a, i) => (
                        <li key={i} className="text-sm">
                          <code className="bg-bg-1 px-1 py-0.5 rounded">{a.role}</code>
                          <span className="text-ink-2"> — {a.brief}</span>
                          <span className="text-xs text-ink-3"> → {a.output}</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                );
              })}
            </div>
            {plan.cross_review?.length > 0 && (
              <div className="mt-3 pt-3 border-t border-bg-2 text-xs text-ink-2">
                <strong>Cross-VP review:</strong>{" "}
                {plan.cross_review.map((c, i) => (
                  <span key={i}>
                    {c.reviewer} reads {c.reads.join(", ")}
                    {i < plan.cross_review.length - 1 ? "; " : ""}
                  </span>
                ))}
              </div>
            )}
            {plan.acceptance_criteria?.length > 0 && (
              <div className="mt-3 pt-3 border-t border-bg-2">
                <div className="text-xs text-ink-2 font-medium mb-1">Acceptance criteria</div>
                <ul className="text-xs text-ink-2 list-disc pl-4">
                  {plan.acceptance_criteria.map((c, i) => (
                    <li key={i}>{c}</li>
                  ))}
                </ul>
              </div>
            )}
            <div className="mt-4 flex gap-2">
              <Button variant="primary" onClick={onApprove} disabled={approving}>
                {approving ? "Spawning…" : `Approve & spawn ${plan.assignments.length} specialists`}
              </Button>
            </div>
          </Card>
          {critique && (
            <Card className="mb-4 border-warn/40">
              <div className="text-xs text-ink-3 mb-2">Plan critique (auto-spawned product-manager)</div>
              <Markdown text={critique} />
            </Card>
          )}
          {!critique && critiqueTriggered && (
            <div className="mb-4 text-xs text-ink-3 italic">
              Plan critic is running — its findings will appear here.
            </div>
          )}
          {rawPlan && (
            <details className="mb-4">
              <summary className="text-xs text-ink-3 cursor-pointer hover:text-ink-2">
                Show raw TEAM_PLAN.md
              </summary>
              <Card className="mt-2">
                <Markdown text={rawPlan} />
              </Card>
            </details>
          )}
        </>
      )}

      {/* Running: show assignments + their live status */}
      {(status === "running" || status === "reviewing" || status === "merged") && state.assignments && (
        <Card>
          <div className="font-medium mb-3">Specialists ({state.assignments.length})</div>
          <div className="space-y-2">
            {(["claude_vp", "codex_vp", "gemini_vp"] as const).map((vp) => {
              const rows = state.assignments!.filter((a) => a.vp === vp);
              if (rows.length === 0) return null;
              return (
                <div key={vp} className="pb-2">
                  <Badge tone={VP_BADGE[vp]}>{VP_LABEL[vp]}</Badge>
                  <ul className="mt-2 ml-2 space-y-1">
                    {rows.map((a) => (
                      <li key={a.run_id} className="text-sm flex items-center gap-2">
                        <code className="bg-bg-1 px-1 py-0.5 rounded text-xs">{a.role}</code>
                        <span className="text-ink-2 truncate">{a.brief}</span>
                        <span className="text-xs text-ink-3 ml-auto">→ {a.output}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              );
            })}
          </div>
          <div className="mt-3 text-xs text-ink-3">
            Watch the live tree for real-time progress.
          </div>
        </Card>
      )}

      {hookFeed.length > 0 && <McpFeed items={hookFeed} />}

      {activity.length > 0 && <ActivityFeed events={activity} />}

      <Modal
        open={cancelOpen}
        onClose={() => setCancelOpen(false)}
        title="Cancel this team task?"
        footer={
          <>
            <Button onClick={() => setCancelOpen(false)}>Keep running</Button>
            <Button variant="danger" onClick={onCancel} disabled={cancelling}>
              {cancelling ? "Cancelling…" : "Cancel specialists"}
            </Button>
          </>
        }
      >
        <div className="space-y-2">
          <div>
            This kills every live specialist subprocess for task {tn}, marks
            their runs <code className="px-1 bg-bg-1 rounded">stopped</code>, and
            marks the task <code className="px-1 bg-bg-1 rounded">cancelled</code>.
          </div>
          <div className="text-xs text-ink-3">
            Idempotent — already-finished work is left untouched. Worktrees are
            not removed (use cleanup for that).
          </div>
        </div>
      </Modal>

      {diffsOpen && (
        <DiffViewer pid={pid} tn={tn} onClose={() => setDiffsOpen(false)} />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Item 13 — per-task cost / token meter
// ---------------------------------------------------------------------------

function CostMeter({ cost }: { cost: TeamCost }) {
  return (
    <Card className="mb-4">
      <div className="flex items-center justify-between mb-2">
        <div className="text-sm font-medium">
          Cost
          <span className="ml-2 text-xs text-ink-3">
            {cost.workers.length} worker{cost.workers.length === 1 ? "" : "s"}
          </span>
        </div>
        <div className="text-sm font-mono">${cost.total_cost_usd.toFixed(4)}</div>
      </div>
      {cost.workers.length === 0 ? (
        <div className="text-xs text-ink-3">No per-worker cost recorded yet.</div>
      ) : (
        <table className="w-full text-xs">
          <thead className="text-left text-ink-3 border-b border-bg-2">
            <tr>
              <th className="py-1 pr-2">worker</th>
              <th className="py-1 pr-2">role</th>
              <th className="py-1 pr-2">status</th>
              <th className="py-1 pr-2 text-right">$</th>
              <th className="py-1 text-right">tokens</th>
            </tr>
          </thead>
          <tbody>
            {cost.workers.map((w) => (
              <tr key={w.run_id} className="border-b border-bg-2/40">
                <td className="py-1 pr-2 font-mono">{w.worker || `run ${w.run_id}`}</td>
                <td className="py-1 pr-2 text-ink-2">{w.role || "—"}</td>
                <td className="py-1 pr-2">
                  <Badge tone={runStatusTone(w.status)}>{w.status}</Badge>
                </td>
                <td className="py-1 pr-2 text-right font-mono">${w.cost_usd.toFixed(4)}</td>
                {/* tokens_available is false in the current schema → always "—" */}
                <td className="py-1 text-right text-ink-3 font-mono">
                  {w.tokens != null ? w.tokens : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Item 16 — live MCP-tool-invocation feed (from SSE `hook_event` frames)
// ---------------------------------------------------------------------------

type HookFeedItem = {
  id: string;
  ts: string;
  event: string;
  summary: string;
};

function summarizeHook(event: string, payload: any): string {
  if (event === "mcp.tool_called") {
    const tool = payload?.tool || "tool";
    const args = payload?.args_summary;
    return args ? `${tool} — ${args}` : String(tool);
  }
  if (event === "worker.exited") {
    const worker = payload?.worker || `run ${payload?.run_id ?? "?"}`;
    const cause = payload?.cause ? ` (${payload.cause})` : "";
    return `${worker} exited${cause}`;
  }
  if (event === "cost.threshold_crossed") {
    const band = payload?.band != null ? `band ${payload.band}` : "threshold";
    const total = payload?.total_cost_usd;
    return `cost crossed ${band}${total != null ? ` — $${Number(total).toFixed(2)}` : ""}`;
  }
  return JSON.stringify(payload).slice(0, 120);
}

function hookTone(event: string): "accent" | "ok" | "warn" | "bad" | "neutral" {
  if (event === "mcp.tool_called") return "accent";
  if (event === "worker.exited") return "neutral";
  if (event === "cost.threshold_crossed") return "warn";
  return "neutral";
}

function McpFeed({ items }: { items: HookFeedItem[] }) {
  return (
    <Card className="mt-4">
      <div className="flex items-center justify-between mb-2">
        <div className="text-sm font-medium">MCP tool feed</div>
        <div className="text-xs text-ink-3">live · {items.length}</div>
      </div>
      <ul className="text-xs space-y-1 max-h-64 overflow-y-auto font-mono">
        {items.map((it) => (
          <li key={it.id} className="flex items-start gap-2">
            <span className="text-ink-3 shrink-0">{formatTs(it.ts)}</span>
            <span className="shrink-0 w-44">
              <Badge tone={hookTone(it.event)}>{it.event}</Badge>
            </span>
            <span className="text-ink-2 truncate" title={it.summary}>{it.summary}</span>
          </li>
        ))}
      </ul>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Item 15 — per-worktree diff viewer (modal)
// ---------------------------------------------------------------------------

function DiffViewer({ pid, tn, onClose }: { pid: number; tn: number; onClose: () => void }) {
  const [diffs, setDiffs] = React.useState<TaskDiffEntry[] | null>(null);
  const [err, setErr] = React.useState<string>("");
  const [sel, setSel] = React.useState<string | null>(null);

  React.useEffect(() => {
    taskDiffs(pid, tn, true)
      .then((r) => {
        setDiffs(r.diffs);
        setSel(r.diffs[0]?.worker || null);
      })
      .catch((e) => setErr(e?.detail || e?.message || String(e)));
  }, [pid, tn]);

  const cur = diffs?.find((d) => d.worker === sel) || diffs?.[0] || null;

  return (
    <div
      className="fixed inset-0 z-50 grid place-items-center bg-black/60 p-4"
      onClick={onClose}
    >
      <div
        className="card w-full"
        style={{ maxWidth: "min(1100px, 94vw)", maxHeight: "88vh", display: "flex", flexDirection: "column" }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-base font-semibold">Diffs · task {tn}</h3>
          <button onClick={onClose} className="text-ink-3 hover:text-ink-0">✕</button>
        </div>
        {err && <div className="text-sm text-bad">Couldn't load diffs: {err}</div>}
        {!err && !diffs && <div className="text-sm text-ink-3">loading…</div>}
        {diffs && diffs.length === 0 && (
          <div className="text-sm text-ink-3">
            No diffs recorded yet. Run <code className="px-1 bg-bg-1 rounded">collect</code> first.
          </div>
        )}
        {cur && (
          <div className="grid gap-3 min-h-0 flex-1" style={{ gridTemplateColumns: "200px 1fr" }}>
            <div className="overflow-y-auto">
              {diffs!.map((d) => (
                <button
                  key={d.worker}
                  onClick={() => setSel(d.worker)}
                  className={`block w-full text-left text-xs px-3 py-1.5 rounded hover:bg-bg-3 ${
                    cur.worker === d.worker ? "bg-bg-3 text-ink-0" : "text-ink-1"
                  }`}
                >
                  <span className="font-mono">{d.worker}</span>{" "}
                  <span className="text-ink-3">({d.files_changed.length})</span>
                </button>
              ))}
            </div>
            <div className="min-h-0 overflow-y-auto">
              {cur.stat?.stat && (
                <div className="text-xs text-ink-2 font-mono mb-2 whitespace-pre-wrap">
                  {cur.stat.stat}
                </div>
              )}
              {cur.files_changed.length > 0 && (
                <div className="text-xs text-ink-3 mb-2">
                  {cur.files_changed.join(", ")}
                </div>
              )}
              {cur.truncated && (
                <div className="text-xs text-warn mb-2">diff truncated (large)</div>
              )}
              {cur.diff_text ? (
                <DiffText text={cur.diff_text} />
              ) : (
                <div className="text-xs text-ink-3">{cur.error || "No diff text."}</div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// Colorize a unified diff: + lines green, - lines red, @@ hunks accent.
function DiffText({ text }: { text: string }) {
  const lines = text.split("\n");
  return (
    <pre className="card bg-bg-1 overflow-x-auto text-xs leading-relaxed whitespace-pre m-0">
      <code>
        {lines.map((ln, i) => {
          let cls = "text-ink-2";
          if (ln.startsWith("+") && !ln.startsWith("+++")) cls = "text-ok";
          else if (ln.startsWith("-") && !ln.startsWith("---")) cls = "text-bad";
          else if (ln.startsWith("@@")) cls = "text-accent";
          else if (ln.startsWith("diff ") || ln.startsWith("index ") || ln.startsWith("+++") || ln.startsWith("---"))
            cls = "text-ink-3";
          return (
            <div key={i} className={cls}>
              {ln || " "}
            </div>
          );
        })}
      </code>
    </pre>
  );
}

function ProgressPanel({ progress }: { progress: TeamProgress }) {
  const tone =
    progress.score >= 100 ? "ok" :
    progress.stuck_count > 0 ? "warn" :
    "accent";
  const barColor =
    tone === "ok" ? "bg-ok" :
    tone === "warn" ? "bg-warn" :
    "bg-accent";
  return (
    <Card className="mb-4">
      <div className="flex items-center justify-between mb-2">
        <div className="text-sm font-medium">
          Progress
          <span className="ml-2 text-xs text-ink-3">phase: <code>{progress.phase}</code></span>
          {progress.test_round > 0 && (
            <span className="ml-2 text-xs text-ink-3">test loop #{progress.test_round}</span>
          )}
          {progress.revise_round > 0 && (
            <span className="ml-2 text-xs text-ink-3">revise round #{progress.revise_round}</span>
          )}
        </div>
        <div className="text-sm font-mono">{progress.score}%</div>
      </div>
      <div className="h-2 w-full rounded bg-bg-2 overflow-hidden">
        <div
          className={`h-2 ${barColor} transition-all duration-300`}
          style={{ width: `${progress.score}%` }}
        />
      </div>
      <div className="mt-2 flex flex-wrap gap-3 text-xs text-ink-2">
        {progress.total > 0 && (
          <span>specialists: {progress.done}/{progress.total} done</span>
        )}
        {progress.in_flight > 0 && <span>{progress.in_flight} in flight</span>}
        {progress.stuck_count > 0 && (
          <Badge tone="bad">
            {progress.stuck_count} stuck (≥{Math.round((progress.stuck[0]?.idle_seconds ?? 0) / 60)}m idle)
          </Badge>
        )}
        {progress.last_event_ts && (
          <span className="text-ink-3 ml-auto">last event: {formatTs(progress.last_event_ts)}</span>
        )}
      </div>
      {progress.stuck_count > 0 && (
        <div className="mt-3 pt-3 border-t border-bg-2">
          <div className="text-xs text-ink-3 mb-1">Stuck runs</div>
          <ul className="text-xs space-y-1">
            {progress.stuck.map((s) => (
              <li key={s.run_id} className="flex items-center gap-2">
                <Badge tone="bad">stuck</Badge>
                <code className="bg-bg-1 px-1 py-0.5 rounded">{s.role || s.worker_name}</code>
                <span className="text-ink-2">
                  idle {Math.floor(s.idle_seconds / 60)}m {s.idle_seconds % 60}s
                  {" · "}{s.event_count} events
                  {" · "}status {s.status}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  );
}

function ActivityFeed({ events }: { events: TeamActivityEvent[] }) {
  return (
    <Card className="mt-4">
      <div className="flex items-center justify-between mb-2">
        <div className="text-sm font-medium">Activity</div>
        <div className="text-xs text-ink-3">{events.length} events</div>
      </div>
      <ul className="text-xs space-y-1 max-h-64 overflow-y-auto font-mono">
        {events.map((e) => (
          <li key={e.id} className="flex items-start gap-2">
            <span className="text-ink-3 shrink-0">{formatTs(e.ts)}</span>
            <span className="text-ink-2 shrink-0 w-32 truncate" title={e.role || e.worker_name}>
              {e.role || e.worker_name}
            </span>
            <span className="shrink-0 w-28">
              <Badge tone={eventTone(e.kind)}>{e.kind}</Badge>
            </span>
            <span className="text-ink-2 truncate" title={e.summary}>{e.summary}</span>
          </li>
        ))}
      </ul>
    </Card>
  );
}

function formatTs(ts: string | null): string {
  if (!ts) return "—";
  // Accept both "2026-05-29T12:04:18" and "2026-05-29T12:04:18.123"
  const cleaned = ts.replace("Z", "");
  const d = new Date(cleaned + (cleaned.includes("Z") ? "" : "Z"));
  if (Number.isNaN(d.getTime())) return ts;
  return d.toLocaleTimeString();
}

function runStatusTone(status: string): "accent" | "ok" | "warn" | "bad" | "neutral" {
  switch (status) {
    case "done":
    case "completed":
    case "merged":
      return "ok";
    case "running":
    case "launched":
    case "retrying":
      return "accent";
    case "failed":
    case "error":
      return "bad";
    case "stopped":
    case "cancelled":
      return "warn";
    default:
      return "neutral";
  }
}

function eventTone(kind: string): "accent" | "ok" | "warn" | "bad" | "neutral" {
  if (kind.startsWith("error")) return "bad";
  if (kind.startsWith("result")) return "ok";
  if (kind.startsWith("system")) return "neutral";
  if (kind.startsWith("assistant")) return "accent";
  if (kind.startsWith("tool")) return "warn";
  return "neutral";
}

function statusTone(s: string): "accent" | "ok" | "warn" | "bad" | "neutral" {
  switch (s) {
    case "planning":
      return "warn";
    case "running":
    case "reviewing":
      return "accent";
    case "merged":
      return "ok";
    case "rejected":
      return "bad";
    default:
      return "neutral";
  }
}
