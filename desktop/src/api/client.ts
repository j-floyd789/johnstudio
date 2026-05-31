// API client for the JohnStudio FastAPI server (default http://127.0.0.1:8765).
// Every function calls a real route — when the backend is down, requests reject
// with a structured ApiError that the UI can show as a toast.

import type {
  ArtifactFile,
  DoctorResponse,
  GraphEntity,
  GraphRelationship,
  HealthResponse,
  MemoryFile,
  MergeResponse,
  Project,
  ProjectDetail,
  ReviewResponse,
  RouteResult,
  RunTaskRequest,
  RunTaskResponse,
  SafetyReport,
  SkillDetail,
  SkillRow,
  SkillSource,
  TaskRow,
  TaskStatus,
  WorkerInfo,
} from "../lib/types";

export const API_BASE =
  (import.meta as any).env?.VITE_API_BASE || "http://127.0.0.1:8765";

// Loopback bearer token. start_ui.sh reads it from
// $JOHNSTUDIO_HOME/server_token and exports it as VITE_JOHNSTUDIO_TOKEN.
// /api/health is the one route that does not require it.
const API_TOKEN: string | undefined =
  (import.meta as any).env?.VITE_JOHNSTUDIO_TOKEN || undefined;

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: string,
    public path: string,
  ) {
    super(`${status} ${detail} (${path})`);
  }
}

async function http<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  const authHeader: Record<string, string> =
    API_TOKEN && path !== "/api/health"
      ? { authorization: `Bearer ${API_TOKEN}` }
      : {};
  try {
    res = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: {
        "content-type": "application/json",
        ...authHeader,
        ...(init?.headers || {}),
      },
    });
  } catch (e: any) {
    throw new ApiError(0, `Backend unreachable: ${e?.message || e}`, path);
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body?.detail || JSON.stringify(body);
    } catch {
      /* keep statusText */
    }
    throw new ApiError(res.status, detail, path);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// ---------------------------------------------------------------------------
// System
// ---------------------------------------------------------------------------

export const getHealth = () => http<HealthResponse>("/api/health");
export const getDoctor = () => http<DoctorResponse>("/api/doctor");

// ---------------------------------------------------------------------------
// Projects
// ---------------------------------------------------------------------------

export const listProjects = () => http<Project[]>("/api/projects");
export const addProject = (name: string, repo_path: string) =>
  http<{ project_id: number; project_yaml: string; stack: any; base_branch: string }>(
    "/api/projects",
    { method: "POST", body: JSON.stringify({ name, repo_path }) },
  );
export const getProject = (id: number) => http<ProjectDetail>(`/api/projects/${id}`);
export const getProjectMemory = (id: number) =>
  http<{ files: MemoryFile[]; vault_root: string }>(`/api/projects/${id}/memory`);
export const getProjectGraph = (id: number) =>
  http<{ entities: GraphEntity[]; relationships: GraphRelationship[] }>(
    `/api/projects/${id}/graph`,
  );

// ---------------------------------------------------------------------------
// Tasks
// ---------------------------------------------------------------------------

export const listTasks = (project_id: number) =>
  http<TaskRow[]>(`/api/projects/${project_id}/tasks`);

export const runTask = (project_id: number, req: RunTaskRequest) =>
  http<RunTaskResponse>(`/api/projects/${project_id}/tasks/run`, {
    method: "POST",
    body: JSON.stringify(req),
  });

export const getTask = (project_id: number, task_number: number) =>
  http<TaskStatus>(`/api/projects/${project_id}/tasks/${task_number}`);

export const collectTask = (project_id: number, task_number: number) =>
  http<{ task_number: number; runs: any[] }>(
    `/api/projects/${project_id}/tasks/${task_number}/collect`,
    { method: "POST" },
  );

export const reviewTask = (project_id: number, task_number: number) =>
  http<ReviewResponse>(
    `/api/projects/${project_id}/tasks/${task_number}/review`,
    { method: "POST" },
  );

