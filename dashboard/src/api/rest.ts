// REST client for interviewer-facing endpoints.
// All calls use credentials:'include' (cookie auth).
// Candidate-facing paths (bundle, replay) remain token-based.
//
// User-facing messages are localised in the UI layer: callers receive a
// `reason` (stable en-snake_case machine-id) and call
// `t('error.message.' + reason)` themselves.

import { AuthError } from './auth';
import type { MembershipInfo } from './auth';
import type {
  FileTreeResponse,
  SessionAssessment,
  SessionConfig,
  SessionMode,
} from './types';

/**
 * Generic API error carrying the backend's HTTP status + machine `reason`
 * (snake_case) + human `message`, so the UI can localize a known failure via
 * the shared `reason→i18n` map (see `lib/apiError.ts`) instead of leaking a raw
 * `HTTP 4xx` string. Thrown by the mutation endpoints whose error
 * is surfaced directly to the user (create-session/workspace/share/invite).
 */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly reason?: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

/** Build an `ApiError` from a non-ok response, parsing `{detail:{reason,message}}`,
 * a string `detail`, or a FastAPI 422 validation array. */
async function readApiError(response: Response): Promise<ApiError> {
  const detail = await safeReadDetail(response);
  return new ApiError(detail.message ?? `HTTP ${response.status}`, response.status, detail.reason);
}

export type SessionConfigErrorReason =
  | 'unauthorized'
  | 'not_found'
  | 'completed'
  | 'network'
  | 'invalid_response'
  | 'unknown';

export class SessionConfigError extends Error {
  reason: SessionConfigErrorReason;
  status?: number;

  constructor(reason: SessionConfigErrorReason, message: string, status?: number) {
    super(message);
    this.name = 'SessionConfigError';
    this.reason = reason;
    this.status = status;
  }
}

export interface GetSessionConfigOptions {
  signal?: AbortSignal;
  baseUrl?: string;
}

