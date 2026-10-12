import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SessionsListPage } from '../pages/SessionsListPage';

function makeMeResponse(
  memberships = [
    { workspace_id: 'c1', company_name: 'Acme', role: 'admin' as const, plan: 'trial', trial_ends_at: null },
  ],
) {
  return {
    user: { id: 'u1', email: 'alice@acme.com', created_at: '2026-05-01T00:00:00Z', email_verified: true },
    memberships,
  };
}

function makeSessionsResponse(sessions = []) {
  return { sessions };
}

function makeTasks() {
  return [
    {
      id: 't2',
      company_id: 'c1',
      slug: 'task-01-training-loop',
      title: 'Training loop',
      runtime_image: 'vibony/sandbox-py:3.12',
      visibility: 'public',
      is_template: true,
      can_edit: false,
      is_favorite: false,
      created_at: '2026-05-01T00:00:00Z',
      updated_at: '2026-05-01T00:00:00Z',
    },
  ];
}

function makeSessions() {
  return [
    {
      session_code: 'AAAA-BBBB',
      session_status: 'active',
      task_id: 'task-01-training-loop',
      task_dir_name: 'training-loop',
      workspace_id: 'c1',
      created_at: '2026-05-15T10:00:00Z',
      expires_at: '2026-05-15T18:00:00Z',
    },
    {
      session_code: 'CCCC-DDDD',
      session_status: 'completed',
      task_id: 'task-02-seat-reservation',
      task_dir_name: 'seat-reservation',
      workspace_id: 'c1',
      created_at: '2026-05-14T09:00:00Z',
      expires_at: '2026-05-14T17:00:00Z',
    },
  ];
}

function renderPage() {
  const router = createMemoryRouter(
    [
      { path: '/', element: <SessionsListPage /> },
      { path: '/login', element: <p data-testid="login-page">Login</p> },
      { path: '/session/:code', element: <p data-testid="session-page">Session</p> },
    ],
    { initialEntries: ['/'] },
  );
  return render(<RouterProvider router={router} />);
}