export const mergeTask = (
  project_id: number,
  task_number: number,
  worker_name: string,
  confirm: boolean,
  dry_run = false,
) =>
  http<MergeResponse>(
    `/api/projects/${project_id}/tasks/${task_number}/merge`,
    {
      method: "POST",
      body: JSON.stringify({ worker_name, confirm, dry_run }),
    },
  );

export const stopTask = (project_id: number, task_number: number) =>
  http<{ task_number: number; session: string }>(
    `/api/projects/${project_id}/tasks/${task_number}/stop`,
    { method: "POST" },
  );

export const cleanupTask = (
  project_id: number,
  task_number: number,
  prune_worktrees = true,
) =>
  http<{ task_number: number; removed_worktrees: string[] }>(
    `/api/projects/${project_id}/tasks/${task_number}/cleanup`,
    { method: "POST", body: JSON.stringify({ prune_worktrees }) },
  );

export const resumeTask = (
  project_id: number,
  task_number: number,
  worker_name: string,
) =>
  http<{ resumed: boolean; prompt: string; session?: string; pane?: string }>(
    `/api/projects/${project_id}/tasks/${task_number}/resume`,
    { method: "POST", body: JSON.stringify({ worker_name }) },
  );

export const getContextPacks = (project_id: number, task_number: number) =>
  http<ArtifactFile[]>(
    `/api/projects/${project_id}/tasks/${task_number}/context-packs`,
  );

export const getResults = (project_id: number, task_number: number) =>
  http<ArtifactFile[]>(
    `/api/projects/${project_id}/tasks/${task_number}/results`,
  );

export const getDiffs = (project_id: number, task_number: number) =>
  http<ArtifactFile[]>(
    `/api/projects/${project_id}/tasks/${task_number}/diffs`,
  );

export const getLogs = (project_id: number, task_number: number) =>
  http<ArtifactFile[]>(`/api/projects/${project_id}/tasks/${task_number}/logs`);

export const getReviewMarkdown = (project_id: number, task_number: number) =>
  http<{ exists: boolean; content: string }>(
    `/api/projects/${project_id}/tasks/${task_number}/review`,
  );

export const getMergePlanMarkdown = (project_id: number, task_number: number) =>
  http<{ exists: boolean; content: string }>(
    `/api/projects/${project_id}/tasks/${task_number}/merge-plan`,
  );

export const getSafetyReport = (project_id: number, task_number: number) =>
  http<SafetyReport>(
    `/api/projects/${project_id}/tasks/${task_number}/safety-report`,
  );

// ---------------------------------------------------------------------------
// Skills
// ---------------------------------------------------------------------------

export const listSkills = (filters?: { enabled_only?: boolean; category?: string }) => {
  const q = new URLSearchParams();
  if (filters?.enabled_only) q.set("enabled_only", "true");
  if (filters?.category) q.set("category", filters.category);
  const qs = q.toString() ? `?${q}` : "";
  return http<SkillRow[]>(`/api/skills${qs}`);
};

export const getSkill = (id: string) => http<SkillDetail>(`/api/skills/${id}`);
export const enableSkill = (id: string) =>
  http<{ skill_id: string; enabled: boolean }>(`/api/skills/${id}/enable`, {
    method: "POST",
  });
export const disableSkill = (id: string) =>
  http<{ skill_id: string; enabled: boolean }>(`/api/skills/${id}/disable`, {
    method: "POST",
  });
export const pinSkill = (project_id: number, skill_id: string) =>
  http<{ pinned: string[] }>(
    `/api/projects/${project_id}/skills/${skill_id}/pin`,
    { method: "POST" },
  );
export const unpinSkill = (project_id: number, skill_id: string) =>
  http<{ pinned: string[] }>(
    `/api/projects/${project_id}/skills/${skill_id}/unpin`,
    { method: "POST" },
  );
export const discoverSkills = (
  project_id: number,
  task: string,
  agent_role = "backend_implementer",
) =>
  http<RouteResult[]>(`/api/projects/${project_id}/skills/discover`, {
    method: "POST",
    body: JSON.stringify({ task, agent_role }),
  });

