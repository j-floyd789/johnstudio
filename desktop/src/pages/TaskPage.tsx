import React from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  cleanupTask,
  collectTask,
  getContextPacks,
  getDiffs,
  getLogs,
  getMergePlanMarkdown,
  getResults,
  getReviewMarkdown,
  getSafetyReport,
  getTask,
  mergeTask,
  resumeTask,
  reviewTask,
  stopTask,
} from "../api/client";
import type {
  ArtifactFile,
  ReviewResponse,
  SafetyReport,
  TaskStatus,
} from "../lib/types";
import {
  Badge,
  Button,
  Card,
  CodeBlock,
  Empty,
  Input,
  Modal,
  Tabs,
  useToast,
} from "../components/ui";
import { Markdown } from "../components/Markdown";

export function TaskPage() {
  const { id, n } = useParams();
  const pid = Number(id);
  const tn = Number(n);
  const toast = useToast();
  const navigate = useNavigate();

  const [task, setTask] = React.useState<TaskStatus | null>(null);
  const [prompts, setPrompts] = React.useState<ArtifactFile[]>([]);
  const [results, setResults] = React.useState<ArtifactFile[]>([]);
  const [diffs, setDiffs] = React.useState<ArtifactFile[]>([]);
  const [logs, setLogs] = React.useState<ArtifactFile[]>([]);
  const [review, setReview] = React.useState<ReviewResponse | null>(null);
  const [reviewMd, setReviewMd] = React.useState<string>("");
  const [mergePlanMd, setMergePlanMd] = React.useState<string>("");
  const [safety, setSafety] = React.useState<SafetyReport | null>(null);

  const [loadErr, setLoadErr] = React.useState<string | null>(null);
  const stopRef = React.useRef(false);

  const [tab, setTab] = React.useState("workers");
  const [mergeOpen, setMergeOpen] = React.useState(false);
  const [mergeWorker, setMergeWorker] = React.useState<string>("");
  const [mergeReason, setMergeReason] = React.useState("");

  async function refresh() {
    try {
      const t = await getTask(pid, tn);
      setTask(t);
      const [p, r, d, l, rv, mp] = await Promise.all([
        getContextPacks(pid, tn),
        getResults(pid, tn),
        getDiffs(pid, tn),
        getLogs(pid, tn),
        getReviewMarkdown(pid, tn),
        getMergePlanMarkdown(pid, tn),
      ]);
      setPrompts(p);
      setResults(r);
      setDiffs(d);
      setLogs(l);
      setReviewMd(rv.content);
      setMergePlanMd(mp.content);
      setLoadErr(null);
    } catch (e: any) {
      const msg = e.detail || e.message || "load failed";
      setLoadErr(msg);
      // A missing/deleted task or project is terminal — stop polling so we
      // don't spam error toasts + hammer the 404 endpoint forever.
      if (e.status === 404 || /not found/i.test(String(msg))) stopRef.current = true;
    }
  }
  React.useEffect(() => {
    stopRef.current = false;
    refresh();
    const t = setInterval(() => { if (!stopRef.current) refresh(); }, 4000);
    return () => clearInterval(t);
  }, [pid, tn]);

  // Team-mode tasks land in TEAM_STATE.json with status='planning' (or
  // running / reviewing / pending_merge). The parallel TaskPage has no
  // idea what to do with those — auto-redirect to the team page so the
  // user lands on the plan + approve button instead of an empty table.
  React.useEffect(() => {
    const status = task?.status;
    if (status && ["planning", "reviewing", "pending_merge"].includes(status)) {
      navigate(`/p/${pid}/team/${tn}`, { replace: true });
    }
  }, [task?.status, pid, tn, navigate]);

  async function onCollect() {
    try {
      const s = await collectTask(pid, tn);
      const flags = s.runs.flatMap((r: any) => [
        ...r.protected_path_hits.map((p: string) => `PROTECTED ${p}`),
        ...r.dangerous_command_hits.map((c: string) => `DANGEROUS ${c}`),
      ]);
      toast.push(flags.length ? "warn" : "ok", `Collected${flags.length ? `; flags: ${flags.join(", ")}` : ""}`);
      const sr = await getSafetyReport(pid, tn);
      setSafety(sr);
      refresh();
    } catch (e: any) {
      toast.push("bad", `Collect failed: ${e.detail || e.message}`);
    }
  }

  async function onReview() {
    try {
      const r = await reviewTask(pid, tn);
      setReview(r);
      toast.push("ok", `Reviewed. Recommended: ${r.recommended || "(none)"}`);
      refresh();
    } catch (e: any) {
      toast.push("bad", `Review failed: ${e.detail || e.message}`);
    }
  }

  async function onStop() {
    try {
      await stopTask(pid, tn);
      toast.push("ok", "Stopped");
      refresh();
    } catch (e: any) {
      toast.push("bad", `Stop failed: ${e.detail || e.message}`);
    }
  }

  async function onCleanup() {
    try {
      await cleanupTask(pid, tn, true);
      toast.push("ok", "Cleaned up worktrees");
      refresh();
    } catch (e: any) {
      toast.push("bad", `Cleanup failed: ${e.detail || e.message}`);
    }
  }

  async function onResume(worker: string) {
    try {
      const r = await resumeTask(pid, tn, worker);
      toast.push("ok", r.resumed ? `Re-nudged ${worker}` : "Prompt rewritten (no live session)");
    } catch (e: any) {
      toast.push("bad", `Resume failed: ${e.detail || e.message}`);
    }
  }

  async function doMerge(dryRun: boolean) {
    try {
      const out = await mergeTask(pid, tn, mergeWorker, !dryRun && true, dryRun);
      if (dryRun) {
        toast.push("ok", `Dry-run exit=${out.exit_code}`);
      } else if (out.merged) {
        toast.push("ok", `Merged ${out.branch}. tests_passed=${out.tests_passed}`);
        setMergeOpen(false);
        navigate(`/p/${pid}`);
      } else {
        toast.push("warn", `Merge incomplete: ${out.output || out.note || ""}`);
      }
    } catch (e: any) {
      toast.push("bad", `Merge failed: ${e.detail || e.message}`);
    }
  }

  if (!task) {
    return (
      <div className="p-8">
        <Card>{loadErr ? `Couldn't load task: ${loadErr}` : "loading task…"}</Card>
      </div>
    );
  }

  return (
    <div className="p-6">
      <div className="mb-4 flex items-center justify-between">
        <div>
          <div className="text-xs text-ink-3">task</div>
          <h1 className="text-2xl font-semibold tracking-tight">
            {String(task.task_number).padStart(4, "0")} — {task.title}
          </h1>
          <div className="text-xs text-ink-2 mt-1">
            status: <Badge tone={task.status === "merged" ? "ok" : "neutral"}>{task.status}</Badge>
          </div>
        </div>
        <div className="flex gap-2">
          <Button onClick={onCollect}>collect</Button>
          <Button onClick={onReview}>review</Button>
          <Button onClick={onStop}>stop</Button>
          <Button onClick={onCleanup}>cleanup</Button>
        </div>
      </div>

      <Tabs
        value={tab}
        onChange={setTab}
        items={[
          { value: "workers", label: "Workers" },
          { value: "prompts", label: "Context packs" },
          { value: "results", label: "Results" },
          { value: "diffs", label: "Diffs" },
          { value: "logs", label: "Logs" },
          { value: "review", label: "Review" },
          { value: "merge", label: "Merge" },
        ]}
      />

      <div className="mt-4">
        {tab === "workers" && (
          <Card className="p-0 overflow-hidden">
            <table className="w-full text-sm">
              <thead className="text-left text-ink-3 border-b border-line">
                <tr>
                  <th className="px-3 py-2">worker</th>
                  <th className="px-3 py-2">status</th>
                  <th className="px-3 py-2">branch</th>
                  <th className="px-3 py-2">worktree</th>
                  <th className="px-3 py-2">RESULT</th>
                  <th className="px-3 py-2">DONE</th>
                  <th className="px-3 py-2">actions</th>
                </tr>
              </thead>
              <tbody>
                {task.runs.map((r, i) => (
                  <tr key={`${r.worker}-${i}`} className="border-b border-line/40">
                    <td className="px-3 py-2 font-medium">{r.worker}</td>
                    <td className="px-3 py-2">
                      <Badge tone={r.status === "completed" ? "ok" : "neutral"}>{r.status}</Badge>
                    </td>
                    <td className="px-3 py-2">
                      <code className="text-xs">{r.branch || "—"}</code>
                    </td>
                    <td className="px-3 py-2">
                      <code className="text-xs text-ink-3 truncate max-w-[24ch] inline-block">{r.worktree || "—"}</code>
                    </td>
                    <td className="px-3 py-2">
                      <Badge tone={r.result_md_exists ? "ok" : "neutral"}>{r.result_md_exists ? "yes" : "no"}</Badge>
                    </td>
                    <td className="px-3 py-2">
                      <Badge tone={r.done_md_exists ? "ok" : "neutral"}>{r.done_md_exists ? "yes" : "no"}</Badge>
                    </td>
                    <td className="px-3 py-2">
                      <Button onClick={() => onResume(r.worker)}>resume</Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
        )}

        {tab === "prompts" && (
          <ArtifactList files={prompts} kind="context pack" />
        )}
        {tab === "results" && (
          <ArtifactList files={results} kind="result" markdown />
        )}
        {tab === "diffs" && (
          <ArtifactList files={diffs} kind="diff" />
        )}
        {tab === "logs" && (
          <ArtifactList files={logs} kind="log" />
        )}

        {tab === "review" && (
          <div className="space-y-3">
            <Card>
              <div className="flex items-center justify-between mb-2">
                <div className="font-medium">FINAL_REVIEW.md</div>
                <Button onClick={onReview}>re-run review</Button>
              </div>
              {reviewMd ? <Markdown text={reviewMd} /> : <div className="text-sm text-ink-3">Run review to generate.</div>}
            </Card>
            {review && (
              <Card className="p-0 overflow-hidden">
                <table className="w-full text-sm">
                  <thead className="text-left text-ink-3 border-b border-line">
                    <tr>
                      <th className="px-3 py-2">worker</th>
                      <th className="px-3 py-2">score</th>
                      <th className="px-3 py-2">flags</th>
                    </tr>
                  </thead>
                  <tbody>
                    {review.scores.map((s) => (
                      <tr key={s.worker_name} className="border-b border-line/40">
                        <td className="px-3 py-2 font-medium">{s.worker_name}</td>
                        <td className="px-3 py-2">{s.score}</td>
                        <td className="px-3 py-2">{s.flags.join(", ") || "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </Card>
            )}
            {safety && (safety.runs.some((r) => r.protected_path_hits.length || r.dangerous_command_hits.length)) && (
              <Card className="border-bad/40">
                <div className="text-bad font-medium mb-2">Safety warnings</div>
                {safety.runs.map((r, i) => (
                  <div key={`${r.worker}-${i}`} className="text-sm">
                    <div className="font-mono text-xs text-ink-2">{r.worker}</div>
                    {r.protected_path_hits.length > 0 && (
                      <div>protected paths: {r.protected_path_hits.join(", ")}</div>
                    )}
                    {r.dangerous_command_hits.length > 0 && (
                      <div>dangerous commands: {r.dangerous_command_hits.join(", ")}</div>
                    )}
                  </div>
                ))}
              </Card>
            )}
          </div>
        )}

        {tab === "merge" && (
          <div className="space-y-3">
            <Card>
              <div className="flex items-center justify-between mb-2">
                <div className="font-medium">MERGE_PLAN.md</div>
                <div className="flex gap-2">
                  {task.runs.map((r, i) => (
                    <Button
                      key={`${r.worker}-${i}`}
                      variant={review?.recommended === r.worker ? "primary" : "ghost"}
                      onClick={() => {
                        setMergeWorker(r.worker);
                        setMergeOpen(true);
                      }}
                    >
                      Merge {r.worker}
                    </Button>
                  ))}
                </div>
              </div>
              {mergePlanMd ? <Markdown text={mergePlanMd} /> : <div className="text-sm text-ink-3">Run review to generate.</div>}
            </Card>
          </div>
        )}
      </div>

      <Modal
        open={mergeOpen}
        onClose={() => setMergeOpen(false)}
        title={`Merge ${mergeWorker} into base?`}
        footer={
          <>
            <Button onClick={() => setMergeOpen(false)}>Cancel</Button>
            <Button onClick={() => doMerge(true)}>Dry run</Button>
            <Button variant="primary" onClick={() => doMerge(false)}>
              Confirm merge
            </Button>
          </>
        }
      >
        <div className="space-y-3">
          <div>
            This will checkout the base branch and merge{" "}
            <code className="px-1 bg-bg-1 rounded">{mergeWorker}</code>'s branch.
            Tests will run automatically after merge.
          </div>
          <div>
            <label className="text-xs text-ink-2 mb-1 block">
              Optional reason (recorded in decision log)
            </label>
            <Input
              placeholder="Why this merge is the right call…"
              value={mergeReason}
              onChange={(e) => setMergeReason(e.target.value)}
            />
          </div>
          <div className="text-xs text-ink-3">
            The merge cannot be undone via the UI. Use git locally to roll back if needed.
          </div>
        </div>
      </Modal>
    </div>
  );
}

function ArtifactList({
  files,
  kind,
  markdown,
}: {
  files: ArtifactFile[];
  kind: string;
  markdown?: boolean;
}) {
  const [sel, setSel] = React.useState<string | null>(files[0]?.name || null);
  React.useEffect(() => {
    setSel(files[0]?.name || null);
  }, [files.length]);
  if (!files.length) {
    return <Empty title={`No ${kind} files yet.`} body={`They appear after the workers run and \`collect\` is called.`} />;
  }
  const cur = files.find((f) => f.name === sel) || files[0];
  return (
    <div className="grid gap-3" style={{ gridTemplateColumns: "260px 1fr" }}>
      <Card className="p-0 overflow-hidden">
        {files.map((f) => (
          <button
            key={f.name}
            onClick={() => setSel(f.name)}
            className={`block w-full text-left text-xs px-3 py-1.5 hover:bg-bg-3 ${
              sel === f.name ? "bg-bg-3 text-ink-0" : "text-ink-1"
            }`}
          >
            {f.name}{" "}
            <span className="text-ink-3">({(f.bytes / 1024).toFixed(1)} KB)</span>
          </button>
        ))}
      </Card>
      <Card className="overflow-y-auto max-h-[70vh]">
        {markdown ? <Markdown text={cur.content} /> : <CodeBlock>{cur.content}</CodeBlock>}
      </Card>
    </div>
  );
}
