import { render, screen, waitFor } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { HomeGate } from '../components/HomeGate';

function renderHome(fetchImpl: ReturnType<typeof vi.fn>) {
  vi.stubGlobal('fetch', fetchImpl);
  const router = createMemoryRouter(
    [
      { path: '/', element: <HomeGate /> },
      { path: '/login', element: <p data-testid="login">login</p> },
    ],
    { initialEntries: ['/'] },
  );
  return render(<RouterProvider router={router} />);
}

describe('HomeGate', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('shows the public landing for an anonymous visitor (getMe 401)', async () => {
    renderHome(
      vi.fn().mockResolvedValue({ ok: false, status: 401, json: () => Promise.resolve({}) }),
    );
    await waitFor(() => expect(screen.getByText('— and catch its mistakes')).toBeInTheDocument());
    // Anonymous `/` must NOT bounce to /login (it's a public page).
    expect(screen.queryByTestId('login')).not.toBeInTheDocument();
  });

  it('shows a retryable offline screen on a network error (not the public landing)', async () => {
    renderHome(vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument(),
    );
    // A backend outage for a (possibly logged-in) user must NOT silently show the
    // instance welcome page, nor bounce to /login.
    expect(screen.queryByText('— and catch its mistakes')).not.toBeInTheDocument();
    expect(screen.queryByTestId('login')).not.toBeInTheDocument();
  });

  it('shows the dashboard when authenticated', async () => {
    renderHome(
      vi.fn().mockImplementation((url: string) => {
        if (String(url).includes('/auth/me')) {
          return Promise.resolve({
            ok: true,
            status: 200,
            json: () =>
              Promise.resolve({
                user: { id: 'u1', email: 'a@b.com', created_at: '2026-05-01T00:00:00Z' },
                memberships: [],
              }),
          });
        }
        // listSessions expects { sessions: [...] }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ sessions: [] }),
        });
      }),
    );
    await waitFor(() =>
      expect(screen.getByTestId('create-session-button')).toBeInTheDocument(),
    );
    expect(screen.queryByText('— and catch its mistakes')).not.toBeInTheDocument();
  });
});
