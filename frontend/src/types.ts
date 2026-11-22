export interface Project {
  id: string;
  name: string;
  workspace_path?: string | null;
  deleted_at?: string | null;
  python_environment?: PythonEnvironment;
}
export interface PythonEnvironment {
  name: string;
  python: string;
  prefix: string;
  version?: string;
  managed: boolean;
}
export interface EnvironmentSelection {
  mode: "default" | "existing" | "managed";
  python: string;
  name: string;
}
export interface EnvironmentSettings {
  default: PythonEnvironment;
  environments: PythonEnvironment[];
}
export type WorkspaceMode = "isolated" | "worktree";
export interface Session {
  id: string;
  project_id: string;
  title: string;
  workspace_mode?: WorkspaceMode;
  archived_at?: string | null;
  deleted_at?: string | null;
  summary?: string;
  pinned?: unknown;
  active_run_id?: string | null;
  active_status?: string | null;
  revision?: number;
  undo_run_id?: string | null;
  created_at?: string;
}
export interface Usage {
  context_tokens?: number;
  context_estimated?: boolean;
  tool_calls?: number;
  active_seconds?: number;
  rounds?: number;
  input_tokens?: number;
  output_tokens?: number;
  total_tokens?: number;
  cost?: number;
  estimated_cost?: number;
}
export interface PlanStep {
  id?: string;
  title?: string;
  description?: string;
  criterion_ids?: string[]; // 旧计划将关联保存在步骤上。
  status?: string;
}
export interface AcceptanceCriterion {
  id: string;
  description: string;
  step_ids?: string[];
}
export interface PlanEntryCheck {
  criterion_id: string;
  path: string;
  args: string[];
  stdin?: string;
  exit_code: number | "nonzero";
  stdout?: string | null;
  stdout_reference?: {
    function: string;
    inputs: (string | number)[][];
    decode?: ("value" | "json")[];
    result_path?: (string | number)[];
    render?: "value" | "text" | "line";
  } | null;
}
export interface Plan {
  version?: number;
  goal?: string;
  rationale?: string;
  changed_assumption?: string;
  change_scope?: "step" | "plan";
  steps?: PlanStep[];
  criteria?: AcceptanceCriterion[];
  entry_checks?: PlanEntryCheck[];
  retained_steps?: string[];
  replanned_steps?: string[];
  checkpoint_steps?: number[];
}
export interface RunLimits {
  max_output_tokens: number;
  max_rounds: number;
  max_tool_calls: number;
  run_timeout: number;
  llm_timeout: number;
  max_context_tokens: number;
  compression_threshold: number;
}
export interface ProjectRunSettings extends RunLimits {
  token_budget: number;
}
export interface RunReview {
  decision?: string;
  summary?: string;
  reason?: string;
  checks?: {
    criterion_id: string;
    passed?: boolean;
    reason?: string;
    evidence_ids?: string[];
  }[];
}

export interface Run {
  id: string;
  status: string;
  task: string;
  mode: "react" | "plan";
  created_at?: string;
  reply?: string | null;
  followups?: string[];
  token_budget?: number;
  limits?: RunLimits | null;
  permission_policy?: PermissionPolicy;
  usage?: Usage;
  result?: unknown;
  interrupt?: unknown;
  instructions?: { id: string; text: string }[];
  patch?: unknown;
  patch_available?: boolean;
  start_snapshot?: string;
  end_snapshot?: string | null;
  state?: {
    plan?: Plan;
    pending_plan?: Plan;
    criteria?: AcceptanceCriterion[];
    user_updates?: string[];
    applied_instruction_ids?: string[];
    usage?: Usage;
    [key: string]: unknown;
  };
}
export interface PermissionPolicy {
  write_scope: "none" | "project" | "all";
  approval_mode: "request" | "full_access";
}
export interface ToolApproval {
  request_id: string;
  decision: "approve" | "approve_similar" | "reject";
}
export interface PermissionRequest {
  kind: "tool_permission";
  request_id: string;
  tool: string;
  arguments: Record<string, unknown>;
  workdir: string;
  read_paths: string[];
  write_paths: string[];
  outside_paths: string[];
  reasons: string[];
  notice: string;
  similar_approval?: { description: string };
  script?: {
    path: string;
    content: string;
    truncated: boolean;
    sha256: string;
  };
}
export interface PlanApproval {
  plan_version: number;
  decision: "approve" | "changes" | "reject";
  feedback?: string;
}
export interface PlanningQuestion {
  id: string;
  question: string;
  reason: string;
  options: { id: string; label: string; description: string }[];
  recommended_option_id: string;
}
export interface PlanningRequest {
  kind: "plan_clarification";
  return_node?: string;
  request_id: string;
  questions: PlanningQuestion[];
  feedback?: string;
}
export interface PlanningAnswers {
  request_id: string;
  answers: { question_id: string; option_id: string; text: string }[];
}
export type RunControlArgs =
  | [action: "pause"]
  | [action: "cancel"]
  | [
      action: "resume",
      body: { answer?: unknown; token_budget?: number; limits?: RunLimits },
    ]
  | [
      action: "steer",
      body: {
        text: string;
        spec_update?: {
          goal?: string;
          criteria?: AcceptanceCriterion[];
        };
      },
    ]
  | [action: "approve", body: PlanApproval]
  | [action: "approve-tool", body: ToolApproval];
