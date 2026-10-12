// Shared error-surfacing helper.
//
// The `api/` layer throws errors carrying a stable backend `reason`
// (snake_case machine-id), an HTTP `status`, and a human `message`. This module
// turns any of them into a localized string so a known failure shows a real
// message instead of a raw `HTTP 4xx` code or an untranslated protocol string.
//
// Resolution order: known reason → real backend message → status bucket →
// generic fallback. Network failures (thrown as a plain Error with the
// "Could not reach backend" prefix from the api layer) map to the offline copy.
import type { TFunction } from 'i18next';
import { AuthError } from '../api/auth';
import { ApiError, MemberApiError, TaskApiError } from '../api/rest';

// Backend machine-reason → i18n key. Keep in sync with the `{"reason": ...}`
// detail bodies in backend/vibony_backend/{auth,sessions}/.
const REASON_KEYS: Record<string, string> = {
  not_found: 'error.api.notFound',
  not_completed: 'error.api.notCompleted',
  unauthorized: 'error.api.forbidden',
  forbidden: 'error.api.forbidden',
  no_membership: 'error.api.noMembership',
  unknown_task: 'error.api.unknownTask',
  workspace_id_required: 'error.api.workspaceRequired',
  duplicate_email: 'error.api.duplicateEmail',
  email_mismatch: 'invite.error.emailMismatch',
  inviter_unauthorized: 'error.api.inviterUnauthorized',
  admin_invite_requires_email: 'error.api.adminInviteRequiresEmail',
  last_admin: 'team.error.lastAdmin',
  rate_limited: 'error.api.rateLimited',
  // Invite-lifecycle 410 reasons (the invite changed state since preview).
  accepted: 'invite.status.accepted',
  expired: 'invite.status.expired',
  revoked: 'invite.status.revoked',
};

// HTTP status → i18n key, used only when no machine reason is present.
const STATUS_KEYS: Record<number, string> = {
  403: 'error.api.forbidden',
  409: 'error.api.conflict',
  422: 'error.api.validation',
  429: 'error.api.rateLimited',
};

interface ErrorInfo {
  reason?: string;
  status?: number;
  message?: string;
}

function extract(err: unknown): ErrorInfo {
  if (err instanceof ApiError) return { reason: err.reason, status: err.status, message: err.message };
  if (err instanceof MemberApiError) {
    return { reason: err.reason ?? undefined, status: err.status, message: err.message };
  }
  if (err instanceof TaskApiError) return { reason: err.reason, status: err.status, message: err.message };
  if (err instanceof AuthError) return { reason: err.reason, status: err.status, message: err.message };
  if (err instanceof Error) return { message: err.message };
  return { message: String(err) };
}

const NETWORK_RE = /Could not reach backend/i;
const HTTP_ONLY_RE = /^HTTP \d+$/;

/**
 * Localize an error thrown by the `api/` layer. `fallbackKey` is used when the
 * error carries no recognizable reason, message, or mappable status.
 */
export function apiErrorMessage(err: unknown, t: TFunction, fallbackKey = 'error.api.generic'): string {
  const { reason, status, message } = extract(err);
  if (reason && REASON_KEYS[reason]) return t(REASON_KEYS[reason]);
  if (message && NETWORK_RE.test(message)) return t('error.api.network');
  // A real authored backend message (string detail / joined 422 validation),
  // but never the placeholder `HTTP 4xx`.
  if (message && !HTTP_ONLY_RE.test(message)) return message;
  if (status && STATUS_KEYS[status]) return t(STATUS_KEYS[status]);
  return t(fallbackKey);
}