export const addSkillSource = (uri: string) =>
  http<{ id: number; local: boolean; path: string | null }>("/api/skills/source", {
    method: "POST",
    body: JSON.stringify({ uri }),
  });
export const listSkillSources = () => http<SkillSource[]>("/api/skills/sources");
export const scanSkillSources = () =>
  http<Array<{ id: number; imported?: number; skipped?: string }>>(
    "/api/skills/sources/scan",
    { method: "POST" },
  );

// ---------------------------------------------------------------------------
// Memory
// ---------------------------------------------------------------------------

export const listMemoryFiles = (project_id: number) =>
  http<MemoryFile[]>(`/api/projects/${project_id}/memory/files`);
export const readMemoryFile = (project_id: number, path: string) =>
  http<{ path: string; content: string }>(
    `/api/projects/${project_id}/memory/file?path=${encodeURIComponent(path)}`,
  );
export const listMemoryEntities = (project_id: number) =>
  http<GraphEntity[]>(`/api/projects/${project_id}/memory/entities`);
export const listRelationships = (project_id: number) =>
  http<GraphRelationship[]>(`/api/projects/${project_id}/memory/relationships`);
export const memoryBacklinks = (project_id: number, note: string) =>
  http<{ note: string; sources: string[] }>(
    `/api/projects/${project_id}/memory/backlinks?note=${encodeURIComponent(note)}`,
  );
export const validateMemory = (project_id: number) =>
  http<{
    ok: boolean;
    missing_files: string[];
    missing_dirs: string[];
    missing_graph_dirs: string[];
  }>(`/api/projects/${project_id}/memory/validate`, { method: "POST" });
export const repairMemory = (project_id: number) =>
  http<{ repaired: boolean }>(`/api/projects/${project_id}/memory/repair`, {
    method: "POST",
  });

// ---------------------------------------------------------------------------
// Workers
// ---------------------------------------------------------------------------

export const listWorkers = () => http<WorkerInfo[]>("/api/workers");
export const workerDoctor = () => http<DoctorResponse>("/api/workers/doctor");
export const testWorker = (name: string) =>
  http<{ available: boolean; tested: boolean; ok?: boolean; note?: string }>(
    `/api/workers/${name}/test`,
    { method: "POST" },
  );

// ---------------------------------------------------------------------------
// Chain (RFC → implement → review → merge)
// ---------------------------------------------------------------------------

export interface ChainPhase {
  id: number;
  phase: string;
  round: number;
  status: string;
  verdict: string | null;
  artifact_path: string | null;
  notes: string | null;
  started_at: string | null;
  completed_at: string | null;
}

export interface ChainStatus {
  task_number: number;
  phases: ChainPhase[];
  current: ChainPhase | null;
  human_gate: boolean;
  terminal: boolean;
}

export const chainRun = (
  project_id: number,
  payload: {
    task: string;
    architect?: string;
    rfc_reviewer?: string;
    implementer?: string;
    reviewer?: string;
  },
) =>
  http<{ task_number: number; task_db_id: number }>(
    `/api/projects/${project_id}/chain/run`,
    { method: "POST", body: JSON.stringify(payload) },
  );

export const chainStatus = (project_id: number, task_number: number) =>
  http<ChainStatus>(`/api/projects/${project_id}/chain/${task_number}`);

export const chainAdvance = (project_id: number, task_number: number) =>
  http<any>(`/api/projects/${project_id}/chain/${task_number}/advance`, {
    method: "POST",
  });

export const chainArtifact = (
  project_id: number,
  task_number: number,
  kind: string,
) =>
  http<{ kind: string; exists: boolean; path: string; content: string }>(
    `/api/projects/${project_id}/chain/${task_number}/artifact?kind=${encodeURIComponent(kind)}`,
  );

export const chainApproveRfc = (
  project_id: number,
  task_number: number,
  note?: string,
) =>
  http<{ approved: boolean }>(
    `/api/projects/${project_id}/chain/${task_number}/approve-rfc`,
    { method: "POST", body: JSON.stringify({ note }) },
  );

