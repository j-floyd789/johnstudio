import React from "react";
import { useParams } from "react-router-dom";
import {
  chainAdvance,
  chainApproveRfc,
  chainArtifact,
  chainMerge,
  chainReject,
  chainRejectRfc,
  chainStatus,
} from "../api/client";
import type { ChainPhase, ChainStatus } from "../api/client";
import { Badge, Button, Card, CodeBlock, Empty, Input, Modal, useToast } from "../components/ui";
import { Markdown } from "../components/Markdown";

const PHASE_ORDER = [
  "rfc_drafting",
  "rfc_review",
  "rfc_pending_approval",
  "implementing",
  "reviewing",
  "revising",
  "pending_merge",
  "conflict",
  "merged",
  "rejected",
];

const HUMAN_GATES = new Set([
  "rfc_pending_approval",
  "pending_merge",
  "conflict",
]);

function phaseTone(phase: string): "neutral" | "accent" | "ok" | "warn" | "bad" {
  if (phase === "merged") return "ok";
  if (phase === "rejected" || phase === "conflict") return "bad";
  if (HUMAN_GATES.has(phase)) return "warn";
  if (phase === "implementing" || phase === "reviewing" || phase === "revising") return "accent";
  return "neutral";
}

export function ChainPage() {
  const { id, n } = useParams();
  const pid = Number(id);
  const tn = Number(n);
  const toast = useToast();
  const [status, setStatus] = React.useState<ChainStatus | null>(null);
  const [artifacts, setArtifacts] = React.useState<Record<string, string>>({});
  const [mergeOpen, setMergeOpen] = React.useState(false);
  const [rejectOpen, setRejectOpen] = React.useState(false);
  const [rejectReason, setRejectReason] = React.useState("");

  async function refresh() {
    try {
      const s = await chainStatus(pid, tn);
      setStatus(s);
      // Pull artifacts that exist
      const wanted = ["rfc", "rfc_review", "result"];
      // Add review_<n> for every reviewing phase
      const reviewRounds = s.phases.filter((p) => p.phase === "reviewing").map((p) => p.round);
      for (const r of reviewRounds) wanted.push(`review_${r}`);
      const out: Record<string, string> = {};
      for (const kind of wanted) {
        try {
          const a = await chainArtifact(pid, tn, kind);
          if (a.exists) out[kind] = a.content;
        } catch {/* ignore */}
      }
      setArtifacts(out);
    } catch (e: any) {
      toast.push("bad", `Reload failed: ${e.detail || e.message}`);
    }
  }
  React.useEffect(() => {
    refresh();
    const t = setInterval(refresh, 3000);
    return () => clearInterval(t);
  }, [pid, tn]);

  async function onAdvance() {
    try {
      await chainAdvance(pid, tn);
      await refresh();
    } catch (e: any) {
      toast.push("bad", `Advance failed: ${e.detail || e.message}`);
    }
  }

  async function onApproveRfc() {
    try {
      await chainApproveRfc(pid, tn);
      toast.push("ok", "RFC approved. Implementer launched.");
      refresh();
    } catch (e: any) {
      toast.push("bad", `Approve failed: ${e.detail || e.message}`);
    }
  }

  async function onRejectRfc() {
    try {
      await chainRejectRfc(pid, tn, rejectReason);
      toast.push("ok", "RFC rejected.");
      setRejectOpen(false);
      refresh();
    } catch (e: any) {
      toast.push("bad", `Reject failed: ${e.detail || e.message}`);
    }
  }

  async function onMerge() {
    try {
      const out = await chainMerge(pid, tn, true);
      if (out.merged) {
        toast.push("ok", `Merged ${out.branch}. tests_passed=${out.tests_passed}`);
        setMergeOpen(false);
        refresh();
      } else {
        toast.push("warn", `Merge incomplete: ${out.note || out.output || ""}`);
      }
    } catch (e: any) {
      toast.push("bad", `Merge failed: ${e.detail || e.message}`);
    }
  }

  if (!status) {
    return (
      <div className="p-8">
        <Card>loading chain…</Card>
      </div>
    );
  }

  const cur = status.current;
  const isAtRfcGate = cur?.phase === "rfc_pending_approval";
  const isAtMergeGate = cur?.phase === "pending_merge" || cur?.phase === "conflict";

  return (
    <div className="p-6">
      <div className="mb-4 flex items-center justify-between">
        <div>
          <div className="text-xs text-ink-3">chain task</div>
          <h1 className="text-2xl font-semibold tracking-tight">
            {String(status.task_number).padStart(4, "0")}
          </h1>
          {cur && (
            <div className="text-xs text-ink-2 mt-1">
              current: <Badge tone={phaseTone(cur.phase)}>{cur.phase}</Badge>{" "}
              round {cur.round}
              {cur.verdict && <> · verdict: <Badge tone={cur.verdict === "approve" ? "ok" : "warn"}>{cur.verdict}</Badge></>}
            </div>
          )}
        </div>
        <div className="flex gap-2">
          <Button onClick={onAdvance}>advance</Button>
          {isAtRfcGate && (
            <>
              <Button variant="primary" onClick={onApproveRfc}>approve RFC</Button>
              <Button variant="danger" onClick={() => setRejectOpen(true)}>reject RFC</Button>
            </>
          )}
          {isAtMergeGate && (
            <>
              <Button variant="primary" onClick={() => setMergeOpen(true)}>merge</Button>
              <Button variant="danger" onClick={() => setRejectOpen(true)}>reject</Button>
            </>
          )}
        </div>
      </div>

      {/* Timeline */}
      <Card className="mb-4">
        <div className="font-medium mb-2">Phase timeline</div>
        <div className="flex flex-wrap gap-1.5">
          {status.phases.map((p) => (
            <div
              key={p.id}
              className={`text-xs px-2 py-1 rounded border ${
                p.status === "completed"
                  ? "border-ok/40 bg-ok/5 text-ok"
                  : p.status === "running"
                    ? "border-accent/40 bg-accent/5 text-accent"
                    : "border-line text-ink-2"
              }`}
              title={p.notes || ""}
            >
              {p.phase}{p.round > 0 && ` r${p.round}`} {p.verdict && `· ${p.verdict}`}
            </div>
          ))}
        </div>
      </Card>

      {/* Artifacts */}
      <div className="grid gap-4">
        {artifacts["rfc"] && (
          <Card>
            <div className="flex items-center justify-between mb-2">
              <div className="font-medium">RFC.md</div>
              {isAtRfcGate && <Badge tone="warn">awaiting your approval</Badge>}
            </div>
            <Markdown text={artifacts["rfc"]} />
          </Card>
        )}
        {artifacts["rfc_review"] && (
          <Card>
            <div className="font-medium mb-2">RFC_REVIEW.md</div>
            <Markdown text={artifacts["rfc_review"]} />
          </Card>
        )}
        {Object.keys(artifacts)
          .filter((k) => k.startsWith("review_"))
          .sort()
          .map((k) => (
            <Card key={k}>
              <div className="font-medium mb-2">{k.toUpperCase()}.md</div>
              <Markdown text={artifacts[k]} />
            </Card>
          ))}
        {artifacts["result"] && (
          <Card>
            <div className="font-medium mb-2">Implementer RESULT.md (latest)</div>
            <Markdown text={artifacts["result"]} />
          </Card>
        )}
        {Object.keys(artifacts).length === 0 && (
          <Empty title="No artifacts yet." body="Phases will populate as workers complete." />
        )}
      </div>

      {/* Modals */}
      <Modal
        open={mergeOpen}
        onClose={() => setMergeOpen(false)}
        title={`Merge chain task ${tn}?`}
        footer={
          <>
            <Button onClick={() => setMergeOpen(false)}>Cancel</Button>
            <Button variant="primary" onClick={onMerge}>Confirm merge</Button>
          </>
        }
      >
        <div className="text-sm">
          This will checkout base, merge the chain branch
          <code className="px-1 mx-1 rounded bg-bg-1">ai/task-{String(tn).padStart(4, "0")}/chain</code>,
          and run configured tests. The merge cannot be undone via the UI.
        </div>
      </Modal>

      <Modal
        open={rejectOpen}
        onClose={() => setRejectOpen(false)}
        title="Reject?"
        footer={
          <>
            <Button onClick={() => setRejectOpen(false)}>Cancel</Button>
            <Button
              variant="danger"
              onClick={async () => {
                if (isAtRfcGate) {
                  await onRejectRfc();
                } else {
                  await chainReject(pid, tn, rejectReason);
                  toast.push("ok", "Chain rejected.");
                  setRejectOpen(false);
                  refresh();
                }
              }}
            >
              Reject
            </Button>
          </>
        }
      >
        <Input
          placeholder="Reason (optional)"
          value={rejectReason}
          onChange={(e) => setRejectReason(e.target.value)}
        />
      </Modal>
    </div>
  );
}
