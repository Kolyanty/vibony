import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { TeamPage } from '../pages/TeamPage';

const COMPANY_ID = 'c1';

function makeMe(role: 'admin' | 'interviewer' = 'admin') {
  return {
    user: { id: 'u1', email: 'alice@acme.com', created_at: '2026-05-01T00:00:00Z', email_verified: true },
    memberships: [{ workspace_id: COMPANY_ID, company_name: 'Acme', role }],
  };
}

function makeMembers() {
  return {
    members: [
      { user_id: 'u1', email: 'alice@acme.com', role: 'admin', joined_at: '2026-04-01T00:00:00Z' },
    ],
  };
}

function makeInvite(overrides: Record<string, unknown> = {}) {
  return {
    id: 'inv-1',
    workspace_id: COMPANY_ID,
    token: 'tok-abc',
    email: 'bob@example.com',
    role: 'interviewer',
    invited_by: 'u1',
    created_at: '2026-05-15T00:00:00Z',
    expires_at: '2026-05-22T00:00:00Z',
    accepted_at: null,
    accepted_by: null,
    status: 'pending',
    accept_url: '/invite/tok-abc',
    ...overrides,
  };
}

function setupFetch(overrides: Record<string, unknown> = {}) {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation((url: string, opts?: RequestInit) => {
      if (url.includes('/auth/me')) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve(makeMe()),
        });
      }
      if (url.includes('/members')) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve(makeMembers()),
        });
      }
      if (url.includes('/invites') && opts?.method === 'POST') {
        return Promise.resolve({
          ok: true,
          status: 201,
          json: () => Promise.resolve({ ...makeInvite(), ...(overrides.createdInvite ?? {}) }),
        });
      }
      if (url.includes('/invites/') && opts?.method === 'DELETE') {
        return Promise.resolve({ ok: true, status: 204 });
      }
      if (url.includes('/invites') && !opts?.method) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve([makeInvite()]),
        });
      }
      if (url.includes('/audit')) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ entries: (overrides.audit as unknown[]) ?? [] }),
        });
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve([]) });
    }),
  );
}

// Two members: alice (current user u1, admin) + bob (u2, interviewer).
function makeTwoMembers() {
  return {
    members: [
      { user_id: 'u1', email: 'alice@acme.com', role: 'admin', joined_at: '2026-04-01T00:00:00Z' },
      { user_id: 'u2', email: 'bob@acme.com', role: 'interviewer', joined_at: '2026-04-05T00:00:00Z' },
    ],
  };
}

function setupMemberMgmtFetch(handlers: {
  onPatch?: (body: Record<string, unknown>) => { status: number; body?: unknown };
  onDelete?: () => { status: number; body?: unknown };
} = {}) {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation((url: string, opts?: RequestInit) => {
      if (url.includes('/auth/me')) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makeMe()) });
      }
      // Member mutation endpoints carry a trailing /{user_id}.
      if (url.includes('/members/') && opts?.method === 'PATCH') {
        const res = handlers.onPatch?.(JSON.parse(opts.body as string)) ?? {
          status: 200,
          body: { user_id: 'u2', email: 'bob@acme.com', role: 'admin', joined_at: '2026-04-05T00:00:00Z' },
        };
        return Promise.resolve({ ok: res.status < 400, status: res.status, json: () => Promise.resolve(res.body ?? {}) });
      }
      if (url.includes('/members/') && opts?.method === 'DELETE') {
        const res = handlers.onDelete?.() ?? { status: 204 };
        return Promise.resolve({ ok: res.status < 400, status: res.status, json: () => Promise.resolve(res.body ?? {}) });
      }
      if (url.includes('/members')) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makeTwoMembers()) });
      }
      if (url.includes('/invites')) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve([]) });
      }
      if (url.includes('/audit')) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ entries: [] }) });
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve([]) });
    }),
  );
}