export const chainRejectRfc = (
  project_id: number,
  task_number: number,
  reason?: string,
) =>
  http<{ rejected: boolean }>(
    `/api/projects/${project_id}/chain/${task_number}/reject-rfc`,
    { method: "POST", body: JSON.stringify({ reason }) },
  );

export const chainMerge = (
  project_id: number,
  task_number: number,
  confirm: boolean,
) =>
  http<MergeResponse>(
    `/api/projects/${project_id}/chain/${task_number}/merge`,
    { method: "POST", body: JSON.stringify({ confirm }) },
  );

export const chainReject = (
  project_id: number,
  task_number: number,
  reason?: string,
) =>
  http<any>(
    `/api/projects/${project_id}/chain/${task_number}/reject`,
    { method: "POST", body: JSON.stringify({ reason }) },
  );

// ---------------------------------------------------------------------------
// Team mode (RFC 0001 — planner-driven specialist teams)
// ---------------------------------------------------------------------------

export interface RoleSummary {
  name: string;
  vp: "claude_vp" | "codex_vp" | "gemini_vp";
  provider: string;
  description: string;
  can_edit: boolean;
  model: string;
  tools: string[];
}

export interface TeamAssignment {
  role: string;
  vp: "claude_vp" | "codex_vp" | "gemini_vp";
  brief: string;
  output: string;
}

export interface TeamPlanDoc {
  summary: string;
  assignments: TeamAssignment[];
  cross_review: { reviewer: string; reads: string[] }[];
  acceptance_criteria: string[];
}

export interface TeamState {
  task_db_id: number;
  task_number: number;
  project_name: string;
  status: "planning" | "running" | "reviewing" | "merged" | "rejected";
  plan_path: string;
  plan_exists: boolean;
  plan_valid?: boolean;
  plan_error?: string;
  plan?: TeamPlanDoc;
  assignments?: Array<{
    i: number; run_id: number; role: string; vp: string;
    brief: string; output: string; worktree: string | null; pid: number | null;
  }>;
}

export const teamCatalog = (project_id: number) =>
  http<{ total: number; by_vp: Record<string, RoleSummary[]> }>(
    `/api/projects/${project_id}/team/catalog`,
  );

export const teamRun = (project_id: number, task: string) =>
  http<{ task_number: number; task_db_id: number; status: string; planner: string }>(
    `/api/projects/${project_id}/team/run`,
    { method: "POST", body: JSON.stringify({ task }) },
  );

export const teamStatus = (project_id: number, task_number: number) =>
  http<TeamState>(`/api/projects/${project_id}/team/${task_number}`);

export const teamPlanRaw = (project_id: number, task_number: number) =>
  http<{ path: string; content: string; plan_valid: boolean; plan_error?: string; plan?: TeamPlanDoc }>(
    `/api/projects/${project_id}/team/${task_number}/plan`,
  );

export const teamApprove = (project_id: number, task_number: number) =>
  http<{ launched: Array<{ i: number; run_id: number; role: string; vp: string; brief: string; output: string; pid: number | null }> }>(
    `/api/projects/${project_id}/team/${task_number}/approve`,
    { method: "POST" },
  );

export const teamPlanCritic = (project_id: number, task_number: number) =>
  http<{ launched?: boolean; already_done?: boolean; critique_path?: string; refused?: boolean; reason?: string }>(
    `/api/projects/${project_id}/team/${task_number}/plan-critic`,
    { method: "POST" },
  );

export const teamBudget = (project_id: number, task_number: number) =>
  http<{ cost_usd: number; budget_usd: number | null; over_budget: boolean }>(
    `/api/projects/${project_id}/team/${task_number}/budget`,
  );

export const teamPlanCritique = (project_id: number, task_number: number) =>
  http<{ exists: boolean; path: string; content?: string }>(
    `/api/projects/${project_id}/team/${task_number}/plan-critique`,
  );