describe('SessionsListPage', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it('shows empty state when no sessions', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string) => {
        if (typeof url === 'string' && url.includes('/auth/me')) {
          return Promise.resolve({
            ok: true,
            status: 200,
            json: () => Promise.resolve(makeMeResponse()),
          });
        }
        if (typeof url === 'string' && url.includes('/tasks')) {
          return Promise.resolve({
            ok: true,
            status: 200,
            json: () => Promise.resolve({ tasks: makeTasks() }),
          });
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve(makeSessionsResponse()),
        });
      }),
    );
    renderPage();
    await waitFor(() => expect(screen.getByTestId('sessions-empty')).toBeInTheDocument());
  });

  it('renders session rows', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string) => {
        if (typeof url === 'string' && url.includes('/auth/me')) {
          return Promise.resolve({
            ok: true,
            status: 200,
            json: () => Promise.resolve(makeMeResponse()),
          });
        }
        if (typeof url === 'string' && url.includes('/tasks')) {
          return Promise.resolve({
            ok: true,
            status: 200,
            json: () => Promise.resolve({ tasks: makeTasks() }),
          });
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ sessions: makeSessions() }),
        });
      }),
    );
    renderPage();
    await waitFor(() => expect(screen.getByTestId('sessions-table')).toBeInTheDocument());
    expect(screen.getByTestId('session-row-AAAA-BBBB')).toBeInTheDocument();
    expect(screen.getByTestId('session-row-CCCC-DDDD')).toBeInTheDocument();
  });

  it('create session button opens modal', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string) => {
        if (typeof url === 'string' && url.includes('/auth/me')) {
          return Promise.resolve({
            ok: true,
            status: 200,
            json: () => Promise.resolve(makeMeResponse()),
          });
        }
        if (typeof url === 'string' && url.includes('/tasks')) {
          return Promise.resolve({
            ok: true,
            status: 200,
            json: () => Promise.resolve({ tasks: makeTasks() }),
          });
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve(makeSessionsResponse()),
        });
      }),
    );
    renderPage();
    await waitFor(() => expect(screen.getByTestId('create-session-button')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('create-session-button'));
    expect(screen.getByTestId('create-modal')).toBeInTheDocument();
  });

  it('renders a mode badge per row (live + async)', async () => {
    const sessions = [
      { ...makeSessions()[0], session_code: 'LIVE-0001', mode: 'live' as const },
      { ...makeSessions()[1], session_code: 'ASNC-0002', mode: 'async' as const, time_limit_seconds: 3600 },
    ];
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string) => {
        if (typeof url === 'string' && url.includes('/auth/me')) {
          return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makeMeResponse()) });
        }
        if (typeof url === 'string' && url.includes('/tasks')) {
          return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ tasks: makeTasks() }) });
        }
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ sessions }) });
      }),
    );
    renderPage();
    await waitFor(() => expect(screen.getByTestId('sessions-table')).toBeInTheDocument());
    expect(screen.getByTestId('session-mode-LIVE-0001')).toHaveTextContent('Live');
    expect(screen.getByTestId('session-mode-ASNC-0002')).toHaveTextContent('Async');
  });

  it('async mode sends mode + time_limit_seconds in the create request', async () => {
    let postBody: Record<string, unknown> | null = null;
    const fetchMock = vi.fn().mockImplementation((url: string, opts?: RequestInit) => {
      if (typeof url === 'string' && url.includes('/auth/me')) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makeMeResponse()) });
      }
      if (typeof url === 'string' && url.includes('/tasks')) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ tasks: makeTasks() }) });
      }
      if (typeof url === 'string' && url === '/sessions' && opts?.method === 'POST') {
        postBody = JSON.parse(opts.body as string);
        return Promise.resolve({
          ok: true,
          status: 201,
          json: () =>
            Promise.resolve({
              session_code: 'ASNC-1234',
              session_status: 'pending',
              protocol_version: 7,
              ws_token: '',
              task_id: 'task-01-training-loop',
              task_dir_name: 'training-loop',
              ai_config: { primary_model: 'deepseek/deepseek-chat', fallback_models: [] },
              bundle_sha256: null,
              created_at: '2026-06-01T10:00:00Z',
              expires_at: '2026-06-01T18:00:00Z',
              candidate_token: 'cand-async',
              candidate_url: 'http://localhost:8000/candidate?token=cand-async',
              mode: 'async',
              time_limit_seconds: 5400,
            }),
        });
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makeSessionsResponse()) });
    });
    vi.stubGlobal('fetch', fetchMock);

    renderPage();
    await waitFor(() => expect(screen.getByTestId('create-session-button')).toBeInTheDocument());

    fireEvent.click(screen.getByTestId('create-session-button'));
    // Switch to async; the time-limit field appears only then.
    expect(screen.queryByTestId('time-limit-input')).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('mode-radio-async'));
    const limit = screen.getByTestId('time-limit-input');
    fireEvent.change(limit, { target: { value: '90' } });
    await act(async () => {
      fireEvent.submit(screen.getByTestId('create-form'));
    });

    await waitFor(() => expect(screen.getByTestId('created-code')).toBeInTheDocument());
    expect(postBody).not.toBeNull();
    expect(postBody!.mode).toBe('async');
    expect(postBody!.time_limit_seconds).toBe(5400);
    // The newly created async session shows up with its mode badge.
    expect(screen.getByTestId('session-mode-ASNC-1234')).toHaveTextContent('Async');
  });

  it('blocks an out-of-range async time limit client-side (no POST)', async () => {
    const fetchMock = vi.fn().mockImplementation((url: string, opts?: RequestInit) => {
      if (typeof url === 'string' && url.includes('/auth/me')) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makeMeResponse()) });
      }
      if (typeof url === 'string' && url.includes('/tasks')) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ tasks: makeTasks() }) });
      }
      if (typeof url === 'string' && url === '/sessions' && opts?.method === 'POST') {
        throw new Error('createSession should not be called for an invalid limit');
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makeSessionsResponse()) });
    });
    vi.stubGlobal('fetch', fetchMock);

    renderPage();
    await waitFor(() => expect(screen.getByTestId('create-session-button')).toBeInTheDocument());

    fireEvent.click(screen.getByTestId('create-session-button'));
    fireEvent.click(screen.getByTestId('mode-radio-async'));
    // Clear the field → Number('') === 0, below the 5-minute minimum.
    fireEvent.change(screen.getByTestId('time-limit-input'), { target: { value: '' } });
    await act(async () => {
      fireEvent.submit(screen.getByTestId('create-form'));
    });

    expect(screen.getByTestId('create-error')).toBeInTheDocument();
    // No POST /sessions fired.
    expect(
      fetchMock.mock.calls.some(
        ([u, o]) => u === '/sessions' && (o as RequestInit | undefined)?.method === 'POST',
      ),
    ).toBe(false);
  });

  it('normalizes leading zeros in the async time limit on blur (no "05"/"000000005")', async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (typeof url === 'string' && url.includes('/auth/me')) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makeMeResponse()) });
      }
      if (typeof url === 'string' && url.includes('/tasks')) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ tasks: makeTasks() }) });
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makeSessionsResponse()) });
    });
    vi.stubGlobal('fetch', fetchMock);

    renderPage();
    await waitFor(() => expect(screen.getByTestId('create-session-button')).toBeInTheDocument());

    fireEvent.click(screen.getByTestId('create-session-button'));
    fireEvent.click(screen.getByTestId('mode-radio-async'));
    const limit = screen.getByTestId('time-limit-input') as HTMLInputElement;

    fireEvent.change(limit, { target: { value: '000000005' } });
    fireEvent.blur(limit);
    expect(limit.value).toBe('5');

    // A cleared field stays empty (it must not snap to "0").
    fireEvent.change(limit, { target: { value: '' } });
    fireEvent.blur(limit);
    expect(limit.value).toBe('');
  });

  it('live mode sends mode live and null time_limit_seconds', async () => {
    let postBody: Record<string, unknown> | null = null;
    const fetchMock = vi.fn().mockImplementation((url: string, opts?: RequestInit) => {
      if (typeof url === 'string' && url.includes('/auth/me')) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makeMeResponse()) });
      }
      if (typeof url === 'string' && url.includes('/tasks')) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ tasks: makeTasks() }) });
      }
      if (typeof url === 'string' && url === '/sessions' && opts?.method === 'POST') {
        postBody = JSON.parse(opts.body as string);
        return Promise.resolve({
          ok: true,
          status: 201,
          json: () =>
            Promise.resolve({
              session_code: 'LIVE-9999',
              session_status: 'pending',
              protocol_version: 7,
              ws_token: '',
              task_id: 'task-01-training-loop',
              task_dir_name: 'training-loop',
              ai_config: { primary_model: 'deepseek/deepseek-chat', fallback_models: [] },
              bundle_sha256: null,
              created_at: '2026-06-01T10:00:00Z',
              expires_at: '2026-06-01T18:00:00Z',
              candidate_token: 'cand-live',
              candidate_url: 'http://localhost:8000/candidate?token=cand-live',
              mode: 'live',
              time_limit_seconds: null,
            }),
        });
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makeSessionsResponse()) });
    });
    vi.stubGlobal('fetch', fetchMock);

    renderPage();
    await waitFor(() => expect(screen.getByTestId('create-session-button')).toBeInTheDocument());

    fireEvent.click(screen.getByTestId('create-session-button'));
    await act(async () => {
      fireEvent.submit(screen.getByTestId('create-form'));
    });

    await waitFor(() => expect(screen.getByTestId('created-code')).toBeInTheDocument());
    expect(postBody).not.toBeNull();
    expect(postBody!.mode).toBe('live');
    expect(postBody!.time_limit_seconds).toBeNull();
  });

  it('shows a localized create error (not "HTTP 4xx") when POST /sessions fails (D-3)', async () => {
    const fetchMock = vi.fn().mockImplementation((url: string, opts?: RequestInit) => {
      if (typeof url === 'string' && url.includes('/auth/me')) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makeMeResponse()) });
      }
      if (typeof url === 'string' && url.includes('/tasks')) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ tasks: makeTasks() }) });
      }
      if (typeof url === 'string' && url === '/sessions' && opts?.method === 'POST') {
        return Promise.resolve({
          ok: false,
          status: 403,
          json: () => Promise.resolve({ detail: { reason: 'no_membership' } }),
        });
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makeSessionsResponse()) });
    });
    vi.stubGlobal('fetch', fetchMock);

    renderPage();
    await waitFor(() => expect(screen.getByTestId('create-session-button')).toBeInTheDocument());

    fireEvent.click(screen.getByTestId('create-session-button'));
    await act(async () => {
      fireEvent.submit(screen.getByTestId('create-form'));
    });

    const err = await screen.findByTestId('create-error');
    expect(err).toHaveTextContent("You don't belong to this workspace.");
    expect(err.textContent ?? '').not.toMatch(/HTTP|Could not reach/i);
  });

  it('scopes the session list to the active workspace', async () => {
    // Two workspaces; the active one (stored) is c2, so only its session shows.
    localStorage.setItem('vibony.workspace', 'c2');
    const memberships = [
      { workspace_id: 'c1', company_name: 'Acme', role: 'admin' as const, plan: 'trial', trial_ends_at: null },
      { workspace_id: 'c2', company_name: 'Beta', role: 'admin' as const, plan: 'trial', trial_ends_at: null },
    ];
    const sessions = [
      { ...makeSessions()[0], session_code: 'C1-0001', workspace_id: 'c1' },
      { ...makeSessions()[1], session_code: 'C2-0002', workspace_id: 'c2' },
    ];
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string) => {
        if (typeof url === 'string' && url.includes('/auth/me')) {
          return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makeMeResponse(memberships)) });
        }
        if (typeof url === 'string' && url.includes('/tasks')) {
          return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ tasks: makeTasks() }) });
        }
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ sessions }) });
      }),
    );
    renderPage();
    await waitFor(() => expect(screen.getByTestId('sessions-table')).toBeInTheDocument());
    expect(screen.getByTestId('session-row-C2-0002')).toBeInTheDocument();
    expect(screen.queryByTestId('session-row-C1-0001')).not.toBeInTheDocument();
    // Switcher reflects the active workspace.
    expect(screen.getByTestId('workspace-switcher-trigger')).toHaveTextContent('Beta');
  });

  it('scopes the create-session task select to the active workspace (D-1)', async () => {
    localStorage.setItem('vibony.workspace', 'c1');
    const memberships = [
      { workspace_id: 'c1', company_name: 'Acme', role: 'admin' as const, plan: 'trial', trial_ends_at: null },
      { workspace_id: 'c2', company_name: 'Beta', role: 'admin' as const, plan: 'trial', trial_ends_at: null },
    ];
    const tmpl = makeTasks()[0]!;
    const tasks = [
      { ...tmpl, id: 't-c2', company_id: 'c2', is_template: false, slug: 'c2-private', title: 'C2 Private' },
      { ...tmpl, id: 't-c1', company_id: 'c1', is_template: false, slug: 'c1-private', title: 'C1 Private' },
      { ...tmpl, id: 't-tmpl', company_id: 'sys', is_template: true, slug: 'tmpl', title: 'Template' },
    ];
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string) => {
        if (typeof url === 'string' && url.includes('/auth/me')) {
          return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makeMeResponse(memberships)) });
        }
        if (typeof url === 'string' && url.includes('/tasks')) {
          return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ tasks }) });
        }
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makeSessionsResponse()) });
      }),
    );
    renderPage();
    await waitFor(() => expect(screen.getByTestId('create-session-button')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('create-session-button'));
    // Open the combobox to enumerate its options.
    fireEvent.click(screen.getByTestId('task-combobox-trigger'));
    await waitFor(() => expect(screen.getByTestId('task-combobox-list')).toBeInTheDocument());

    // Own task + template are offered; the other workspace's private task is not.
    expect(screen.getByTestId('task-option-c1-private')).toBeInTheDocument();
    expect(screen.getByTestId('task-option-tmpl')).toBeInTheDocument();
    expect(screen.queryByTestId('task-option-c2-private')).not.toBeInTheDocument();
  });

  it('defaults the task selection to a workspace-usable task, not a cross-workspace one (D-1)', async () => {
    localStorage.setItem('vibony.workspace', 'c1');
    const memberships = [
      { workspace_id: 'c1', company_name: 'Acme', role: 'admin' as const, plan: 'trial', trial_ends_at: null },
      { workspace_id: 'c2', company_name: 'Beta', role: 'admin' as const, plan: 'trial', trial_ends_at: null },
    ];
    const tmpl = makeTasks()[0]!;
    // c2's task is first overall — the old code seeded it (tasks[0]) → backend 400.
    const tasks = [
      { ...tmpl, id: 't-c2', company_id: 'c2', is_template: false, slug: 'c2-private', title: 'C2 Private' },
      { ...tmpl, id: 't-c1', company_id: 'c1', is_template: false, slug: 'c1-private', title: 'C1 Private' },
    ];
    let postBody: Record<string, unknown> | null = null;
    const fetchMock = vi.fn().mockImplementation((url: string, opts?: RequestInit) => {
      if (typeof url === 'string' && url.includes('/auth/me')) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makeMeResponse(memberships)) });
      }
      if (typeof url === 'string' && url.includes('/tasks')) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ tasks }) });
      }
      if (typeof url === 'string' && url === '/sessions' && opts?.method === 'POST') {
        postBody = JSON.parse(opts.body as string);
        return Promise.resolve({
          ok: true,
          status: 201,
          json: () =>
            Promise.resolve({
              session_code: 'SCOP-0001',
              session_status: 'pending',
              protocol_version: 7,
              ws_token: '',
              task_id: 'c1-private',
              task_dir_name: 'c1-private',
              ai_config: { primary_model: 'deepseek/deepseek-chat', fallback_models: [] },
              bundle_sha256: null,
              created_at: '2026-06-01T10:00:00Z',
              expires_at: '2026-06-01T18:00:00Z',
              candidate_token: 'cand',
              candidate_url: 'http://localhost:8000/candidate?token=cand',
              mode: 'live',
              time_limit_seconds: null,
            }),
        });
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makeSessionsResponse()) });
    });
    vi.stubGlobal('fetch', fetchMock);

    renderPage();
    await waitFor(() => expect(screen.getByTestId('create-session-button')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('create-session-button'));
    await act(async () => {
      fireEvent.submit(screen.getByTestId('create-form'));
    });

    await waitFor(() => expect(screen.getByTestId('created-code')).toBeInTheDocument());
    expect(postBody).not.toBeNull();
    expect(postBody!.task_id).toBe('c1-private');
    expect(postBody!.workspace_id).toBe('c1');
  });

  it('shows the verify-email banner for an unverified user', async () => {
    const meUnverified = {
      user: { id: 'u1', email: 'alice@acme.com', created_at: '2026-05-01T00:00:00Z', email_verified: false },
      memberships: [{ workspace_id: 'c1', company_name: 'Acme', role: 'admin', plan: 'trial', trial_ends_at: null }],
    };
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string) => {
        if (typeof url === 'string' && url.includes('/auth/me')) {
          return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(meUnverified) });
        }
        if (typeof url === 'string' && url.includes('/tasks')) {
          return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ tasks: makeTasks() }) });
        }
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makeSessionsResponse()) });
      }),
    );
    renderPage();
    await waitFor(() => expect(screen.getByTestId('verify-email-banner')).toBeInTheDocument());
  });

  it('create session shows the candidate URL', async () => {
    const fetchMock = vi.fn().mockImplementation((url: string, opts?: RequestInit) => {
      if (typeof url === 'string' && url.includes('/auth/me')) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makeMeResponse()) });
      }
      if (typeof url === 'string' && url.includes('/tasks')) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ tasks: makeTasks() }) });
      }
      if (typeof url === 'string' && url === '/sessions' && opts?.method === 'POST') {
        return Promise.resolve({
          ok: true,
          status: 201,
          json: () =>
            Promise.resolve({
              session_code: 'WWWW-9999',
              session_status: 'pending',
              protocol_version: 5,
              ws_token: '',
              task_id: 'task-01-training-loop',
              task_dir_name: 'training-loop',
              ai_config: { primary_model: 'deepseek/deepseek-chat', fallback_models: [] },
              bundle_sha256: null,
              created_at: '2026-05-20T10:00:00Z',
              expires_at: '2026-05-20T18:00:00Z',
              candidate_token: 'web-token-xyz',
              candidate_url: 'http://localhost:8000/candidate/WWWW-9999?token=web-token-xyz',
            }),
        });
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makeSessionsResponse()) });
    });
    vi.stubGlobal('fetch', fetchMock);

    renderPage();
    await waitFor(() => expect(screen.getByTestId('create-session-button')).toBeInTheDocument());

    fireEvent.click(screen.getByTestId('create-session-button'));
    await act(async () => {
      fireEvent.submit(screen.getByTestId('create-form'));
    });

    await waitFor(() => expect(screen.getByTestId('created-code')).toBeInTheDocument());

    // The candidate URL is the only entry point (no legacy CLI command block).
    expect(screen.getByTestId('candidate-url')).toBeInTheDocument();
    expect(screen.getByTestId('candidate-url')).toHaveTextContent('http://localhost:8000/candidate/WWWW-9999');
    expect(screen.queryByTestId('candidate-command')).not.toBeInTheDocument();
  });
});
