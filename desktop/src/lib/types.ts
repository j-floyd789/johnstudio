// JohnStudio API types — kept loose where the backend returns dict[str, Any].

export interface HealthResponse {
  ok: boolean;
  version: string;
  service: string;
}

export interface WorkerInfo {
  name: string;
  provider: "terminal" | "claude" | "codex" | "gemini";
  role: string;
  command: string;
  can_edit: boolean;
  worktree: boolean;
  max_runtime_minutes: number;
  always_available: boolean;
  is_available: boolean;
}

export interface DoctorResponse {
  home: string;
  config_path: string;
  db_path: string;
  fts5: boolean;
  tools: Record<string, boolean>;
  workers: WorkerInfo[];
}

export interface Project {
  id: number;
  name: string;
  repo_path: string;
  base_branch: string;
}

export interface ProjectDetail extends Project {
  config: {
    name: string;
    repo_path: string;
    base_branch: string;
    test_commands: string[];
    stack: {
      languages: string[];
      frameworks: string[];
      package_managers: string[];
      detected_files: string[];
    };
    pinned_skills: string[];
  };
}

export interface TaskRow {
  id: number;
  task_number: number;
  title: string;
  description: string;
  status: string;
  base_branch: string;
  created_at: string;
  updated_at: string;
}

export interface RunStatus {
  // `id` and `task_id` are populated by orchestrator.status() and used by
  // the live-tree UI to re-seed run state when an unknown run_id event
  // arrives mid-stream (specialist spawned after the SSE snapshot).
  id?: number;
  task_id?: number;
  worker: string;
  status: string;
  branch: string | null;
  worktree: string | null;
  tmux_pane?: string | null;
  result_md_exists: boolean;
  done_md_exists: boolean;
}

export interface TaskStatus {
  task_id?: number;
  task_number: number;
  title: string;
  status: string;
  runs: RunStatus[];
}

export interface RunTaskRequest {
  task: string;
  stub_only?: boolean;
  dry_run?: boolean;
  workers?: string[];
  max_agents?: number;
  relevant_files?: string[];
}

export interface RunTaskResponse {
  task_db_id: number;
  task_number: number;
  task_folder: string;
  team: string[];
  session?: string | null;
  dry_run: boolean;
  launched?: Array<{ worker: string; pid: number | null; pane: string | null; worktree: string | null }>;
}

export interface ArtifactFile {
  name: string;
  path: string;
  bytes: number;
  content: string;
}

export interface CollectRunSummary {
  worker: string;
  result_present: boolean;
  done_present: boolean;
  files_changed: string[];
  tests: Array<{ command: string; exit_code: number; timeout?: boolean }>;
  protected_path_hits: string[];
  dangerous_command_hits: string[];
  approval_command_hits: string[];
}

export interface ReviewScore {
  worker_name: string;
  score: number;
  breakdown: Record<string, number>;
  flags: string[];
}

export interface ReviewResponse {
  task_number: number;
  scores: ReviewScore[];
  final_review_path: string;
  merge_plan_path: string;
  recommended: string | null;
}

export interface MergeResponse {
  merged?: boolean;
  tests_passed?: boolean;
  branch?: string;
  decision_path?: string;
  dry_run?: boolean;
  exit_code?: number;
  output?: string;
  test_results?: Array<{ command: string; exit_code: number }>;
  note?: string;
}

export interface SkillRow {
  skill_id: string;
  type: string;
  name: string;
  description: string | null;
  category: string | null;
  enabled: boolean;
  trust_level: string;
}

export interface SkillDetail extends SkillRow {
  metadata_json?: string;
  tags_json?: string;
  original_path?: string;
  distilled_path?: string;
  summary_path?: string;
  files?: {
    metadata_yaml: string;
    distilled_md: string;
    summary_md: string;
    original_md: string;
    source_json: string;
    score_json: string;
  };
}

export interface SkillSource {
  id: number;
  repo_url: string | null;
  local_path: string | null;
  status: string;
  last_scanned_at: string | null;
}

export interface RouteResult {
  skill_id: string;
  score: number;
  rationale: string;
  tokens: number;
}

export interface GraphEntity {
  entity_id: string;
  entity_type: string;
  name: string;
  path: string | null;
}

export interface GraphRelationship {
  from_entity_id: string;
  to_entity_id: string;
  relation_type: string;
  confidence: number;
}

export interface MemoryFile {
  path: string;
  bytes: number;
}

export interface SafetyReport {
  task_number: number;
  runs: Array<{
    worker: string;
    protected_path_hits: string[];
    dangerous_command_hits: string[];
    approval_command_hits: string[];
  }>;
}
