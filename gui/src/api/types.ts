export type SchedulerStatus = {
  auto_run: boolean;
  running: boolean;
  paused: boolean;
  task_id: string | null;
  reason: string | null;
  last_result: unknown[];
  last_error: string | null;
  started_at: number | null;
  finished_at: number | null;
  default_max_quanta: number | null;
};

export type RuntimeProcess = {
  pid: string;
  parent_pid: string | null;
  image_id: string;
  llm_profile_id: string;
  status: string;
  goal_oid: string | null;
  checkpoint_head: string | null;
  working_directory: string;
  status_message: string | null;
  loaded_skills: Record<string, unknown>;
  tool_table: Record<string, string>;
  capabilities: string[];
  terminal: boolean;
  unread_message_count: number;
  interrupt_count: number;
  messages: ProcessMessage[];
  llm_call_count: number;
  token_total: number;
  resource_budget?: Record<string, unknown>;
  resource_usage?: Record<string, unknown>;
  resource_remaining?: Record<string, unknown>;
};

export type ProcessMessage = {
  message_id: string;
  sender: string;
  recipient_pid: string;
  kind: "normal" | "interrupt";
  subject: string;
  body: string;
  channel: string;
  status: string;
  created_at: string;
  payload: Record<string, unknown>;
};

export type HumanRequest = {
  request_id: string;
  pid: string;
  human: string;
  payload: Record<string, unknown>;
  status: string;
  decision: Record<string, unknown> | null;
  blocking: boolean;
  created_at: string;
  updated_at: string;
};

export type AuditRecord = {
  record_id: string;
  timestamp: string;
  actor: string;
  action: string;
  target: string | null;
  decision: Record<string, unknown> | null;
  capability_refs: string[];
};

export type RuntimeEvent = {
  event_id: string;
  type: string;
  source: string;
  target: string | null;
  payload: Record<string, unknown>;
  priority: string;
  created_at: string;
};

export type LlmCall = {
  call_id: string;
  pid: string | null;
  image_id: string | null;
  purpose: string;
  status: string;
  api: string | null;
  model: string | null;
  request_options: Record<string, unknown>;
  response_content: string;
  tool_calls: unknown[];
  usage: Record<string, unknown>;
  reasoning: unknown;
  error: string | null;
  created_at: string;
  completed_at: string | null;
};

export type ToolSummary = {
  tool_id: string;
  name: string;
  scope: string;
  description: string;
  tags: string[];
  policy: Record<string, unknown>;
  ephemeral: boolean;
};

export type WorkflowRunResult = {
  pid: string;
  image: string;
  tool: string;
  ok: boolean;
  status: string;
  call_id: string | null;
  tool_id: string | null;
  result_oid: string | null;
  payload: unknown;
  error: string | null;
  waiting_human: boolean;
  request_id: string | null;
  waiting_process: boolean;
  child_pid: string | null;
  waiting_message: boolean;
  filters: Record<string, unknown> | null;
};

export type ObjectTask = {
  task_id: string;
  owner_oid: string;
  creator_pid: string;
  runner_pid: string | null;
  tool: string;
  tool_id: string | null;
  status: string;
  notification: Record<string, unknown>;
  owner_watch: Record<string, unknown>;
  result_oid: string | null;
  error: string | null;
  wait: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  completed_at: string | null;
};

export type ImageSummary = {
  image_id: string;
  name: string;
  version: string;
  boot_kind: string;
  default_tools: string[];
  default_skills: string[];
  required_capabilities_count: number;
  required_modules_count: number;
  [key: string]: unknown;
};

export type ImageInspectResult = {
  image: {
    image_id: string;
    name: string;
    version: string;
    default_tools: string[];
    default_skills: string[];
    required_capabilities: Record<string, unknown>[];
    required_modules: Record<string, unknown>[];
    boot: Record<string, unknown>;
    metadata: Record<string, unknown>;
    [key: string]: unknown;
  };
  registry: Record<string, unknown>;
  artifact: Record<string, unknown> | null;
};

export type ImageMutationResult = {
  image_id: string;
  name: string;
  version: string;
  replaced: boolean;
  boot: Record<string, unknown>;
  default_tools?: string[];
  default_skills?: string[];
  package_sha256?: string;
  package_jit_tools?: string[];
  required_capabilities_count: number;
  required_modules_count: number;
  source?: string | null;
};

export type RuntimeSnapshot = {
  db: string;
  scheduler: SchedulerStatus;
  processes: RuntimeProcess[];
  human_requests: HumanRequest[];
  events: RuntimeEvent[];
  audit: AuditRecord[];
  llm_calls: LlmCall[];
  object_tasks: ObjectTask[];
  tools: ToolSummary[];
  images: ImageSummary[];
  skills: Record<string, unknown>[];
  jsonrpc_endpoints: Record<string, unknown>[];
  modules: Record<string, unknown>[];
};

export type GuiConnection = {
  url: string;
  token: string;
  db: string;
};

export type ImagePackageFileValue = string | { base64: string };

export type ImagePackageFile = {
  path: string;
  name: string;
  manifest: string;
  files: Record<string, ImagePackageFileValue>;
};

export type SseMessage = {
  id: string;
  event: string;
  data: unknown;
};