// Fetch stub for the delete-workspace flow. `me` overrides the membership list;
// `onDelete` observes the DELETE /workspaces/{id} call (no /members|/invites suffix).
function setupDeleteFetch(handlers: { me?: unknown; onDelete?: () => { status: number; body?: unknown } } = {}) {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation((url: string, opts?: RequestInit) => {
      if (url.includes('/auth/me')) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(handlers.me ?? makeMe()) });
      }
      if (opts?.method === 'DELETE' && /\/workspaces\/[^/]+$/.test(url)) {
        const res = handlers.onDelete?.() ?? { status: 204 };
        return Promise.resolve({ ok: res.status < 400, status: res.status, json: () => Promise.resolve(res.body ?? {}) });
      }
      if (url.includes('/members')) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makeMembers()) });
      }
      if (url.includes('/invites')) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve([]) });
      }
      if (url.includes('/audit')) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ entries: [] }) });
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve([]) });
    }),
  );
}

function renderPage() {
  const router = createMemoryRouter(
    [
      { path: '/team', element: <TeamPage /> },
      { path: '/login', element: <p data-testid="login-page">Login</p> },
      { path: '/', element: <p data-testid="sessions-page">Sessions</p> },
    ],
    { initialEntries: ['/team'] },
  );
  return render(<RouterProvider router={router} />);
}

