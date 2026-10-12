import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AcceptInvitePage } from '../pages/AcceptInvitePage';

const TOKEN = 'test-token-123';

function makePreview(status = 'pending') {
  return {
    company_name: 'Acme Corp',
    inviter_email: 'alice@acme.com',
    role: 'interviewer',
    expires_at: '2026-05-22T00:00:00Z',
    status,
  };
}

function makeMe() {
  return {
    user: { id: 'u2', email: 'bob@example.com', created_at: '2026-05-01T00:00:00Z' },
    memberships: [{ workspace_id: 'c2', company_name: 'Bob Co', role: 'admin' as const }],
  };
}

function renderPage(token = TOKEN) {
  const router = createMemoryRouter(
    [
      { path: '/invite/:token', element: <AcceptInvitePage /> },
      { path: '/', element: <p data-testid="sessions-page">Sessions</p> },
      { path: '/login', element: <p data-testid="login-page">Login</p> },
    ],
    { initialEntries: [`/invite/${token}`] },
  );
  return render(<RouterProvider router={router} />);
}

describe('AcceptInvitePage', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('logged-in user sees join button with company name', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string) => {
        if (url.includes('/auth/me')) {
          return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makeMe()) });
        }
        if (url.includes(`/invites/${TOKEN}`) && !url.includes('/accept')) {
          return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makePreview()) });
        }
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
      }),
    );
    renderPage();
    await waitFor(() => expect(screen.getByTestId('invite-logged-in')).toBeInTheDocument());
    expect(screen.getByTestId('join-button')).toBeInTheDocument();
    expect(screen.getAllByText(/Acme Corp/).length).toBeGreaterThan(0);
  });

  it('join button calls accept and shows joined screen', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string, opts?: RequestInit) => {
        if (url.includes('/auth/me')) {
          return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makeMe()) });
        }
        if (url.includes(`/invites/${TOKEN}/accept`) && opts?.method === 'POST') {
          return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ status: 'ok' }) });
        }
        if (url.includes(`/invites/${TOKEN}`)) {
          return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makePreview()) });
        }
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
      }),
    );
    renderPage();
    await waitFor(() => expect(screen.getByTestId('join-button')).toBeInTheDocument());

    await act(async () => {
      fireEvent.click(screen.getByTestId('join-button'));
    });

    await waitFor(() => expect(screen.getByTestId('invite-joined')).toBeInTheDocument());
    expect(screen.getByTestId('go-to-sessions')).toBeInTheDocument();
  });

  it('not-logged-in user sees signup form', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string) => {
        if (url.includes('/auth/me')) {
          return Promise.resolve({ ok: false, status: 401, json: () => Promise.resolve({ detail: { reason: 'unauthorized' } }) });
        }
        if (url.includes(`/invites/${TOKEN}`)) {
          return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makePreview()) });
        }
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
      }),
    );
    renderPage();
    await waitFor(() => expect(screen.getByTestId('invite-signup-form')).toBeInTheDocument());
    expect(screen.getByTestId('signup-email')).toBeInTheDocument();
    expect(screen.getByTestId('signup-password')).toBeInTheDocument();
  });

  it('expired invite shows unavailable message', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string) => {
        if (url.includes('/auth/me')) {
          return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makeMe()) });
        }
        if (url.includes(`/invites/${TOKEN}`)) {
          return Promise.resolve({
            ok: true,
            status: 200,
            json: () => Promise.resolve(makePreview('expired')),
          });
        }
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
      }),
    );
    renderPage();
    await waitFor(() => expect(screen.getByTestId('invite-unavailable')).toBeInTheDocument());
  });

  it('accept failure maps a backend reason to a localized message, not a raw string', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string, opts?: RequestInit) => {
        if (url.includes('/auth/me')) {
          return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makeMe()) });
        }
        if (url.includes(`/invites/${TOKEN}/accept`) && opts?.method === 'POST') {
          return Promise.resolve({
            ok: false,
            status: 410,
            json: () => Promise.resolve({ detail: { reason: 'inviter_unauthorized' } }),
          });
        }
        if (url.includes(`/invites/${TOKEN}`)) {
          return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makePreview()) });
        }
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
      }),
    );
    renderPage();
    await waitFor(() => expect(screen.getByTestId('join-button')).toBeInTheDocument());

    await act(async () => {
      fireEvent.click(screen.getByTestId('join-button'));
    });

    await waitFor(() =>
      expect(screen.getByText('The person who sent this invite no longer has access.')).toBeInTheDocument(),
    );
    expect(screen.queryByText(/inviter_unauthorized|HTTP/)).not.toBeInTheDocument();
  });

  it('invalid token shows not-found message', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string) => {
        if (url.includes('/auth/me')) {
          return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makeMe()) });
        }
        if (url.includes('/invites/')) {
          return Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve({ detail: { reason: 'not_found' } }) });
        }
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
      }),
    );
    renderPage('bad-token-xyz');
    await waitFor(() => expect(screen.getByTestId('invite-not-found')).toBeInTheDocument());
  });
});