export interface Message {
  id?: string | number;
  role: string;
  content: unknown;
  created_at?: string;
  run_id?: string;
}
export interface RunEvent {
  id: string;
  type: string;
  data: Record<string, unknown>;
  created_at?: string;
}
export interface PageResult<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
  through?: string;
}
export interface Memory {
  updated_at?: string | null;
  id: string;
  key: string;
  content: string;
  keywords: string[];
  revision: number;
  session_id?: string | null;
}
export interface FileEntry {
  path: string;
  size?: number;
}
export interface FileContent {
  path: string;
  content: string;
  binary?: boolean;
  truncated?: boolean;
}
export type Resolution = "ours" | "theirs" | { content: string | null };
export interface MergeConflict {
  id: string;
  path: string;
  base?: string;
  ours?: string;
  theirs?: string;
  binary?: boolean;
}
export interface MemoryConflict {
  id: string;
  key: string;
  expected_revision: number;
  current_revision: number;
}
export interface ChangePreview {
  token: string;
  diff: string;
  conflicts: MergeConflict[];
  memory_conflicts?: MemoryConflict[];
  verification?: VerificationResult;
  no_file_changes?: boolean;
}
export interface RequiredCheck {
  id: string;
  kind: "python" | "pytest";
  path: string;
  args: string[];
  min_tests: number;
}
export interface VerificationPolicy {
  checks: RequiredCheck[];
  timeout_seconds: number;
}
export interface AcceptanceSuiteRecord {
  id: string;
  run_id?: string | null;
  scope: {
    from_step?: number;
    through_step?: number;
    phase: number;
    final?: boolean;
    plan_version?: number;
  };
  contract: {
    rationale?: string;
    cases?: {
      id: string;
      description: string;
      driver?: string;
      inputs?: unknown;
      reference?: string | null;
      probe?: unknown;
      assertions?: unknown[];
    }[];
  };
  source: string;
  active: boolean;
  status?: string;
  task_revision?: number;
  used_in_verification?: boolean | null;
  from_current_run?: boolean;
  source_session?: { id: string; title: string } | null;
  retirement_reason?: string;
}

export interface VerificationResult {
  id: string;
  snapshot: string;
  status: string;
  error?: string;
  suite_ids?: string[];
  scope?: {
    phase: number;
    from_step?: number;
    through_step?: number;
    final: boolean;
  };
  checks: {
    id: string;
    kind: string;
    path: string;
    status: string;
    stdout?: string;
    stderr?: string;
    error?: string;
    tests_collected?: number;
    tests_passed?: number;
    duration?: number;
    exit_code?: number;
    timed_out?: boolean;
    origin?: string;
  }[];
}
export interface MCPServer {
  id: string;
  name: string;
  transport: "stdio" | "http";
  command?: string;
  args?: string[];
  url?: string;
  env?: Record<string, string>;
  headers?: Record<string, string>;
  enabled: boolean;
}
export interface Skill {
  name: string;
  description: string;
  enabled: boolean;
  path: string;
}

export const statusLabels: Record<string, string> = {
  queued: "排队中",
  running: "执行中",
  paused: "已暂停",
  waiting_approval: "等待计划审批",
  awaiting_approval: "等待计划审批",
  awaiting_tool_approval: "等待操作审批",
  awaiting_input: "等待你补充",
  waiting_human: "等待你确认",
  interrupted: "等待你确认",
  completed: "已完成",
  succeeded: "已完成",
  blocked: "受阻",
  failed: "执行失败",
  crashed: "崩溃后已恢复",
  cancelled: "已取消",
  rolled_back: "已撤回",
  replaced: "已被替换",
  limit_reached: "预算已用尽",
  budget_exceeded: "预算已用尽",
};
export const activeStatuses = new Set(["queued", "running"]);
export const waitingStatuses = new Set([
  "paused",
  "awaiting_approval",
  "awaiting_tool_approval",
  "awaiting_input",
]);
export const terminalStatuses = new Set([
  "completed",
  "succeeded",
  "blocked",
  "failed",
  "crashed",
  "cancelled",
  "rolled_back",
  "replaced",
  "limit_reached",
  "budget_exceeded",
]);
export function display(value: unknown): string {
  return value == null
    ? ""
    : typeof value === "string"
      ? value
      : JSON.stringify(value, null, 2);
}

// 旧版收尾消息在验收结果与模型原文之间额外插入过这个标题。
export function displaySummary(text: string): string {
  return text.replace(
    /^((?:会话版本已通过验收，项目文件待写回。[\s\S]*?\r?\n\r?\n)?最终自动验收已通过：[^\r\n]*\r?\n\r?\n)验收前的实现说明\r?\n/,
    "$1",
  );
}
