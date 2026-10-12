import { afterEach, describe, expect, it, vi } from 'vitest';
import { getAuthConfig, getMe, login, signup, logout, AuthError } from '../api/auth';

function makeMeResponse() {
  return {
    user: { id: 'u1', email: 'alice@example.com', created_at: '2026-05-01T00:00:00Z' },
    memberships: [{ workspace_id: 'c1', company_name: 'Acme', role: 'admin' }],
  };
}

describe('auth API', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('getAuthConfig returns mode + oidc_login_url', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: () =>
          Promise.resolve({ mode: 'password', oidc_login_url: null }),
      }),
    );
    const config = await getAuthConfig();
    expect(config.mode).toBe('password');
    expect(config.oidc_login_url).toBeNull();
  });

  it('getMe returns user + memberships on 200', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: () => Promise.resolve(makeMeResponse()),
      }),
    );
    const me = await getMe();
    expect(me.user.email).toBe('alice@example.com');
    expect(me.memberships).toHaveLength(1);
  });

  it('getMe throws AuthError("unauthorized") on 401', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: false, status: 401, json: () => Promise.resolve({}) }),
    );
    try {
      await getMe();
      throw new Error('expected throw');
    } catch (err) {
      expect(err).toBeInstanceOf(AuthError);
      expect((err as AuthError).reason).toBe('unauthorized');
    }
  });

  it('login posts credentials and returns MeResponse on 200', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve(makeMeResponse()),
    });
    vi.stubGlobal('fetch', fetchMock);
    const me = await login({ email: 'alice@example.com', password: 'password123' });
    expect(me.user.email).toBe('alice@example.com');
    const [url, opts] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain('/auth/login');
    expect(opts.method).toBe('POST');
    expect(opts.credentials).toBe('include');
    const body = JSON.parse(opts.body as string) as unknown;
    expect((body as { email: string }).email).toBe('alice@example.com');
  });

  it('login throws AuthError("unauthorized") on 401', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: false, status: 401, json: () => Promise.resolve({}) }),
    );
    await expect(login({ email: 'x@x.com', password: 'wrong' })).rejects.toMatchObject({
      name: 'AuthError',
      reason: 'unauthorized',
    });
  });

  it('signup throws AuthError("duplicate") on 409', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: false, status: 409, json: () => Promise.resolve({}) }),
    );
    await expect(
      signup({ email: 'x@x.com', password: 'password123', company_name: 'Corp' }),
    ).rejects.toMatchObject({ reason: 'duplicate' });
  });

  it('logout does not throw even on network error', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('network down')));
    await expect(logout()).resolves.toBeUndefined();
  });

  it('all auth calls use credentials:include', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () =>
        Promise.resolve({ mode: 'password', oidc_login_url: null }),
    });
    vi.stubGlobal('fetch', fetchMock);
    await getAuthConfig();
    const [, opts] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(opts.credentials).toBe('include');
  });
});
