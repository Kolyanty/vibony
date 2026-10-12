import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  endSession,
  EndSessionError,
  getFileContent,
  getFileTree,
  getSessionConfig,
  SessionConfigError,
} from '../api/rest';

function makeConfig() {
  return {
    session_code: 'AAAA-BBBB',
    session_status: 'pending',
    protocol_version: 1,
    ws_token: 'tok',
    task_id: 'task-01-training-loop',
    task_dir_name: 'training-loop',
    ai_config: { primary_model: 'm', fallback_models: [] },
    bundle_sha256: null,
    created_at: '2026-05-15T10:00:00Z',
    expires_at: '2026-05-15T18:00:00Z',
  };
}

describe('safeReadDetail / getSessionConfig — structured detail', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('structured {reason: "not_found"} → SessionConfigError.reason = "not_found"', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 404,
        json: () => Promise.resolve({ detail: { reason: 'not_found', code: 'AAAA-BBBB' } }),
      }),
    );

    await expect(getSessionConfig('AAAA-BBBB')).rejects.toMatchObject({
      name: 'SessionConfigError',
      reason: 'not_found',
      status: 404,
    });
  });

  it('structured {reason: "unauthorized"} with 401 → reason = "unauthorized"', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 401,
        json: () => Promise.resolve({ detail: { reason: 'unauthorized' } }),
      }),
    );

    try {
      await getSessionConfig('AAAA-BBBB');
      throw new Error('expected throw');
    } catch (err) {
      expect(err).toBeInstanceOf(SessionConfigError);
      const e = err as SessionConfigError;
      expect(e.reason).toBe('unauthorized');
      expect(e.status).toBe(401);
    }
  });

  it('structured {reason: "completed"} with 410 → reason = "completed"', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 410,
        json: () => Promise.resolve({ detail: { reason: 'completed', code: 'AAAA-BBBB' } }),
      }),
    );

    await expect(getSessionConfig('AAAA-BBBB')).rejects.toMatchObject({
      reason: 'completed',
      status: 410,
    });
  });

  it('string detail (back-compat with an older backend) → reason from status, message = detail', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 404,
        json: () => Promise.resolve({ detail: 'Session AAAA-BBBB not found' }),
      }),
    );

    try {
      await getSessionConfig('AAAA-BBBB');
      throw new Error('expected throw');
    } catch (err) {
      const e = err as SessionConfigError;
      expect(e.reason).toBe('not_found');
      expect(e.message).toBe('Session AAAA-BBBB not found');
    }
  });

  it('unknown reason from backend — fallback to status-mapped reason', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 404,
        json: () => Promise.resolve({ detail: { reason: 'wat_is_this' } }),
      }),
    );

    try {
      await getSessionConfig('AAAA-BBBB');
      throw new Error('expected throw');
    } catch (err) {
      const e = err as SessionConfigError;
      expect(e.reason).toBe('not_found');
    }
  });

  it('successful 200 → SessionConfig is returned as-is', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: () => Promise.resolve(makeConfig()),
      }),
    );
    const cfg = await getSessionConfig('AAAA-BBBB');
    expect(cfg.session_code).toBe('AAAA-BBBB');
  });

  it('uses credentials:include (cookie auth)', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve(makeConfig()),
    });
    vi.stubGlobal('fetch', mockFetch);
    await getSessionConfig('AAAA-BBBB');
    const [, options] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(options.credentials).toBe('include');
    expect((options.headers as Record<string, string>)['Authorization']).toBeUndefined();
  });
});

// -------- getFileTree --------

describe('getFileTree', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('returns entries array on 200', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: () =>
          Promise.resolve({
            entries: [
              { path: 'train.py', kind: 'file', size: 42 },
              { path: 'data.py', kind: 'file', size: 18 },
            ],
          }),
      }),
    );
    const result = await getFileTree('AAAA-BBBB');
    expect(result.entries).toHaveLength(2);
    expect(result.entries[0]!.path).toBe('train.py');
    expect(result.entries[1]!.path).toBe('data.py');
  });

  it('throws with HTTP status on non-200', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: false, status: 503, json: () => Promise.resolve({}) }),
    );
    await expect(getFileTree('AAAA-BBBB')).rejects.toThrow('HTTP 503');
  });

  it('uses credentials:include, no Authorization header', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ entries: [] }),
    });
    vi.stubGlobal('fetch', mockFetch);
    await getFileTree('AAAA-BBBB');
    const [, options] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(options.credentials).toBe('include');
    expect((options.headers as Record<string, string>)['Authorization']).toBeUndefined();
  });
});

// -------- getFileContent --------

describe('getFileContent', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('returns content string on 200', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        text: () => Promise.resolve('import torch\n'),
      }),
    );
    const content = await getFileContent('AAAA-BBBB', 'train.py');
    expect(content).toBe('import torch\n');
  });

  it('throws on 404', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: false, status: 404, text: () => Promise.resolve('') }),
    );
    await expect(getFileContent('AAAA-BBBB', 'missing.py')).rejects.toThrow('HTTP 404');
  });

  it('URL-encodes path query param', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      text: () => Promise.resolve(''),
    });
    vi.stubGlobal('fetch', mockFetch);
    await getFileContent('AAAA-BBBB', 'src/utils.py');
    const [url] = mockFetch.mock.calls[0] as [string];
    expect(url).toContain('path=src%2Futils.py');
  });
});

// -------- endSession --------

describe('endSession — structured detail', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('structured {reason: "unauthorized"} with 401 → EndSessionError.reason = "unauthorized"', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 401,
        json: () => Promise.resolve({ detail: { reason: 'unauthorized' } }),
      }),
    );

    try {
      await endSession('AAAA-BBBB');
      throw new Error('expected throw');
    } catch (err) {
      expect(err).toBeInstanceOf(EndSessionError);
      const e = err as EndSessionError;
      expect(e.reason).toBe('unauthorized');
      expect(e.status).toBe(401);
    }
  });

  it('structured {reason: "not_found"} with 404 → reason = "not_found"', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 404,
        json: () => Promise.resolve({ detail: { reason: 'not_found', code: 'AAAA-BBBB' } }),
      }),
    );

    await expect(endSession('AAAA-BBBB')).rejects.toMatchObject({
      reason: 'not_found',
      status: 404,
    });
  });

  it('string detail (back-compat) → reason from status, message = detail', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 401,
        json: () => Promise.resolve({ detail: 'Bad token' }),
      }),
    );

    try {
      await endSession('AAAA-BBBB');
      throw new Error('expected throw');
    } catch (err) {
      const e = err as EndSessionError;
      expect(e.reason).toBe('unauthorized');
      expect(e.message).toBe('Bad token');
    }
  });

  it('2xx with valid JSON → returns SessionConfig', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        text: () => Promise.resolve('{"session_code":"AAAA-BBBB"}'),
      }),
    );
    const cfg = await endSession('AAAA-BBBB');
    expect(cfg).toMatchObject({ session_code: 'AAAA-BBBB' });
  });

  it('2xx with an empty body → null, no throw', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 204,
        text: () => Promise.resolve(''),
      }),
    );
    await expect(endSession('AAAA-BBBB')).resolves.toBeNull();
  });

  it('2xx with a non-JSON body → null, no throw', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        text: () => Promise.resolve('OK'),
      }),
    );
    await expect(endSession('AAAA-BBBB')).resolves.toBeNull();
  });
});
