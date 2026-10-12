import { afterEach, describe, expect, it, vi } from 'vitest';
import type { TFunction } from 'i18next';
import { ApiError, MemberApiError, createSession } from '../api/rest';
import { AuthError } from '../api/auth';
import { apiErrorMessage } from '../lib/apiError';

// Identity `t`: assert the i18n KEY the resolver picked (not the localized text).
const t = ((key: string) => key) as unknown as TFunction;

describe('apiErrorMessage — shared reason→i18n resolution', () => {
  it('maps a known backend reason to its i18n key', () => {
    expect(apiErrorMessage(new ApiError('HTTP 403', 403, 'no_membership'), t)).toBe(
      'error.api.noMembership',
    );
    expect(apiErrorMessage(new ApiError('HTTP 410', 410, 'inviter_unauthorized'), t)).toBe(
      'error.api.inviterUnauthorized',
    );
    expect(apiErrorMessage(new ApiError('HTTP 422', 422, 'workspace_id_required'), t)).toBe(
      'error.api.workspaceRequired',
    );
  });

  it('reuses existing keys for invite-lifecycle + email-mismatch reasons', () => {
    expect(apiErrorMessage(new ApiError('HTTP 403', 403, 'email_mismatch'), t)).toBe(
      'invite.error.emailMismatch',
    );
    expect(apiErrorMessage(new ApiError('HTTP 410', 410, 'expired'), t)).toBe('invite.status.expired');
    expect(apiErrorMessage(new MemberApiError('HTTP 409', 409, 'last_admin'), t)).toBe(
      'team.error.lastAdmin',
    );
  });

  it('falls back to a localized status bucket when no reason + only an HTTP-code message', () => {
    expect(apiErrorMessage(new ApiError('HTTP 422', 422), t)).toBe('error.api.validation');
    expect(apiErrorMessage(new ApiError('HTTP 403', 403), t)).toBe('error.api.forbidden');
    expect(apiErrorMessage(new ApiError('HTTP 409', 409), t)).toBe('error.api.conflict');
    expect(apiErrorMessage(new ApiError('HTTP 429', 429), t)).toBe('error.api.rateLimited');
  });

  it('surfaces a real authored backend message (never a bare HTTP code)', () => {
    expect(apiErrorMessage(new ApiError("model 'x' is not allowed", 422), t)).toBe(
      "model 'x' is not allowed",
    );
  });

  it('maps a network failure (plain Error) to the offline message', () => {
    expect(apiErrorMessage(new Error('Could not reach backend: failed to fetch'), t)).toBe(
      'error.api.network',
    );
  });

  it('maps an AuthError 429 reason to the rate-limit key', () => {
    expect(apiErrorMessage(new AuthError('rate_limited', 'HTTP 429', 429), t)).toBe(
      'error.api.rateLimited',
    );
  });

  it('uses the generic fallback when there is no reason, message, or mappable status', () => {
    // HTTP-only message + an unmapped status (500) → generic.
    expect(apiErrorMessage(new ApiError('HTTP 500', 500), t)).toBe('error.api.generic');
  });

  it('passes a non-Error thrown value through as its message', () => {
    // Edge: a raw string is treated as an authored message (we never strip it
    // to the generic fallback — it may carry real information).
    expect(apiErrorMessage('weird', t)).toBe('weird');
  });
});

describe('readApiError — detail parsing (D-3)', () => {
  afterEach(() => vi.unstubAllGlobals());

  const payload = { task_id: 'task-01', mode: 'live' as const };

  it('parses {reason} into ApiError.reason + status (createSession)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 403,
        json: () => Promise.resolve({ detail: { reason: 'no_membership' } }),
      }),
    );
    await expect(createSession(payload)).rejects.toMatchObject({
      name: 'ApiError',
      status: 403,
      reason: 'no_membership',
    });
  });

  it('parses a string detail into the message (model-allowlist 422)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 422,
        json: () => Promise.resolve({ detail: "model 'x' is not allowed" }),
      }),
    );
    await expect(createSession(payload)).rejects.toMatchObject({
      status: 422,
      message: "model 'x' is not allowed",
    });
  });

  it('joins a FastAPI 422 validation array into the message', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 422,
        json: () =>
          Promise.resolve({ detail: [{ loc: ['body', 'email'], msg: 'value is not a valid email address' }] }),
      }),
    );
    await expect(createSession(payload)).rejects.toMatchObject({
      status: 422,
      message: 'value is not a valid email address',
    });
  });

  it('also extracts the message from a {reason, message} object detail', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 409,
        json: () => Promise.resolve({ detail: { reason: 'not_completed', message: 'finish first' } }),
      }),
    );
    await expect(createSession(payload)).rejects.toMatchObject({
      status: 409,
      reason: 'not_completed',
      message: 'finish first',
    });
  });
});
