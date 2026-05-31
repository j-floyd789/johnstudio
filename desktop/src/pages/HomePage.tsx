import React from "react";
import { useNavigate } from "react-router-dom";
import { Play, FolderPlus, Activity } from "lucide-react";
import {
  addProject,
  chainRun,
  getDoctor,
  getHealth,
  listProjects,
  runTask,
  teamRun,
} from "../api/client";
import type { DoctorResponse, Project, RunTaskRequest } from "../lib/types";
import { Badge, Button, Card, Empty, Input, Modal, Textarea, useToast } from "../components/ui";

export function HomePage() {
  const navigate = useNavigate();
  const toast = useToast();
  const [projects, setProjects] = React.useState<Project[]>([]);
  const [selected, setSelected] = React.useState<number | null>(null);
  const [doctor, setDoctor] = React.useState<DoctorResponse | null>(null);
  const [serverDown, setServerDown] = React.useState(false);

  const [task, setTask] = React.useState("");
  const [stubOnly, setStubOnly] = React.useState(true);
  const [maxAgents, setMaxAgents] = React.useState<number | "auto">("auto");
  const [mode, setMode] = React.useState<"parallel" | "chain" | "team">("parallel");
  const [running, setRunning] = React.useState(false);

  const [addOpen, setAddOpen] = React.useState(false);
  const [newName, setNewName] = React.useState("");
  const [newPath, setNewPath] = React.useState("");

  async function refresh() {
    try {
      await getHealth();
      setServerDown(false);
      const [p, d] = await Promise.all([listProjects(), getDoctor()]);
      setProjects(p);
      setDoctor(d);
      if (selected === null && p.length) setSelected(p[0].id);
    } catch (e: any) {
      setServerDown(true);
    }
  }
  React.useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, []);

  async function onRun() {
    if (!selected || !task.trim()) return;
    setRunning(true);
    try {
      if (mode === "team") {
        const out = await teamRun(selected, task);
        toast.push("ok", `Team task ${out.task_number} launched — planner running.`);
        navigate(`/p/${selected}/team/${out.task_number}`);
      } else if (mode === "chain") {
        const worker = "claude_backend";
        const out = await chainRun(selected, {
          task, architect: worker, rfc_reviewer: worker,
          implementer: worker, reviewer: worker,
        });
        toast.push("ok", `Chain task ${out.task_number} started — RFC drafting…`);
        navigate(`/p/${selected}/c/${out.task_number}`);
      } else {
        const req: RunTaskRequest = {
          task,
          stub_only: stubOnly,
          max_agents: maxAgents === "auto" ? undefined : maxAgents,
        };
        const out = await runTask(selected, req);
        toast.push("ok", `Task ${out.task_number} launched — team: ${out.team.join(", ")}`);
        navigate(`/p/${selected}/t/${out.task_number}`);
      }
    } catch (e: any) {
      toast.push("bad", `Run failed: ${e.detail || e.message}`);
    } finally {
      setRunning(false);
    }
  }

  async function onAdd() {
    const cleanName = newName.trim();
    const cleanPath = newPath.trim();
    if (!cleanName || !cleanPath) return;
    try {
      const out = await addProject(cleanName, cleanPath);
      toast.push("ok", `Project ${newName} added (id=${out.project_id})`);
      setAddOpen(false);
      setNewName("");
      setNewPath("");
      refresh();
      setSelected(out.project_id);
    } catch (e: any) {
      toast.push("bad", `Add project failed: ${e.detail || e.message}`);
    }
  }

  if (serverDown) {
    return (
      <div className="p-8">
        <Card className="border-bad/40">
          <div className="text-bad font-semibold mb-2">Backend not running</div>
          <div className="text-sm text-ink-1 mb-3">
            JohnStudio backend is not running. Start it with:
          </div>
          <pre className="card bg-bg-1 text-xs">johnstudio server</pre>
          <div className="mt-3 text-xs text-ink-3">
            Then this page will reconnect automatically.
          </div>
        </Card>
      </div>
    );
  }

  return (
    <div className="p-8 max-w-5xl mx-auto">
      <div className="mb-8">
        <h1 className="text-3xl font-semibold tracking-tight">
          John, what are we building today?
        </h1>
        <p className="text-ink-2 mt-1 text-sm">
          Pick a project, describe one task, and JohnStudio will coordinate a bounded AI dev team.
        </p>
      </div>

      <Card className="mb-6">
        <div className="flex items-center gap-2 mb-3">
          <Activity size={16} className="text-ink-2" />
          <div className="font-medium">System health</div>
        </div>
        <HealthBadges doctor={doctor} />
      </Card>

      <Card className="mb-6">
        <div className="flex items-center justify-between mb-3">
          <div className="font-medium">Project</div>
          <Button variant="ghost" onClick={() => setAddOpen(true)}>
            <FolderPlus size={14} />
            Add project
          </Button>
        </div>
        {projects.length === 0 ? (
          <Empty
            title="No projects yet."
            body="Register a git repository to start coordinating AI workers on it."
            cta={
              <Button variant="primary" onClick={() => setAddOpen(true)}>
                <FolderPlus size={14} /> Add a project
              </Button>
            }
          />
        ) : (
          <div className="grid grid-cols-2 md:grid-cols-3 gap-2">
            {projects.map((p) => (
              <button
                key={p.id}
                onClick={() => setSelected(p.id)}
                className={`text-left p-3 rounded-md border ${
                  selected === p.id
                    ? "border-accent bg-accent/5"
                    : "border-line hover:bg-bg-3"
                }`}
              >
                <div className="font-medium truncate">{p.name}</div>
                <div className="text-xs text-ink-3 truncate">{p.repo_path}</div>
                <div className="mt-1 flex gap-1">
                  <Badge tone="neutral">{p.base_branch}</Badge>
                </div>
              </button>
            ))}
          </div>
        )}
      </Card>

      <Card>
        <div className="flex items-center gap-2 mb-3">
          <Play size={16} className="text-accent" />
          <div className="font-medium">New task</div>
          {selected && (
            <Badge tone="accent">
              {projects.find((p) => p.id === selected)?.name || `project ${selected}`}
            </Badge>
          )}
        </div>
        <Textarea
          placeholder="Describe what you want to build…"
          value={task}
          onChange={(e) => setTask(e.target.value)}
        />
        <div className="flex flex-wrap items-center justify-between gap-3 mt-3">
          <div className="flex items-center gap-3 flex-wrap">
            <label className="text-sm flex items-center gap-2 text-ink-1">
              <input
                type="radio"
                name="mode"
                checked={mode === "parallel"}
                onChange={() => setMode("parallel")}
              />
              Parallel
            </label>
            <label className="text-sm flex items-center gap-2 text-ink-1">
              <input
                type="radio"
                name="mode"
                checked={mode === "chain"}
                onChange={() => setMode("chain")}
              />
              Chain (RFC → impl → review)
            </label>
            <label className="text-sm flex items-center gap-2 text-ink-1">
              <input
                type="radio"
                name="mode"
                checked={mode === "team"}
                onChange={() => setMode("team")}
              />
              Team (planner → specialists)
            </label>
            {mode === "parallel" && (
              <label className="text-sm flex items-center gap-2 text-ink-1">
                <input
                  type="checkbox"
                  checked={stubOnly}
                  onChange={(e) => setStubOnly(e.target.checked)}
                />
                Stub-only (offline)
              </label>
            )}
            {mode === "parallel" && (
              <label className="text-sm flex items-center gap-2 text-ink-1">
                Max agents:
                <select
                  value={String(maxAgents)}
                  onChange={(e) =>
                    setMaxAgents(e.target.value === "auto" ? "auto" : Number(e.target.value))
                  }
                  className="bg-bg-1 border border-line rounded px-2 py-1 text-sm"
                >
                  <option value="auto">auto</option>
                  <option value="1">1</option>
                  <option value="2">2</option>
                  <option value="3">3</option>
                  <option value="4">4</option>
                  <option value="6">6</option>
                </select>
              </label>
            )}
            {mode === "team" && (
              <span className="text-xs text-ink-3">
                Gemini planner writes a TEAM_PLAN.md you approve before any specialist runs.
              </span>
            )}
          </div>
          <Button
            variant="primary"
            onClick={onRun}
            disabled={!selected || !task.trim() || running}
          >
            <Play size={14} />
            {running ? "Launching…" : "Run"}
          </Button>
        </div>
      </Card>

      <Modal
        open={addOpen}
        onClose={() => setAddOpen(false)}
        title="Add a project"
        footer={
          <>
            <Button onClick={() => setAddOpen(false)}>Cancel</Button>
            <Button variant="primary" onClick={onAdd}>
              Add
            </Button>
          </>
        }
      >
        <div className="space-y-3">
          <div>
            <label className="text-xs text-ink-2 mb-1 block">Name</label>
            <Input
              placeholder="my-app"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
            />
          </div>
          <div>
            <label className="text-xs text-ink-2 mb-1 block">
              Absolute path to git repo
            </label>
            <Input
              placeholder="/Users/you/code/my-app"
              value={newPath}
              onChange={(e) => setNewPath(e.target.value)}
            />
          </div>
          <div className="text-xs text-ink-3">
            The path must be a git repository. JohnStudio will scaffold
            <code className="mx-1 px-1 bg-bg-1 rounded">.johnstudio/</code>
            inside it (untracked).
          </div>
        </div>
      </Modal>
    </div>
  );
}

function HealthBadges({ doctor }: { doctor: DoctorResponse | null }) {
  if (!doctor) {
    return <div className="text-sm text-ink-3">checking…</div>;
  }
  const items: Array<{ label: string; ok: boolean }> = [
    ...Object.entries(doctor.tools).map(([k, v]) => ({ label: k, ok: !!v })),
    { label: "DB", ok: true },
    { label: doctor.fts5 ? "FTS5" : "LIKE", ok: true },
    ...doctor.workers.map((w) => ({ label: w.name, ok: w.is_available })),
  ];
  return (
    <div className="flex flex-wrap gap-1.5">
      {items.map((b) => (
        <Badge key={b.label} tone={b.ok ? "ok" : "warn"}>
          {b.label}: {b.ok ? "ok" : "missing"}
        </Badge>
      ))}
    </div>
  );
}