describe('TeamPage', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('admin sees members and invites tables', async () => {
    setupFetch();
    renderPage();
    await waitFor(() => expect(screen.getByTestId('members-table')).toBeInTheDocument());
    expect(screen.getByTestId('member-row-alice@acme.com')).toBeInTheDocument();
    expect(screen.getByTestId('invites-table')).toBeInTheDocument();
    expect(screen.getByTestId('invite-row-inv-1')).toBeInTheDocument();
    expect(screen.getByTestId('invite-button')).toBeInTheDocument();
  });

  it('non-admin sees no-access message', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string) => {
        if (url.includes('/auth/me')) {
          return Promise.resolve({
            ok: true,
            status: 200,
            json: () => Promise.resolve(makeMe('interviewer')),
          });
        }
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve([]) });
      }),
    );
    renderPage();
    await waitFor(() => expect(screen.getByTestId('no-access-message')).toBeInTheDocument());
    expect(screen.queryByTestId('invite-button')).not.toBeInTheDocument();
  });

  it('invite modal opens and submits', async () => {
    setupFetch();
    renderPage();
    await waitFor(() => expect(screen.getByTestId('invite-button')).toBeInTheDocument());

    fireEvent.click(screen.getByTestId('invite-button'));
    expect(screen.getByTestId('invite-modal')).toBeInTheDocument();

    fireEvent.change(screen.getByTestId('invite-email-input'), {
      target: { value: 'newbie@example.com' },
    });

    await act(async () => {
      fireEvent.submit(screen.getByTestId('invite-form'));
    });

    await waitFor(() => expect(screen.getByTestId('invite-accept-url')).toBeInTheDocument());
  });

  it('revoke button calls DELETE and updates status', async () => {
    setupFetch();
    renderPage();
    await waitFor(() => expect(screen.getByTestId('invite-row-inv-1')).toBeInTheDocument());

    fireEvent.click(screen.getByTestId('revoke-invite-inv-1'));
    // Confirm via the AlertDialog (was window.confirm).
    await act(async () => {
      fireEvent.click(await screen.findByTestId('revoke-invite-confirm-confirm'));
    });

    await waitFor(() => {
      const fetchMock = vi.mocked(globalThis.fetch);
      const deleteCalls = fetchMock.mock.calls.filter((args) => {
        const url = args[0];
        const opts = args[1];
        return typeof url === 'string' && url.includes('/invites/tok-abc') && opts?.method === 'DELETE';
      });
      expect(deleteCalls.length).toBeGreaterThan(0);
    });
  });

  it('own row has no role select / remove; other row does', async () => {
    setupMemberMgmtFetch();
    renderPage();
    await waitFor(() => expect(screen.getByTestId('member-row-bob@acme.com')).toBeInTheDocument());
    // Self (alice = u1): no controls.
    expect(screen.queryByTestId('member-role-alice@acme.com')).not.toBeInTheDocument();
    expect(screen.queryByTestId('member-remove-alice@acme.com')).not.toBeInTheDocument();
    // Other (bob): role select + remove present.
    expect(screen.getByTestId('member-role-bob@acme.com')).toBeInTheDocument();
    expect(screen.getByTestId('member-remove-bob@acme.com')).toBeInTheDocument();
  });

  it('changing a member role PATCHes and updates the row', async () => {
    let patched: Record<string, unknown> | null = null;
    setupMemberMgmtFetch({
      onPatch: (body) => {
        patched = body;
        return { status: 200, body: { user_id: 'u2', email: 'bob@acme.com', role: 'admin', joined_at: '2026-04-05T00:00:00Z' } };
      },
    });
    renderPage();
    await waitFor(() => expect(screen.getByTestId('member-role-bob@acme.com')).toBeInTheDocument());
    await act(async () => {
      fireEvent.change(screen.getByTestId('member-role-bob@acme.com'), { target: { value: 'admin' } });
    });
    await waitFor(() => expect(patched).toEqual({ role: 'admin' }));
    expect((screen.getByTestId('member-role-bob@acme.com') as HTMLSelectElement).value).toBe('admin');
  });

  it('removing a member deletes the row', async () => {
    setupMemberMgmtFetch();
    renderPage();
    await waitFor(() => expect(screen.getByTestId('member-remove-bob@acme.com')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('member-remove-bob@acme.com'));
    // Confirm via the AlertDialog (was window.confirm).
    await act(async () => {
      fireEvent.click(await screen.findByTestId('member-remove-confirm-confirm'));
    });
    await waitFor(() => expect(screen.queryByTestId('member-row-bob@acme.com')).not.toBeInTheDocument());
    // Self row remains.
    expect(screen.getByTestId('member-row-alice@acme.com')).toBeInTheDocument();
  });

  it('surfaces the last_admin 409 as a localized error', async () => {
    setupMemberMgmtFetch({
      onPatch: () => ({ status: 409, body: { detail: { reason: 'last_admin' } } }),
    });
    renderPage();
    await waitFor(() => expect(screen.getByTestId('member-role-bob@acme.com')).toBeInTheDocument());
    await act(async () => {
      fireEvent.change(screen.getByTestId('member-role-bob@acme.com'), { target: { value: 'admin' } });
    });
    await waitFor(() => expect(screen.getByTestId('member-error')).toBeInTheDocument());
    expect(screen.getByTestId('member-error')).toHaveTextContent(/at least one admin/i);
  });

  it('disables the role select while a PATCH is in flight (D-7)', async () => {
    let resolvePatch: ((v: { status: number; body?: unknown }) => void) | null = null;
    const patchGate = new Promise<{ status: number; body?: unknown }>((res) => {
      resolvePatch = res;
    });
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string, opts?: RequestInit) => {
        if (url.includes('/auth/me')) {
          return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makeMe()) });
        }
        if (url.includes('/members/') && opts?.method === 'PATCH') {
          // Stays pending until the test opens the gate.
          return patchGate.then((res) => ({
            ok: res.status < 400,
            status: res.status,
            json: () => Promise.resolve(res.body ?? {}),
          }));
        }
        if (url.includes('/members')) {
          return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makeTwoMembers()) });
        }
        if (url.includes('/invites')) {
          return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve([]) });
        }
        if (url.includes('/audit')) {
          return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ entries: [] }) });
        }
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve([]) });
      }),
    );

    renderPage();
    await waitFor(() => expect(screen.getByTestId('member-role-bob@acme.com')).toBeInTheDocument());
    const select = () => screen.getByTestId('member-role-bob@acme.com') as HTMLSelectElement;
    expect(select().disabled).toBe(false);

    await act(async () => {
      fireEvent.change(select(), { target: { value: 'admin' } });
    });
    // PATCH is gated open → the select is disabled so a second change can't race it.
    expect(select().disabled).toBe(true);

    await act(async () => {
      resolvePatch!({
        status: 200,
        body: { user_id: 'u2', email: 'bob@acme.com', role: 'admin', joined_at: '2026-04-05T00:00:00Z' },
      });
      await patchGate;
    });
    await waitFor(() => expect(select().disabled).toBe(false));
    expect(select().value).toBe('admin');
  });

  it('delete-workspace confirm is gated on typing the workspace name', async () => {
    let deleted = false;
    setupDeleteFetch({ onDelete: () => { deleted = true; return { status: 204 }; } });
    renderPage();
    await waitFor(() => expect(screen.getByTestId('danger-zone')).toBeInTheDocument());

    fireEvent.click(screen.getByTestId('delete-workspace-button'));
    const confirm = () => screen.getByTestId('delete-workspace-confirm') as HTMLButtonElement;
    await waitFor(() => expect(screen.getByTestId('delete-workspace-input')).toBeInTheDocument());
    // Disabled until the typed name matches exactly.
    expect(confirm().disabled).toBe(true);
    fireEvent.change(screen.getByTestId('delete-workspace-input'), { target: { value: 'Nope' } });
    expect(confirm().disabled).toBe(true);
    fireEvent.change(screen.getByTestId('delete-workspace-input'), { target: { value: 'Acme' } });
    expect(confirm().disabled).toBe(false);

    await act(async () => {
      fireEvent.submit(screen.getByTestId('delete-workspace-form'));
    });
    await waitFor(() => expect(deleted).toBe(true));
    // Deleting the only workspace routes to the home / zero-workspace onboarding.
    await waitFor(() => expect(screen.getByTestId('sessions-page')).toBeInTheDocument());
  });

  it('surfaces a delete-workspace error and keeps the page', async () => {
    setupDeleteFetch({ onDelete: () => ({ status: 403, body: { detail: { reason: 'forbidden' } } }) });
    renderPage();
    await waitFor(() => expect(screen.getByTestId('danger-zone')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('delete-workspace-button'));
    fireEvent.change(await screen.findByTestId('delete-workspace-input'), { target: { value: 'Acme' } });
    await act(async () => {
      fireEvent.submit(screen.getByTestId('delete-workspace-form'));
    });
    await waitFor(() => expect(screen.getByTestId('delete-workspace-error')).toBeInTheDocument());
    // Still on the team page (not navigated away).
    expect(screen.queryByTestId('sessions-page')).not.toBeInTheDocument();
  });

  it('renders the activity log with localized lines', async () => {
    setupFetch({
      audit: [
        {
          id: 'a1',
          action: 'member.invited',
          actor_email: 'alice@acme.com',
          target_type: 'invite',
          target_id: 'i1',
          // Email now resolved server-side into target_email.
          target_email: 'bob@e.com',
          metadata: { role: 'interviewer' },
          created_at: '2026-05-20T10:00:00Z',
        },
      ],
    });
    renderPage();
    await waitFor(() => expect(screen.getByTestId('audit-list')).toBeInTheDocument());
    const row = screen.getByTestId('audit-row-a1');
    expect(row).toHaveTextContent('alice@acme.com');
    expect(row).toHaveTextContent('bob@e.com');
    // Dotted i18n key resolved to the localized template, not the raw action fallback.
    expect(row).not.toHaveTextContent('member.invited');
  });

  it('team link appears for admin on sessions list', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string) => {
        if (url.includes('/auth/me')) {
          return Promise.resolve({
            ok: true,
            status: 200,
            json: () => Promise.resolve(makeMe('admin')),
          });
        }
        if (url.includes('/tasks')) {
          return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ tasks: [] }) });
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ sessions: [] }),
        });
      }),
    );
    const { SessionsListPage } = await import('../pages/SessionsListPage');
    const router = createMemoryRouter(
      [
        { path: '/', element: <SessionsListPage /> },
        { path: '/team', element: <TeamPage /> },
      ],
      { initialEntries: ['/'] },
    );
    render(<RouterProvider router={router} />);
    await waitFor(() => expect(screen.getByTestId('team-link')).toBeInTheDocument());
  });
});
