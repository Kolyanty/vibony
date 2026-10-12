import { render, screen, waitFor } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { RequireAuth } from '../components/RequireAuth';

function renderWithRouter(
  authenticated: boolean,
  initialPath = '/',
) {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue(
      authenticated
        ? {
            ok: true,
            status: 200,
            json: () =>
              Promise.resolve({
                user: { id: 'u1', email: 'a@b.com', created_at: '2026-05-01T00:00:00Z' },
                memberships: [],
              }),
          }
        : { ok: false, status: 401, json: () => Promise.resolve({}) },
    ),
  );

  const router = createMemoryRouter(
    [
      {
        path: '/',
        element: (
          <RequireAuth>
            <p data-testid="protected">Protected content</p>
          </RequireAuth>
        ),
      },
      { path: '/login', element: <p data-testid="login-page">Login</p> },
    ],
    { initialEntries: [initialPath] },
  );
  return render(<RouterProvider router={router} />);
}

describe('RequireAuth', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('renders children when authenticated', async () => {
    renderWithRouter(true);
    await waitFor(() => expect(screen.getByTestId('protected')).toBeInTheDocument());
  });

  it('redirects to /login when 401', async () => {
    renderWithRouter(false);
    await waitFor(() => expect(screen.getByTestId('login-page')).toBeInTheDocument());
    expect(screen.queryByTestId('protected')).not.toBeInTheDocument();
  });
});
