import React from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  enableSkill,
  disableSkill,
  getProject,
  getProjectGraph,
  listMemoryEntities,
  listMemoryFiles,
  listRelationships,
  listSkills,
  listTasks,
  pinSkill,
  readMemoryFile,
  unpinSkill,
  validateMemory,
  repairMemory,
} from "../api/client";
import type {
  GraphEntity,
  GraphRelationship,
  MemoryFile,
  ProjectDetail,
  SkillRow,
  TaskRow,
} from "../lib/types";
import { Badge, Button, Card, CodeBlock, Empty, Tabs, useToast } from "../components/ui";
import { Markdown } from "../components/Markdown";

const TABS = [
  { value: "overview", label: "Overview" },
  { value: "tasks", label: "Tasks" },
  { value: "skills", label: "Skills" },
  { value: "memory", label: "Memory" },
  { value: "graph", label: "Graph" },
  { value: "safety", label: "Safety" },
  { value: "settings", label: "Settings" },
];

export function ProjectPage() {
  const { id } = useParams();
  const pid = Number(id);
  const [tab, setTab] = React.useState("overview");
  const [project, setProject] = React.useState<ProjectDetail | null>(null);

  React.useEffect(() => {
    getProject(pid).then(setProject).catch(() => setProject(null));
  }, [pid]);

  if (!project) {
    return (
      <div className="p-8">
        <Card>loading project…</Card>
      </div>
    );
  }

  return (
    <div className="p-6">
      <div className="mb-4 flex items-start justify-between gap-4">
        <div>
          <div className="text-xs text-ink-3">project</div>
          <h1 className="text-2xl font-semibold tracking-tight">{project.name}</h1>
          <div className="text-xs text-ink-2 mt-1">
            <code className="bg-bg-1 px-1 py-0.5 rounded">{project.repo_path}</code>{" "}
            on <Badge tone="neutral">{project.base_branch}</Badge>
          </div>
        </div>
        <a
          href={`/p/${project.id}/graph`}
          className="text-xs px-3 py-2 rounded border border-bg-2 bg-bg-1 hover:bg-bg-2 transition-colors flex items-center gap-2"
          title="Live tree of all tasks, workers, and phases for this project"
        >
          ● Live tree
        </a>
      </div>

      <Tabs value={tab} onChange={setTab} items={TABS} />

      <div className="mt-4">
        {tab === "overview" && <OverviewTab project={project} />}
        {tab === "tasks" && <TasksTab project={project} />}
        {tab === "skills" && <ProjectSkillsTab project={project} />}
        {tab === "memory" && <MemoryTab project={project} />}
        {tab === "graph" && <GraphTab project={project} />}
        {tab === "safety" && <SafetyTab project={project} />}
        {tab === "settings" && <SettingsTab project={project} />}
      </div>
    </div>
  );
}