export async function getSessionConfig(
  code: string,
  options: GetSessionConfigOptions = {},
): Promise<SessionConfig> {
  const base = options.baseUrl ?? '';
  const url = `${base}/sessions/${encodeURIComponent(code)}`;

  let response: Response;
  try {
    response = await fetch(url, {
      method: 'GET',
      headers: { Accept: 'application/json' },
      credentials: 'include',
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new SessionConfigError(
      'network',
      `Could not reach backend: ${err instanceof Error ? err.message : String(err)}`,
    );
  }

  if (response.ok) {
    let body: unknown;
    try {
      body = await response.json();
    } catch (err) {
      throw new SessionConfigError(
        'invalid_response',
        `Backend returned non-JSON: ${err instanceof Error ? err.message : String(err)}`,
        response.status,
      );
    }
    if (!isSessionConfig(body)) {
      throw new SessionConfigError(
        'invalid_response',
        'Backend response does not match SessionConfig',
        response.status,
      );
    }
    return body;
  }

  const httpReason = mapStatusToReason(response.status);
  const detail = await safeReadDetail(response);
  const reason = parseConfigReason(detail.reason) ?? httpReason;
  const message = detail.message ?? defaultMessage(reason);
  throw new SessionConfigError(reason, message, response.status);
}

function mapStatusToReason(status: number): SessionConfigErrorReason {
  if (status === 401 || status === 403) return 'unauthorized';
  if (status === 404) return 'not_found';
  if (status === 410) return 'completed';
  return 'unknown';
}

function parseConfigReason(s: string | undefined): SessionConfigErrorReason | undefined {
  if (s === 'unauthorized' || s === 'not_found' || s === 'completed') return s;
  return undefined;
}

function parseEndReason(s: string | undefined): EndSessionErrorReason | undefined {
  if (s === 'unauthorized' || s === 'not_found') return s;
  return undefined;
}

function defaultMessage(reason: SessionConfigErrorReason): string {
  switch (reason) {
    case 'unauthorized':
      return 'Not authenticated';
    case 'not_found':
      return 'Session not found';
    case 'completed':
      return 'Session ended';
    case 'network':
      return 'No connection to backend';
    case 'invalid_response':
      return 'Invalid response from backend';
    default:
      return 'Unknown backend error';
  }
}

interface ParsedDetail {
  reason?: string;
  message?: string;
}

async function safeReadDetail(response: Response): Promise<ParsedDetail> {
  try {
    const body = await response.json();
    if (typeof body === 'object' && body !== null && 'detail' in body) {
      const detail = (body as { detail: unknown }).detail;
      if (typeof detail === 'string') return { message: detail };
      if (Array.isArray(detail)) {
        // FastAPI 422 validation: [{loc, msg, type}, ...] — join the messages.
        const message = detail
          .map((e) => (e && typeof e === 'object' && 'msg' in e ? String((e as { msg: unknown }).msg) : ''))
          .filter(Boolean)
          .join('; ');
        return { message: message || undefined };
      }
      if (typeof detail === 'object' && detail !== null) {
        const obj = detail as Record<string, unknown>;
        // Surface the backend `message` too — previously dropped, so a
        // `{reason, message}` body lost its human text and the UI fell back to a code.
        return {
          reason: typeof obj.reason === 'string' ? obj.reason : undefined,
          message: typeof obj.message === 'string' ? obj.message : undefined,
        };
      }
    }
  } catch {
    // non-JSON — return default message
  }
  return {};
}

// -------- POST /sessions/{code}/end --------

export type EndSessionErrorReason =
  | 'unauthorized'
  | 'not_found'
  | 'network'
  | 'unknown';

export class EndSessionError extends Error {
  reason: EndSessionErrorReason;
  status?: number;

  constructor(reason: EndSessionErrorReason, message: string, status?: number) {
    super(message);
    this.name = 'EndSessionError';
    this.reason = reason;
    this.status = status;
  }
}

export interface EndSessionOptions {
  signal?: AbortSignal;
  baseUrl?: string;
}

export async function endSession(
  code: string,
  options: EndSessionOptions = {},
): Promise<SessionConfig | null> {
  const base = options.baseUrl ?? '';
  const url = `${base}/sessions/${encodeURIComponent(code)}/end`;

  let response: Response;
  try {
    response = await fetch(url, {
      method: 'POST',
      headers: { Accept: 'application/json' },
      credentials: 'include',
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new EndSessionError(
      'network',
      `Could not reach backend: ${err instanceof Error ? err.message : String(err)}`,
    );
  }

  if (response.ok) {
    // A 2xx means the session ended. The body is the refreshed SessionConfig,
    // but an empty/non-JSON 2xx (e.g. 204, proxy quirk) still means success —
    // don't surface a spurious error. The WS `session_end` frame is the
    // authoritative signal; the caller ignores the returned config.
    const text = await response.text();
    if (!text.trim()) return null;
    try {
      return JSON.parse(text) as SessionConfig;
    } catch {
      return null;
    }
  }

  let httpReason: EndSessionErrorReason;
  if (response.status === 401 || response.status === 403) httpReason = 'unauthorized';
  else if (response.status === 404) httpReason = 'not_found';
  else httpReason = 'unknown';

  const detail = await safeReadDetail(response);
  const reason = parseEndReason(detail.reason) ?? httpReason;
  const message = detail.message ?? `Backend returned status ${response.status}`;
  throw new EndSessionError(reason, message, response.status);
}

// -------- GET /sessions/{code}/files --------

export interface GetFilesOptions {
  signal?: AbortSignal;
  baseUrl?: string;
}

export async function getFileTree(
  code: string,
  options: GetFilesOptions = {},
): Promise<FileTreeResponse> {
  const base = options.baseUrl ?? '';
  const url = `${base}/sessions/${encodeURIComponent(code)}/files`;
  let response: Response;
  try {
    response = await fetch(url, {
      method: 'GET',
      headers: { Accept: 'application/json' },
      credentials: 'include',
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new Error(`Could not reach backend: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return (await response.json()) as FileTreeResponse;
}

// -------- GET /sessions/{code}/files/content?path=… --------

export async function getFileContent(
  code: string,
  path: string,
  options: GetFilesOptions = {},
): Promise<string> {
  const base = options.baseUrl ?? '';
  const url = `${base}/sessions/${encodeURIComponent(code)}/files/content?path=${encodeURIComponent(path)}`;
  let response: Response;
  try {
    response = await fetch(url, {
      method: 'GET',
      headers: { Accept: 'application/json' },
      credentials: 'include',
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new Error(`Could not reach backend: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.text();
}

// -------- POST /sessions/{code}/share --------

export interface ShareResponse {
  share_token: string;
  // When the replay link stops working (stamped at mint).
  expires_at?: string | null;
}

export async function postShare(
  code: string,
  options: { signal?: AbortSignal; baseUrl?: string } = {},
): Promise<ShareResponse> {
  const base = options.baseUrl ?? '';
  const url = `${base}/sessions/${encodeURIComponent(code)}/share`;
  let response: Response;
  try {
    response = await fetch(url, {
      method: 'POST',
      headers: { Accept: 'application/json' },
      credentials: 'include',
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new Error(`Could not reach backend: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (!response.ok) throw await readApiError(response);
  return (await response.json()) as ShareResponse;
}

// -------- POST /sessions/{code}/sandbox/reset --------

export async function resetSandbox(
  code: string,
  options: { signal?: AbortSignal } = {},
): Promise<void> {
  const url = `/sessions/${encodeURIComponent(code)}/sandbox/reset`;
  let response: Response;
  try {
    response = await fetch(url, {
      method: 'POST',
      headers: { Accept: 'application/json' },
      credentials: 'include',
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new Error(`Could not reach backend: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(
      (detail as { detail?: { message?: string } }).detail?.message ??
        `HTTP ${response.status}`,
    );
  }
}

// -------- AI assessment --------

export interface AssessmentOptions {
  signal?: AbortSignal;
  baseUrl?: string;
}

export async function getAssessment(
  code: string,
  options: AssessmentOptions = {},
): Promise<SessionAssessment> {
  const base = options.baseUrl ?? '';
  const url = `${base}/sessions/${encodeURIComponent(code)}/assessment`;
  let response: Response;
  try {
    response = await fetch(url, {
      method: 'GET',
      headers: { Accept: 'application/json' },
      credentials: 'include',
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new Error(`Could not reach backend: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return (await response.json()) as SessionAssessment;
}

export async function regenerateAssessment(
  code: string,
  options: AssessmentOptions = {},
): Promise<SessionAssessment> {
  const base = options.baseUrl ?? '';
  const url = `${base}/sessions/${encodeURIComponent(code)}/assessment/regenerate`;
  let response: Response;
  try {
    response = await fetch(url, {
      method: 'POST',
      headers: { Accept: 'application/json' },
      credentials: 'include',
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new Error(`Could not reach backend: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return (await response.json()) as SessionAssessment;
}

// -------- GET /sessions/{code}/replay --------

export type ReplayConfigErrorReason =
  | 'unauthorized'
  | 'not_found'
  | 'not_completed'
  | 'network'
  | 'invalid_response'
  | 'unknown';

export class ReplayConfigError extends Error {
  reason: ReplayConfigErrorReason;
  status?: number;

  constructor(reason: ReplayConfigErrorReason, message: string, status?: number) {
    super(message);
    this.name = 'ReplayConfigError';
    this.reason = reason;
    this.status = status;
  }
}

export async function getReplayConfig(
  code: string,
  shareToken: string,
  options: { signal?: AbortSignal; baseUrl?: string } = {},
): Promise<SessionConfig> {
  const base = options.baseUrl ?? '';
  const url = `${base}/sessions/${encodeURIComponent(code)}/replay?share_token=${encodeURIComponent(shareToken)}`;
  let response: Response;
  try {
    response = await fetch(url, {
      method: 'GET',
      headers: { Accept: 'application/json' },
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new ReplayConfigError(
      'network',
      `Could not reach backend: ${err instanceof Error ? err.message : String(err)}`,
    );
  }

  if (response.ok) {
    let body: unknown;
    try {
      body = await response.json();
    } catch (err) {
      throw new ReplayConfigError(
        'invalid_response',
        `Backend returned non-JSON: ${err instanceof Error ? err.message : String(err)}`,
        response.status,
      );
    }
    if (!isSessionConfig(body)) {
      throw new ReplayConfigError(
        'invalid_response',
        'Backend response does not match SessionConfig',
        response.status,
      );
    }
    return body;
  }

  const detail = await safeReadDetail(response);
  const reason = parseReplayReason(detail.reason) ?? mapStatusToReplayReason(response.status);
  throw new ReplayConfigError(reason, detail.message ?? `HTTP ${response.status}`, response.status);
}

function mapStatusToReplayReason(status: number): ReplayConfigErrorReason {
  if (status === 401 || status === 403) return 'unauthorized';
  if (status === 404) return 'not_found';
  if (status === 410) return 'not_completed';
  return 'unknown';
}

function parseReplayReason(s: string | undefined): ReplayConfigErrorReason | undefined {
  if (s === 'unauthorized' || s === 'not_found' || s === 'not_completed') return s;
  return undefined;
}

// -------- GET /sessions/{code}/events --------

export async function getEvents(
  code: string,
  options: { signal?: AbortSignal; baseUrl?: string; shareToken?: string } = {},
): Promise<import('./types').EventEnvelope[]> {
  const base = options.baseUrl ?? '';
  const qs = options.shareToken
    ? `since=0&share_token=${encodeURIComponent(options.shareToken)}`
    : 'since=0';
  const url = `${base}/sessions/${encodeURIComponent(code)}/events?${qs}`;
  let response: Response;
  try {
    response = await fetch(url, {
      method: 'GET',
      headers: { Accept: 'application/json' },
      credentials: 'include',
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new Error(`Could not reach backend: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return (await response.json()) as import('./types').EventEnvelope[];
}

// -------- POST /sessions --------

export interface SessionCreatePayload {
  task_id: string;
  ai_config?: { primary_model?: string; fallback_models?: string[] };
  workspace_id?: string | null;
  // Async mode + server-side deadline. `time_limit_seconds` is required
  // by the backend when mode==='async' (300–14400) and rejected for live.
  mode?: SessionMode;
  time_limit_seconds?: number | null;
}

export interface SessionCreatedResponse {
  session_code: string;
  session_status: string;
  protocol_version: number;
  ws_token: string;
  task_id: string;
  task_dir_name: string;
  ai_config: { primary_model: string; fallback_models: string[] };
  bundle_sha256: string | null;
  created_at: string;
  expires_at: string;
  candidate_token: string;
  candidate_url: string | null;
  mode: SessionMode;
  time_limit_seconds: number | null;
}

export async function createSession(
  payload: SessionCreatePayload,
  options: { signal?: AbortSignal; baseUrl?: string } = {},
): Promise<SessionCreatedResponse> {
  const base = options.baseUrl ?? '';
  let response: Response;
  try {
    response = await fetch(`${base}/sessions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      credentials: 'include',
      body: JSON.stringify(payload),
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new Error(`Could not reach backend: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (!response.ok) throw await readApiError(response);
  return (await response.json()) as SessionCreatedResponse;
}

// -------- GET /sessions --------

export interface SessionListItem {
  session_code: string;
  session_status: 'pending' | 'active' | 'completed';
  task_id: string;
  task_dir_name: string;
  workspace_id: string | null;
  created_at: string;
  expires_at: string;
  // The backend always sends `mode` (defaults to 'live'); optional so
  // older rows / fixtures without it still typecheck.
  mode?: SessionMode;
  time_limit_seconds?: number | null;
}

export async function listSessions(
  options: { signal?: AbortSignal; baseUrl?: string } = {},
): Promise<SessionListItem[]> {
  const base = options.baseUrl ?? '';
  let response: Response;
  try {
    response = await fetch(`${base}/sessions`, {
      method: 'GET',
      headers: { Accept: 'application/json' },
      credentials: 'include',
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new Error(`Could not reach backend: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const body = (await response.json()) as { sessions: SessionListItem[] };
  return body.sessions;
}

// -------- Tasks --------

export type TaskVisibility = 'private' | 'workspace' | 'public';
// What a user may set via the API (public is template-only).
export type EditableTaskVisibility = 'private' | 'workspace';

export interface TaskStarterFile {
  path: string;
  content: string;
}

export interface TaskListItem {
  id: string;
  company_id: string;
  slug: string;
  title: string;
  runtime_image: string;
  visibility: TaskVisibility;
  is_template: boolean;
  can_edit: boolean;
  is_favorite: boolean;
  created_at: string;
  updated_at: string;
}

export interface TaskDetail extends TaskListItem {
  created_by: string | null;
  description_md: string;
  starter_files: TaskStarterFile[];
  weakness: Record<string, unknown>;
  spec: Record<string, unknown>;
}

export interface TaskCreatePayload {
  slug: string;
  title: string;
  description_md?: string;
  starter_files?: TaskStarterFile[];
  runtime_image: string;
  weakness?: Record<string, unknown>;
  spec?: Record<string, unknown>;
  visibility?: EditableTaskVisibility;
  workspace_id?: string | null;
}

export type TaskUpdatePayload = Partial<Omit<TaskCreatePayload, 'slug' | 'workspace_id'>>;

/** Carries the backend's HTTP status + machine `reason` so the editor can map
 * 409 (slug taken) / 422 (validation) / 403 (read-only) to a localized message. */
export class TaskApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly reason?: string,
  ) {
    super(message);
    this.name = 'TaskApiError';
  }
}

async function taskError(response: Response): Promise<TaskApiError> {
  let message = `HTTP ${response.status}`;
  let reason: string | undefined;
  try {
    const body = (await response.json()) as { detail?: unknown };
    const detail = body?.detail;
    if (typeof detail === 'string') {
      message = detail;
    } else if (Array.isArray(detail)) {
      // FastAPI 422: [{loc, msg, type}, ...] — surface the messages.
      message = detail
        .map((e) => (e && typeof e === 'object' && 'msg' in e ? String((e as { msg: unknown }).msg) : ''))
        .filter(Boolean)
        .join('; ') || message;
    } else if (detail && typeof detail === 'object' && 'reason' in detail) {
      reason = String((detail as { reason: unknown }).reason);
      message = reason;
    }
  } catch {
    // non-JSON body — keep the HTTP status message
  }
  return new TaskApiError(message, response.status, reason);
}

export async function listTasks(
  options: { signal?: AbortSignal; baseUrl?: string; q?: string } = {},
): Promise<TaskListItem[]> {
  const base = options.baseUrl ?? '';
  const term = options.q?.trim();
  const qs = term ? `?q=${encodeURIComponent(term)}` : '';
  let response: Response;
  try {
    response = await fetch(`${base}/tasks${qs}`, {
      headers: { Accept: 'application/json' },
      credentials: 'include',
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new Error(`Could not reach backend: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (!response.ok) throw await taskError(response);
  const body = (await response.json()) as { tasks?: TaskListItem[] };
  return body.tasks ?? [];
}

export async function getTask(
  id: string,
  options: { signal?: AbortSignal; baseUrl?: string } = {},
): Promise<TaskDetail> {
  const base = options.baseUrl ?? '';
  let response: Response;
  try {
    response = await fetch(`${base}/tasks/${encodeURIComponent(id)}`, {
      headers: { Accept: 'application/json' },
      credentials: 'include',
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new Error(`Could not reach backend: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (!response.ok) throw await taskError(response);
  return (await response.json()) as TaskDetail;
}

export async function createTask(
  payload: TaskCreatePayload,
  options: { signal?: AbortSignal; baseUrl?: string } = {},
): Promise<TaskDetail> {
  const base = options.baseUrl ?? '';
  let response: Response;
  try {
    response = await fetch(`${base}/tasks`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      credentials: 'include',
      body: JSON.stringify(payload),
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new Error(`Could not reach backend: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (!response.ok) throw await taskError(response);
  return (await response.json()) as TaskDetail;
}

export async function updateTask(
  id: string,
  payload: TaskUpdatePayload,
  options: { signal?: AbortSignal; baseUrl?: string } = {},
): Promise<TaskDetail> {
  const base = options.baseUrl ?? '';
  let response: Response;
  try {
    response = await fetch(`${base}/tasks/${encodeURIComponent(id)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      credentials: 'include',
      body: JSON.stringify(payload),
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new Error(`Could not reach backend: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (!response.ok) throw await taskError(response);
  return (await response.json()) as TaskDetail;
}

export async function deleteTask(
  id: string,
  options: { signal?: AbortSignal; baseUrl?: string } = {},
): Promise<void> {
  const base = options.baseUrl ?? '';
  let response: Response;
  try {
    response = await fetch(`${base}/tasks/${encodeURIComponent(id)}`, {
      method: 'DELETE',
      credentials: 'include',
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new Error(`Could not reach backend: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (response.status !== 204) throw await taskError(response);
}

/** Toggle the caller's personal favorite for a task (idempotent both ways). */
export async function setTaskFavorite(
  id: string,
  favorite: boolean,
  options: { signal?: AbortSignal; baseUrl?: string } = {},
): Promise<void> {
  const base = options.baseUrl ?? '';
  let response: Response;
  try {
    response = await fetch(`${base}/tasks/${encodeURIComponent(id)}/favorite`, {
      method: favorite ? 'PUT' : 'DELETE',
      credentials: 'include',
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new Error(`Could not reach backend: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (response.status !== 204) throw await taskError(response);
}

// -------- Workspaces --------

/** Create a new workspace; the caller becomes its admin. Returns the new
 * membership (same shape as a `MeResponse.memberships[]` entry). */
export async function createWorkspace(
  name: string,
  options: { signal?: AbortSignal; baseUrl?: string } = {},
): Promise<MembershipInfo> {
  const base = options.baseUrl ?? '';
  let response: Response;
  try {
    response = await fetch(`${base}/workspaces`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ name }),
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new Error(`Could not reach backend: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (!response.ok) throw await readApiError(response);
  return (await response.json()) as MembershipInfo;
}

/** Permanently delete a workspace and everything it owns (admin-only). 204 on success. */
export async function deleteWorkspace(
  companyId: string,
  options: { signal?: AbortSignal; baseUrl?: string } = {},
): Promise<void> {
  const base = options.baseUrl ?? '';
  let response: Response;
  try {
    response = await fetch(`${base}/workspaces/${encodeURIComponent(companyId)}`, {
      method: 'DELETE',
      credentials: 'include',
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new Error(`Could not reach backend: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (response.status !== 204) throw await readApiError(response);
}

// -------- Team / Invites --------

export interface MemberListItem {
  user_id: string;
  email: string;
  role: 'admin' | 'interviewer';
  joined_at: string;
}

export type InviteStatus = 'pending' | 'accepted' | 'expired' | 'revoked';

export interface InviteItem {
  id: string;
  workspace_id: string;
  token: string;
  email: string | null;
  role: 'admin' | 'interviewer';
  invited_by: string;
  created_at: string;
  expires_at: string;
  accepted_at: string | null;
  accepted_by: string | null;
  status: InviteStatus;
  accept_url: string | null;
}

export interface InvitePreview {
  company_name: string;
  inviter_email: string;
  invited_email: string | null;
  role: 'admin' | 'interviewer';
  expires_at: string;
  status: InviteStatus;
}

export interface AuditLogEntry {
  id: string;
  action: string;
  actor_email: string | null;
  target_type: string | null;
  target_id: string | null;
  /** Target email resolved server-side at read time. */
  target_email: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
}

/** Most-recent workspace activity (admin-only). */
export async function listAudit(
  companyId: string,
  options: { signal?: AbortSignal; baseUrl?: string } = {},
): Promise<AuditLogEntry[]> {
  const base = options.baseUrl ?? '';
  let response: Response;
  try {
    response = await fetch(`${base}/workspaces/${encodeURIComponent(companyId)}/audit`, {
      headers: { Accept: 'application/json' },
      credentials: 'include',
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new Error(`Could not reach backend: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const body = (await response.json()) as { entries: AuditLogEntry[] };
  return body.entries;
}

export async function listMembers(
  companyId: string,
  options: { signal?: AbortSignal; baseUrl?: string } = {},
): Promise<MemberListItem[]> {
  const base = options.baseUrl ?? '';
  let response: Response;
  try {
    response = await fetch(`${base}/workspaces/${encodeURIComponent(companyId)}/members`, {
      headers: { Accept: 'application/json' },
      credentials: 'include',
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new Error(`Could not reach backend: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const body = (await response.json()) as { members: MemberListItem[] };
  return body.members;
}

export async function listCompanyInvites(
  companyId: string,
  options: { signal?: AbortSignal; baseUrl?: string } = {},
): Promise<InviteItem[]> {
  const base = options.baseUrl ?? '';
  let response: Response;
  try {
    response = await fetch(`${base}/workspaces/${encodeURIComponent(companyId)}/invites`, {
      headers: { Accept: 'application/json' },
      credentials: 'include',
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new Error(`Could not reach backend: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return (await response.json()) as InviteItem[];
}

export async function createInvite(
  companyId: string,
  payload: { role: 'admin' | 'interviewer'; email?: string },
  options: { signal?: AbortSignal; baseUrl?: string } = {},
): Promise<InviteItem> {
  const base = options.baseUrl ?? '';
  let response: Response;
  try {
    response = await fetch(`${base}/workspaces/${encodeURIComponent(companyId)}/invites`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      credentials: 'include',
      body: JSON.stringify(payload),
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new Error(`Could not reach backend: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (!response.ok) throw await readApiError(response);
  return (await response.json()) as InviteItem;
}

export async function revokeInvite(
  token: string,
  options: { signal?: AbortSignal; baseUrl?: string } = {},
): Promise<void> {
  const base = options.baseUrl ?? '';
  let response: Response;
  try {
    response = await fetch(`${base}/invites/${encodeURIComponent(token)}`, {
      method: 'DELETE',
      credentials: 'include',
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new Error(`Could not reach backend: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (response.status !== 204) throw await readApiError(response);
}

/** Error from a member-management call; carries the backend `{reason}` so the UI
 * can localize the `last_admin` 409 vs a generic failure. */
export class MemberApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public reason: string | null,
  ) {
    super(message);
    this.name = 'MemberApiError';
  }
}

async function _memberError(response: Response): Promise<MemberApiError> {
  let reason: string | null = null;
  try {
    const body = (await response.json()) as { detail?: { reason?: string } };
    reason = body?.detail?.reason ?? null;
  } catch {
    // non-JSON body
  }
  return new MemberApiError(`HTTP ${response.status}`, response.status, reason);
}

/** Change a member's role within a workspace (admin-only). */
export async function updateMemberRole(
  companyId: string,
  userId: string,
  role: 'admin' | 'interviewer',
  options: { signal?: AbortSignal; baseUrl?: string } = {},
): Promise<MemberListItem> {
  const base = options.baseUrl ?? '';
  let response: Response;
  try {
    response = await fetch(
      `${base}/workspaces/${encodeURIComponent(companyId)}/members/${encodeURIComponent(userId)}`,
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ role }),
        signal: options.signal,
      },
    );
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new Error(`Could not reach backend: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (!response.ok) throw await _memberError(response);
  return (await response.json()) as MemberListItem;
}

/** Remove a member from a workspace (admin-only). */
export async function removeMember(
  companyId: string,
  userId: string,
  options: { signal?: AbortSignal; baseUrl?: string } = {},
): Promise<void> {
  const base = options.baseUrl ?? '';
  let response: Response;
  try {
    response = await fetch(
      `${base}/workspaces/${encodeURIComponent(companyId)}/members/${encodeURIComponent(userId)}`,
      { method: 'DELETE', credentials: 'include', signal: options.signal },
    );
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new Error(`Could not reach backend: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (response.status !== 204) throw await _memberError(response);
}

export async function previewInvite(
  token: string,
  options: { signal?: AbortSignal; baseUrl?: string } = {},
): Promise<InvitePreview> {
  const base = options.baseUrl ?? '';
  let response: Response;
  try {
    response = await fetch(`${base}/invites/${encodeURIComponent(token)}`, {
      headers: { Accept: 'application/json' },
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new Error(`Could not reach backend: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (!response.ok) throw await readApiError(response);
  return (await response.json()) as InvitePreview;
}

export async function acceptInvite(
  token: string,
  options: { signal?: AbortSignal; baseUrl?: string } = {},
): Promise<void> {
  const base = options.baseUrl ?? '';
  let response: Response;
  try {
    response = await fetch(`${base}/invites/${encodeURIComponent(token)}/accept`, {
      method: 'POST',
      credentials: 'include',
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new Error(`Could not reach backend: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (!response.ok) throw await readApiError(response);
}

export async function signupAndAcceptInvite(
  token: string,
  data: { email: string; password: string },
  options: { signal?: AbortSignal; baseUrl?: string } = {},
): Promise<void> {
  const base = options.baseUrl ?? '';
  let response: Response;
  try {
    response = await fetch(`${base}/invites/${encodeURIComponent(token)}/signup-and-accept`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      credentials: 'include',
      body: JSON.stringify(data),
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new Error(`Could not reach backend: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (!response.ok) {
    const apiError = await readApiError(response);
    // The signup form special-cases a duplicate-email collision (AuthError path);
    // every other failure flows through the shared ApiError reason→i18n map.
    if (response.status === 409) {
      throw new AuthError('duplicate', apiError.reason ?? apiError.message, response.status);
    }
    throw apiError;
  }
}

// -------- GET /sessions/by-candidate-token/{token} --------

export async function getSessionByCandidateToken(
  token: string,
  options: GetSessionConfigOptions = {},
): Promise<SessionConfig> {
  const base = options.baseUrl ?? '';
  const url = `${base}/sessions/by-candidate-token/${encodeURIComponent(token)}`;

  let response: Response;
  try {
    response = await fetch(url, {
      method: 'GET',
      headers: { Accept: 'application/json' },
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new SessionConfigError(
      'network',
      `Could not reach backend: ${err instanceof Error ? err.message : String(err)}`,
    );
  }

  if (response.ok) {
    let body: unknown;
    try {
      body = await response.json();
    } catch (err) {
      throw new SessionConfigError(
        'invalid_response',
        `Backend returned non-JSON: ${err instanceof Error ? err.message : String(err)}`,
        response.status,
      );
    }
    if (!isSessionConfig(body)) {
      throw new SessionConfigError(
        'invalid_response',
        'Backend response does not match SessionConfig',
        response.status,
      );
    }
    return body;
  }

  const httpReason = mapStatusToReason(response.status);
  const detail = await safeReadDetail(response);
  const reason = parseConfigReason(detail.reason) ?? httpReason;
  const message = detail.message ?? defaultMessage(reason);
  throw new SessionConfigError(reason, message, response.status);
}

// -------- POST /sessions/{code}/end via candidate_token --------

export async function endSessionByToken(
  code: string,
  token: string,
  options: EndSessionOptions = {},
): Promise<void> {
  const base = options.baseUrl ?? '';
  const url = `${base}/sessions/${encodeURIComponent(code)}/end?candidate_token=${encodeURIComponent(token)}`;

  let response: Response;
  try {
    response = await fetch(url, {
      method: 'POST',
      headers: { Accept: 'application/json' },
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new EndSessionError(
      'network',
      `Could not reach backend: ${err instanceof Error ? err.message : String(err)}`,
    );
  }

  if (!response.ok) {
    let httpReason: EndSessionErrorReason;
    if (response.status === 401 || response.status === 403) httpReason = 'unauthorized';
    else if (response.status === 404) httpReason = 'not_found';
    else httpReason = 'unknown';

    const detail = await safeReadDetail(response);
    const reason = parseEndReason(detail.reason) ?? httpReason;
    const message = detail.message ?? `Backend returned status ${response.status}`;
    throw new EndSessionError(reason, message, response.status);
  }
}

function isSessionConfig(value: unknown): value is SessionConfig {
  if (typeof value !== 'object' || value === null) return false;
  const obj = value as Record<string, unknown>;
  return (
    typeof obj.session_code === 'string' &&
    typeof obj.session_status === 'string' &&
    typeof obj.protocol_version === 'number' &&
    typeof obj.task_id === 'string' &&
    typeof obj.task_dir_name === 'string' &&
    typeof obj.ai_config === 'object' &&
    obj.ai_config !== null
  );
}
