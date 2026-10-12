import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { LoginPage } from '../pages/LoginPage';

function makePasswordConfig() {
  return { mode: 'password', oidc_login_url: null };
}

function makeOidcConfig() {
  return { mode: 'oidc', oidc_login_url: '/auth/oidc/start' };
}

function renderLoginPage() {
  const router = createMemoryRouter(
    [
      { path: '/login', element: <LoginPage /> },
      { path: '/', element: <p data-testid="home">Home</p> },
    ],
    { initialEntries: ['/login'] },
  );
  return render(<RouterProvider router={router} />);
}

describe('LoginPage', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('shows email/password form in password mode', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: () => Promise.resolve(makePasswordConfig()),
      }),
    );
    renderLoginPage();
    await waitFor(() => expect(screen.queryByTestId('login-form')).toBeInTheDocument());
    expect(screen.getByTestId('login-email')).toBeInTheDocument();
    expect(screen.getByTestId('login-password')).toBeInTheDocument();
    expect(screen.queryByTestId('login-oidc-button')).not.toBeInTheDocument();
  });

  it('shows OIDC button in oidc mode, hides password form', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: () => Promise.resolve(makeOidcConfig()),
      }),
    );
    renderLoginPage();
    await waitFor(() => expect(screen.queryByTestId('login-oidc-button')).toBeInTheDocument());
    expect(screen.queryByTestId('login-form')).not.toBeInTheDocument();
  });

  it('successful login redirects to /', async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (typeof url === 'string' && url.includes('/auth/config')) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve(makePasswordConfig()),
        });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () =>
          Promise.resolve({
            user: { id: 'u1', email: 'a@b.com', created_at: '2026-05-01T00:00:00Z' },
            memberships: [],
          }),
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    renderLoginPage();
    await waitFor(() => expect(screen.queryByTestId('login-form')).toBeInTheDocument());

    fireEvent.change(screen.getByTestId('login-email'), { target: { value: 'a@b.com' } });
    fireEvent.change(screen.getByTestId('login-password'), { target: { value: 'password123' } });
    await act(async () => {
      fireEvent.click(screen.getByTestId('login-submit'));
    });

    await waitFor(() => expect(screen.getByTestId('home')).toBeInTheDocument());
  });

  it('wrong credentials shows error message', async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (typeof url === 'string' && url.includes('/auth/config')) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve(makePasswordConfig()),
        });
      }
      return Promise.resolve({
        ok: false,
        status: 401,
        json: () => Promise.resolve({}),
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    renderLoginPage();
    await waitFor(() => expect(screen.queryByTestId('login-form')).toBeInTheDocument());

    fireEvent.change(screen.getByTestId('login-email'), { target: { value: 'a@b.com' } });
    fireEvent.change(screen.getByTestId('login-password'), { target: { value: 'wrong' } });
    await act(async () => {
      fireEvent.click(screen.getByTestId('login-submit'));
    });

    await waitFor(() => expect(screen.getByTestId('login-error')).toBeInTheDocument());
  });

  it('rate-limited login (429) shows a localized message, not "HTTP 429"', async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (typeof url === 'string' && url.includes('/auth/config')) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makePasswordConfig()) });
      }
      return Promise.resolve({
        ok: false,
        status: 429,
        json: () => Promise.resolve({ detail: { reason: 'rate_limited' } }),
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    renderLoginPage();
    await waitFor(() => expect(screen.queryByTestId('login-form')).toBeInTheDocument());

    fireEvent.change(screen.getByTestId('login-email'), { target: { value: 'a@b.com' } });
    fireEvent.change(screen.getByTestId('login-password'), { target: { value: 'password123' } });
    await act(async () => {
      fireEvent.click(screen.getByTestId('login-submit'));
    });

    const err = await screen.findByTestId('login-error');
    expect(err).toHaveTextContent('Too many attempts. Please try again shortly.');
    expect(err.textContent ?? '').not.toMatch(/HTTP|Could not reach/i);
  });
});
