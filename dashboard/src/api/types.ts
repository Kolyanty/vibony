// Protocol types — a manual projection of docs/protocol/*.md onto TypeScript.
// Source of truth is the backend (Pydantic schemas in backend/vibony_backend/schemas.py
// and events.md / session.md / ws-contract.md). Desync is caught by the reducer tests
// on fixtures from events.md and by the live smoke test.

export type SessionStatus = 'pending' | 'active' | 'completed';

export interface AiConfig {
  primary_model: string;
  fallback_models: string[];
}

export type SessionMode = 'live' | 'async';

export interface SessionConfig {
  session_code: string;
  session_status: SessionStatus;
  protocol_version: number;
  ws_token: string;
  task_id: string;
  task_dir_name: string;
  ai_config: AiConfig;
  bundle_sha256: string | null;
  created_at: string;
  expires_at: string;
  // Async mode. Backend always sends `mode` (defaults to 'live');
  // optional here so legacy fixtures and replay configs still typecheck.
  mode?: SessionMode;
  time_limit_seconds?: number | null;
  candidate_token?: string;
}

// -------- AI assessment --------
// Mirrors backend assessment/schemas.py (SessionAssessmentResponse / AssessmentFinding).
// 'not_started' is synthesized by the REST layer when no row exists yet.

export type AssessmentStatus =
  | 'not_started'
  | 'pending'
  | 'running'
  | 'ready'
  | 'failed';
export type AssessmentFindingCategory = 'strength' | 'gap' | 'pattern';
export type AssessmentSeverity = 'blocking' | 'degrading' | 'info';

export interface AssessmentFinding {
  category: AssessmentFindingCategory;
  title: string;
  detail: string;
  severity: AssessmentSeverity | null;
  // Citation into the session event log.
  event_seq: number | null;
}

export interface SessionAssessment {
  status: AssessmentStatus;
  summary_md: string | null;
  findings: AssessmentFinding[];
  model: string | null;
  error: string | null;
  created_at: string | null;
  updated_at: string | null;
}

// -------- Event envelope (docs/protocol/events.md §Envelope) --------

export type EventType =
  | 'session_start'
  | 'session_end'
  | 'user_message'
  | 'assistant_message'
  | 'tool_call'
  | 'tool_result'
  | 'diff_proposed'
  | 'diff_accepted'
  | 'diff_rejected'
  | 'file_changed'
  | 'command_run'
  | 'command_output'
  | 'command_finished'
  | 'model_switched'
  | 'sandbox_reset'
  | 'sandbox_restored';

export interface EventEnvelopeBase<T extends EventType, P> {
  id: string;
  seq: number;
  session_code: string;
  timestamp: string;
  type: T;
  payload: P;
}

export interface UsageInfo {
  input_tokens?: number;
  output_tokens?: number;
}

export interface UserMessagePayload {
  text: string;
}

export interface AssistantMessagePayload {
  text: string;
  usage?: UsageInfo;
}

// Permission gate decision for Bash tool_call
export type PermissionDecision =
  | 'auto_allow'
  | 'user_allow'
  | 'user_deny'
  | 'policy_deny';

export interface ToolCallPayload {
  tool_call_id: string;
  tool_name: string;
  input: Record<string, unknown>;
  permission_decision?: PermissionDecision;
  prompted?: boolean;
}

export interface ToolResultPayload {
  tool_call_id: string;
  output: string;
  is_error: boolean;
  // Optional tool-specific fields (see docs/protocol/events.md):
  // Edit/Write — unified diff old → new. Empty/absent on a no-op.
  diff?: string;
  // Bash — stdout/stderr and metrics on top of the output summary.
  stdout?: string;
  stderr?: string;
  exit_code?: number;
  duration_ms?: number;
  timed_out?: boolean;
}

export interface DiffProposedPayload {
  proposal_id: string;
  file_path: string;
  diff: string;
}

export interface DiffAcceptedPayload {
  proposal_id: string;
}

export interface DiffRejectedPayload {
  proposal_id: string;
  reason?: string;
}

export interface FileChangedPayload {
  file_path: string;
  // null when the backend does not compute a diff (e.g. sandbox Write/Edit).
  diff: string | null;
  source: 'agent' | 'manual' | 'baseline';
  // Additive: the full content after the change.
  //   string — UTF-8 text;
  //   null — file deleted;
  //   undefined — binary file or a legacy CLI event.
  content?: string | null;
  // Additive: the operation type.
  //   'modified' — created or overwritten;
  //   'deleted'  — removed;
  //   'created'  — directory created (is_dir=true) or a baseline file at session start (source='baseline').
  //   undefined  — a legacy CLI event.
  op?: 'modified' | 'deleted' | 'created';
  // Additive: true if this is a directory (mkdir/rmdir).
  is_dir?: boolean;
}

export interface CommandRunPayload {
  command_id: string;
  command: string;
  cwd: string;
  // Additive: the candidate's terminal size at launch.
  // Omitted if the CLI couldn't determine it (non-TTY) or on a legacy CLI event.
  cols?: number;
  rows?: number;
}

