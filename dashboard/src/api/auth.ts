export interface AuthConfig {
  mode: 'password' | 'oidc';
  oidc_login_url: string | null;
}

export interface User {
  id: string;
  email: string;
  created_at: string;
  // False until the user confirms their email (non-blocking banner).
  email_verified: boolean;
}

export type WorkspacePlan = 'trial' | 'pro';

export interface MembershipInfo {
  workspace_id: string;
  company_name: string;
  role: 'admin' | 'interviewer';
  // Billing-plan stub. Defaults preserve compatibility with older payloads.
  plan: WorkspacePlan;
  trial_ends_at: string | null;
}

export interface MeResponse {
  user: User;
  memberships: MembershipInfo[];
}

export type AuthErrorReason =
  | 'unauthorized'
  | 'duplicate'
  | 'invalid'
  | 'rate_limited'
  | 'network'
  | 'unknown';

export class AuthError extends Error {
  reason: AuthErrorReason;
  status?: number;

  constructor(reason: AuthErrorReason, message: string, status?: number) {
    super(message);
    this.name = 'AuthError';
    this.reason = reason;
    this.status = status;
  }
}

interface Options {
  signal?: AbortSignal;
  baseUrl?: string;
}

export async function getAuthConfig(options: Options = {}): Promise<AuthConfig> {
  const base = options.baseUrl ?? '';
  let response: Response;
  try {
    response = await fetch(`${base}/auth/config`, {
      method: 'GET',
      headers: { Accept: 'application/json' },
      credentials: 'include',
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new AuthError(
      'network',
      `Could not reach backend: ${err instanceof Error ? err.message : String(err)}`,
    );
  }
  if (!response.ok) throw new AuthError('unknown', `HTTP ${response.status}`, response.status);
  return (await response.json()) as AuthConfig;
}

export async function getMe(options: Options = {}): Promise<MeResponse> {
  const base = options.baseUrl ?? '';
  let response: Response;
  try {
    response = await fetch(`${base}/auth/me`, {
      method: 'GET',
      headers: { Accept: 'application/json' },
      credentials: 'include',
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new AuthError(
      'network',
      `Could not reach backend: ${err instanceof Error ? err.message : String(err)}`,
    );
  }
  if (response.status === 401 || response.status === 403) {
    throw new AuthError('unauthorized', 'Not authenticated', response.status);
  }
  if (!response.ok) throw new AuthError('unknown', `HTTP ${response.status}`, response.status);
  return (await response.json()) as MeResponse;
}

export async function login(
  data: { email: string; password: string },
  options: Options = {},
): Promise<MeResponse> {
  return _authPost(`${options.baseUrl ?? ''}/auth/login`, data, options);
}

export async function signup(
  data: { email: string; password: string; company_name: string },
  options: Options = {},
): Promise<MeResponse> {
  return _authPost(`${options.baseUrl ?? ''}/auth/signup`, data, options);
}

/** Confirm an email from the verification-link token (no auth — the token is the proof). */
export async function verifyEmail(token: string, options: Options = {}): Promise<void> {
  const base = options.baseUrl ?? '';
  let response: Response;
  try {
    response = await fetch(`${base}/auth/verify-email`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ token }),
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new AuthError('network', `Could not reach backend: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (response.ok) return;
  if (response.status === 400) throw new AuthError('invalid', 'Invalid or expired token', 400);
  throw new AuthError('unknown', `HTTP ${response.status}`, response.status);
}

/** Re-send the verification email to the logged-in user. */
export async function resendVerification(options: Options = {}): Promise<void> {
  const base = options.baseUrl ?? '';
  let response: Response;
  try {
    response = await fetch(`${base}/auth/verify-email/resend`, {
      method: 'POST',
      credentials: 'include',
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new AuthError('network', `Could not reach backend: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (response.status === 401 || response.status === 403) {
    throw new AuthError('unauthorized', 'Not authenticated', response.status);
  }
  if (!response.ok) throw new AuthError('unknown', `HTTP ${response.status}`, response.status);
}

export async function logout(options: Options = {}): Promise<void> {
  const base = options.baseUrl ?? '';
  try {
    await fetch(`${base}/auth/logout`, {
      method: 'POST',
      credentials: 'include',
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    // Logout errors are non-fatal; caller can redirect after anyway.
  }
}

async function _authPost<T>(url: string, data: unknown, options: Options): Promise<T> {
  let response: Response;
  try {
    response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      credentials: 'include',
      body: JSON.stringify(data),
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new AuthError(
      'network',
      `Could not reach backend: ${err instanceof Error ? err.message : String(err)}`,
    );
  }
  if (response.ok) return (await response.json()) as T;
  const reason = _mapStatusToReason(response.status);
  throw new AuthError(reason, `HTTP ${response.status}`, response.status);
}

function _mapStatusToReason(status: number): AuthErrorReason {
  if (status === 401 || status === 403) return 'unauthorized';
  if (status === 409) return 'duplicate';
  if (status === 422) return 'invalid';
  // Rate limiter on login/signup → surface a dedicated "too many attempts"
  // message instead of folding 429 into "could not reach server".
  if (status === 429) return 'rate_limited';
  return 'unknown';
}