// Cluster E — progress score, stuck flag, activity feed
export interface TeamStuckRun {
  run_id: number;
  worker_name: string;
  role: string;
  status: string;
  last_event_ts: string | null;
  idle_seconds: number;
  event_count: number;
}

export interface TeamProgress {
  score: number;
  phase: string;
  total: number;
  done: number;
  in_flight: number;
  stuck: TeamStuckRun[];
  stuck_count: number;
  plan_exists: boolean;
  last_event_ts: string | null;
  test_round: number;
  revise_round: number;
}

export interface TeamActivityEvent {
  id: number;
  run_id: number;
  ts: string;
  kind: string;
  summary: string;
  seq: number;
  worker_name: string;
  role: string;
  run_status: string;
}

export const teamProgress = (project_id: number, task_number: number) =>
  http<TeamProgress>(
    `/api/projects/${project_id}/team/${task_number}/progress`,
  );

export const teamActivity = (project_id: number, task_number: number, limit = 30) =>
  http<{ task_number: number; count: number; events: TeamActivityEvent[] }>(
    `/api/projects/${project_id}/team/${task_number}/activity?limit=${limit}`,
  );

// Cluster F — cost meter, mid-flight cancel, per-worktree diffs ------------

export interface TeamCostWorker {
  run_id: number;
  worker_id: number | null;
  worker: string | null;
  role: string | null;
  status: string;
  cost_usd: number;
  tokens: number | null;
}

export interface TeamCost {
  task_db_id: number;
  total_cost_usd: number;
  runs_cost_usd: number;
  tokens_available: boolean;
  workers: TeamCostWorker[];
}

// Item 13 — per-task cost / token breakdown for the cost meter.
export const teamCost = (project_id: number, task_number: number) =>
  http<TeamCost>(`/api/projects/${project_id}/team/${task_number}/cost`);

export interface TeamCancelResult {
  task_db_id: number;
  task_status: string;
  count: number;
  cancelled: Array<{ run_id: number; worker: string; pid: number | null; killed: boolean }>;
  already_done?: boolean;
}

// Item 14 — kill live specialists for a team task (idempotent).
export const cancelTeam = (project_id: number, task_number: number) =>
  http<TeamCancelResult>(
    `/api/projects/${project_id}/team/${task_number}/cancel`,
    { method: "POST" },
  );

export interface TaskDiffEntry {
  worker: string;
  files_changed: string[];
  stat: { stat?: string } & Record<string, unknown>;
  diff_path: string | null;
  diff_text?: string;
  truncated?: boolean;
  error?: string;
}

// Item 15 — per-worktree diffs (changed files + diffstat + raw diff text).
export const taskDiffs = (project_id: number, task_number: number, include_text = true) =>
  http<{ task_number: number; diffs: TaskDiffEntry[] }>(
    `/api/projects/${project_id}/tasks/${task_number}/diffs?include_text=${include_text}`,
  );

// ---------------------------------------------------------------------------
// Transcripts (Claude Code session transcripts on disk, incl. subagents)
// ---------------------------------------------------------------------------

export interface TranscriptEntry {
  type: string;
  isSidechain?: boolean;
  sessionId?: string;
  parentUuid?: string;
  uuid?: string;
  message?: any;
  timestamp?: string;
  _index: number;
  _kind_summary: string;
}

export interface TranscriptResponse {
  run_id: number;
  cwd: string;
  session_id: string;
  transcript_path: string;
  encoded_dir: string;
  entries: TranscriptEntry[];
  n_total: number;
  n_sidechain: number;
}

export const runTranscript = (project_id: number, run_id: number, opts: { limit?: number; only_sidechain?: boolean } = {}) => {
  const q = new URLSearchParams();
  if (opts.limit) q.set("limit", String(opts.limit));
  if (opts.only_sidechain) q.set("only_sidechain", "true");
  const qs = q.toString() ? `?${q}` : "";
  return http<TranscriptResponse>(`/api/projects/${project_id}/runs/${run_id}/transcript${qs}`);
};