function OverviewTab({ project }: { project: ProjectDetail }) {
  return (
    <div className="grid md:grid-cols-2 gap-4">
      <Card>
        <div className="font-medium mb-2">Stack</div>
        <div className="text-sm text-ink-1 space-y-1">
          <div>Languages: {project.config.stack.languages.join(", ") || "—"}</div>
          <div>Frameworks: {project.config.stack.frameworks.join(", ") || "—"}</div>
          <div>Package managers: {project.config.stack.package_managers.join(", ") || "—"}</div>
          <div className="text-ink-3 text-xs">
            Detected: {project.config.stack.detected_files.join(", ") || "none"}
          </div>
        </div>
      </Card>
      <Card>
        <div className="font-medium mb-2">Test commands</div>
        {project.config.test_commands.length === 0 ? (
          <div className="text-sm text-ink-3">no test commands configured</div>
        ) : (
          <ul className="text-sm text-ink-1 list-disc pl-4">
            {project.config.test_commands.map((c) => (
              <li key={c}>
                <code>{c}</code>
              </li>
            ))}
          </ul>
        )}
      </Card>
      <Card>
        <div className="font-medium mb-2">Pinned skills</div>
        {project.config.pinned_skills.length === 0 ? (
          <div className="text-sm text-ink-3">none pinned</div>
        ) : (
          <div className="flex flex-wrap gap-1.5">
            {project.config.pinned_skills.map((s) => (
              <Badge key={s} tone="accent">
                {s}
              </Badge>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}

function TasksTab({ project }: { project: ProjectDetail }) {
  const navigate = useNavigate();
  const [tasks, setTasks] = React.useState<TaskRow[]>([]);
  React.useEffect(() => {
    listTasks(project.id).then(setTasks).catch(() => setTasks([]));
  }, [project.id]);
  if (!tasks.length) {
    return <Empty title="No tasks yet." body="Launch one from the Home page." />;
  }
  return (
    <Card className="p-0 overflow-hidden">
      <table className="w-full text-sm">
        <thead className="text-left text-ink-3 border-b border-line">
          <tr>
            <th className="px-4 py-2">#</th>
            <th className="px-4 py-2">title</th>
            <th className="px-4 py-2">status</th>
            <th className="px-4 py-2">base</th>
            <th className="px-4 py-2">created</th>
          </tr>
        </thead>
        <tbody>
          {tasks.map((t) => (
            <tr
              key={t.id}
              className="border-b border-line/40 hover:bg-bg-3/40 cursor-pointer"
              onClick={() => navigate(`/p/${project.id}/t/${t.task_number}`)}
            >
              <td className="px-4 py-2 font-mono">{String(t.task_number).padStart(4, "0")}</td>
              <td className="px-4 py-2 text-ink-0">{t.title}</td>
              <td className="px-4 py-2">
                <Badge
                  tone={
                    t.status === "merged"
                      ? "ok"
                      : t.status === "stopped"
                        ? "warn"
                        : t.status === "running"
                          ? "accent"
                          : "neutral"
                  }
                >
                  {t.status}
                </Badge>
              </td>
              <td className="px-4 py-2">{t.base_branch}</td>
              <td className="px-4 py-2 text-ink-3 text-xs">{t.created_at}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}

function ProjectSkillsTab({ project }: { project: ProjectDetail }) {
  const toast = useToast();
  const [rows, setRows] = React.useState<SkillRow[]>([]);
  const [filter, setFilter] = React.useState("");
  const pinned = new Set(project.config.pinned_skills);
  async function refresh() {
    setRows(await listSkills());
  }
  React.useEffect(() => {
    refresh();
  }, []);

  const visible = rows.filter(
    (r) =>
      !filter ||
      r.skill_id.includes(filter) ||
      (r.category || "").includes(filter) ||
      (r.name || "").toLowerCase().includes(filter.toLowerCase()),
  );

  return (
    <div className="space-y-3">
      <input
        className="input"
        placeholder="filter skills…"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
      />
      <Card className="p-0 overflow-hidden">
        <table className="w-full text-sm">
          <thead className="text-left text-ink-3 border-b border-line">
            <tr>
              <th className="px-3 py-2">skill</th>
              <th className="px-3 py-2">category</th>
              <th className="px-3 py-2">enabled</th>
              <th className="px-3 py-2">trust</th>
              <th className="px-3 py-2">actions</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((s) => (
              <tr key={s.skill_id} className="border-b border-line/40">
                <td className="px-3 py-2">
                  <div className="font-medium">{s.skill_id}</div>
                  <div className="text-xs text-ink-3 truncate max-w-[40ch]">
                    {s.description || ""}
                  </div>
                </td>
                <td className="px-3 py-2">{s.category}</td>
                <td className="px-3 py-2">
                  <Badge tone={s.enabled ? "ok" : "neutral"}>{s.enabled ? "on" : "off"}</Badge>
                </td>
                <td className="px-3 py-2">
                  <Badge tone={s.trust_level === "local-curated" ? "accent" : "warn"}>
                    {s.trust_level}
                  </Badge>
                </td>
                <td className="px-3 py-2 flex gap-1.5">
                  <Button
                    onClick={async () => {
                      if (s.enabled) {
                        await disableSkill(s.skill_id);
                      } else {
                        await enableSkill(s.skill_id);
                      }
                      refresh();
                    }}
                  >
                    {s.enabled ? "disable" : "enable"}
                  </Button>
                  <Button
                    onClick={async () => {
                      if (pinned.has(s.skill_id)) {
                        await unpinSkill(project.id, s.skill_id);
                      } else {
                        await pinSkill(project.id, s.skill_id);
                      }
                      toast.push("ok", `${pinned.has(s.skill_id) ? "Unpinned" : "Pinned"} ${s.skill_id}`);
                      window.location.reload();
                    }}
                  >
                    {pinned.has(s.skill_id) ? "unpin" : "pin"}
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </div>
  );
}

function MemoryTab({ project }: { project: ProjectDetail }) {
  const toast = useToast();
  const [files, setFiles] = React.useState<MemoryFile[]>([]);
  const [selected, setSelected] = React.useState<string | null>(null);
  const [content, setContent] = React.useState<string>("");
  React.useEffect(() => {
    listMemoryFiles(project.id).then(setFiles);
  }, [project.id]);
  React.useEffect(() => {
    if (!selected) {
      setContent("");
      return;
    }
    readMemoryFile(project.id, selected).then((r) => setContent(r.content));
  }, [selected, project.id]);

  return (
    <div className="grid gap-4" style={{ gridTemplateColumns: "260px 1fr" }}>
      <Card className="p-0 overflow-hidden">
        <div className="px-3 py-2 border-b border-line flex items-center justify-between">
          <div className="text-sm font-medium">Vault</div>
          <div className="flex gap-1.5">
            <Button
              onClick={async () => {
                const r = await validateMemory(project.id);
                toast.push(r.ok ? "ok" : "warn", r.ok ? "Vault OK" : `Missing: ${[...r.missing_files, ...r.missing_dirs].join(", ")}`);
              }}
            >
              validate
            </Button>
            <Button
              onClick={async () => {
                await repairMemory(project.id);
                toast.push("ok", "Vault repaired");
                listMemoryFiles(project.id).then(setFiles);
              }}
            >
              repair
            </Button>
          </div>
        </div>
        <div className="max-h-[60vh] overflow-y-auto">
          {files.map((f) => (
            <button
              key={f.path}
              onClick={() => setSelected(f.path)}
              className={`block w-full text-left text-xs px-3 py-1.5 hover:bg-bg-3 ${
                selected === f.path ? "bg-bg-3 text-ink-0" : "text-ink-1"
              }`}
            >
              {f.path}
            </button>
          ))}
        </div>
      </Card>
      <Card className="overflow-y-auto max-h-[70vh]">
        {!selected && <div className="text-sm text-ink-3">Pick a note.</div>}
        {selected && <Markdown text={content} />}
      </Card>
    </div>
  );
}

function GraphTab({ project }: { project: ProjectDetail }) {
  const [entities, setEntities] = React.useState<GraphEntity[]>([]);
  const [rels, setRels] = React.useState<GraphRelationship[]>([]);
  React.useEffect(() => {
    listMemoryEntities(project.id).then(setEntities);
    listRelationships(project.id).then(setRels);
  }, [project.id]);
  return (
    <div className="grid md:grid-cols-2 gap-4">
      <Card>
        <div className="font-medium mb-2">Entities ({entities.length})</div>
        <div className="max-h-[50vh] overflow-y-auto">
          <table className="w-full text-sm">
            <thead className="text-ink-3 text-left">
              <tr>
                <th className="py-1">type</th>
                <th className="py-1">name</th>
              </tr>
            </thead>
            <tbody>
              {entities.map((e) => (
                <tr key={e.entity_id} className="border-t border-line/40">
                  <td className="py-1 text-xs text-ink-2">{e.entity_type}</td>
                  <td className="py-1">{e.name}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
      <Card>
        <div className="font-medium mb-2">Relationships ({rels.length})</div>
        <div className="max-h-[50vh] overflow-y-auto text-sm">
          {rels.map((r, i) => (
            <div key={i} className="border-t border-line/40 py-1 text-ink-1">
              <code className="text-xs">{r.from_entity_id}</code>
              <span className="text-ink-3"> —[{r.relation_type}]→ </span>
              <code className="text-xs">{r.to_entity_id}</code>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}

function SafetyTab({ project }: { project: ProjectDetail }) {
  return (
    <Card>
      <div className="font-medium mb-2">Project-level safety</div>
      <div className="text-sm text-ink-2 mb-3">
        Global protected paths and dangerous commands also apply. See the global
        Safety page for the full list.
      </div>
      <div className="text-sm text-ink-1">
        <div className="mb-2">Project protected paths:</div>
        <CodeBlock>
          {project.config.test_commands.length
            ? "(see project.yaml; UI display coming soon)"
            : "(none configured)"}
        </CodeBlock>
      </div>
    </Card>
  );
}

function SettingsTab({ project }: { project: ProjectDetail }) {
  return (
    <Card>
      <div className="font-medium mb-2">Project config</div>
      <CodeBlock>{JSON.stringify(project.config, null, 2)}</CodeBlock>
    </Card>
  );
}