export interface CommandOutputPayload {
  command_id: string;
  stream: 'stdout' | 'stderr';
  data: string;
}

export interface CommandFinishedPayload {
  command_id: string;
  exit_code: number;
  duration_ms: number;
}

export interface SessionStartPayload {
  task_id: string;
  ai_config: { model: string };
  protocol_version: number;
  cli_version: string;
}

export interface SessionEndPayload {
  // `submitted` — candidate ended an async session via session_submit;
  // `timeout` (async) — server-side countdown hit the deadline.
  reason: 'interviewer' | 'timeout' | 'candidate_quit' | 'error' | 'submitted';
  details?: string;
}

export interface ModelSwitchedPayload {
  from_model: string;
  to_model: string;
  reason: 'provider_5xx' | 'provider_timeout' | 'quota_exhausted';
}

export interface SandboxResetPayload {
  // Present only on the post-expiry auto-reset. Absent for an interviewer-triggered
  // manual reset — the candidate UI maps an absent reason to a "manual reset" notice.
  reason?: 'expired';
}

export interface SandboxRestoredPayload {
  file_count: number;
  deleted_count: number;
}

export type EventEnvelope =
  | EventEnvelopeBase<'user_message', UserMessagePayload>
  | EventEnvelopeBase<'assistant_message', AssistantMessagePayload>
  | EventEnvelopeBase<'tool_call', ToolCallPayload>
  | EventEnvelopeBase<'tool_result', ToolResultPayload>
  | EventEnvelopeBase<'diff_proposed', DiffProposedPayload>
  | EventEnvelopeBase<'diff_accepted', DiffAcceptedPayload>
  | EventEnvelopeBase<'diff_rejected', DiffRejectedPayload>
  | EventEnvelopeBase<'file_changed', FileChangedPayload>
  | EventEnvelopeBase<'command_run', CommandRunPayload>
  | EventEnvelopeBase<'command_output', CommandOutputPayload>
  | EventEnvelopeBase<'command_finished', CommandFinishedPayload>
  | EventEnvelopeBase<'session_start', SessionStartPayload>
  | EventEnvelopeBase<'session_end', SessionEndPayload>
  | EventEnvelopeBase<'model_switched', ModelSwitchedPayload>
  | EventEnvelopeBase<'sandbox_reset', SandboxResetPayload>
  | EventEnvelopeBase<'sandbox_restored', SandboxRestoredPayload>;

// -------- WS frames (docs/protocol/ws-contract.md §Frame kinds) --------

export interface HelloFrame {
  kind: 'hello';
  protocol_version: number;
  session_status: SessionStatus;
  last_seq: number;
}

export interface EventFrame {
  kind: 'event';
  event: EventEnvelope;
}

export interface ErrorFrame {
  kind: 'error';
  id?: string;
  code: string;
  message: string;
}

export interface FsResponseFrame {
  kind: 'fs_response';
  id: string;
  op?: string;
  path?: string;
  content?: string;
  entries?: FileTreeEntry[];
  error?: string;
}

// Bash confirm round-trip (web candidate only)
export type BashConfirmChoice = 'allow_once' | 'allow_prefix' | 'deny';

export interface BashConfirmRequestFrame {
  kind: 'bash_confirm_request';
  cmd_id: string;
  cmd: string;
  prefix: string | null;
  choices: BashConfirmChoice[];
}

// Session presence
export interface Attendee {
  name: string;
  role?: string;
  is_active: boolean;
  first_seen: string;
  last_seen: string;
}

export interface SessionJoinOkFrame {
  kind: 'session_join_ok';
}

export interface SessionJoinErrorFrame {
  kind: 'session_join_error';
  reason: 'name_taken' | 'invalid_name';
}

export interface PresenceUpdatedFrame {
  kind: 'presence_updated';
  attendees: Attendee[];
}

// Advisory async countdown frame (backend → clients, async sessions only).
// Transient — never persisted, not replayed on reconnect. Server is authoritative.
export interface TimerTickFrame {
  kind: 'timer_tick';
  remaining_seconds: number;
}

export interface PresenceAlertFrame {
  kind: 'presence_alert';
  reason: string;
}

// Advisory, non-persisted hint that an agent turn is in flight, so the
// candidate sees "AI is working…" for the whole turn (incl. the silent gap before
// the next LLM call) instead of a tool-heavy turn looking idle.
export interface AgentStatusFrame {
  kind: 'agent_status';
  state: 'working' | 'idle';
}

export type ServerFrame =
  | HelloFrame
  | EventFrame
  | ErrorFrame
  | FsResponseFrame
  | BashConfirmRequestFrame
  | SessionJoinOkFrame
  | SessionJoinErrorFrame
  | PresenceUpdatedFrame
  | PresenceAlertFrame
  | TimerTickFrame
  | AgentStatusFrame;

// -------- REST /files --------

export interface FileTreeEntry {
  path: string;
  kind: 'file' | 'dir';
  // omitted/0 for directories
  size?: number;
}

export interface FileTreeResponse {
  entries: FileTreeEntry[];
}
