import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SignupPage } from '../pages/SignupPage';

function renderSignupPage() {
  const router = createMemoryRouter(
    [
      { path: '/signup', element: <SignupPage /> },
      { path: '/login', element: <p data-testid="login-page">Login</p> },
      { path: '/', element: <p data-testid="home">Home</p> },
    ],
    { initialEntries: ['/signup'] },
  );
  return render(<RouterProvider router={router} />);
}

describe('SignupPage', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('shows signup form in password mode', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: () =>
          Promise.resolve({ mode: 'password', oidc_login_url: null }),
      }),
    );
    renderSignupPage();
    await waitFor(() => expect(screen.queryByTestId('signup-form')).toBeInTheDocument());
    expect(screen.getByTestId('signup-email')).toBeInTheDocument();
    expect(screen.getByTestId('signup-company')).toBeInTheDocument();
  });

  it('redirects to /login in oidc mode', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: () =>
          Promise.resolve({ mode: 'oidc', oidc_login_url: '/auth/oidc/start' }),
      }),
    );
    renderSignupPage();
    await waitFor(() => expect(screen.queryByTestId('login-page')).toBeInTheDocument());
    expect(screen.queryByTestId('signup-form')).not.toBeInTheDocument();
  });

  it('successful signup redirects to /', async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (typeof url === 'string' && url.includes('/auth/config')) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () =>
            Promise.resolve({ mode: 'password', oidc_login_url: null }),
        });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () =>
          Promise.resolve({
            user: { id: 'u1', email: 'new@co.com', created_at: '2026-05-01T00:00:00Z' },
            memberships: [{ workspace_id: 'c1', company_name: 'Acme', role: 'admin' }],
          }),
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    renderSignupPage();
    await waitFor(() => expect(screen.queryByTestId('signup-form')).toBeInTheDocument());

    fireEvent.change(screen.getByTestId('signup-company'), { target: { value: 'Acme' } });
    fireEvent.change(screen.getByTestId('signup-email'), { target: { value: 'new@co.com' } });
    fireEvent.change(screen.getByTestId('signup-password'), { target: { value: 'password123' } });
    await act(async () => {
      fireEvent.click(screen.getByTestId('signup-submit'));
    });

    await waitFor(() => expect(screen.getByTestId('home')).toBeInTheDocument());
  });

  it('duplicate email shows error', async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (typeof url === 'string' && url.includes('/auth/config')) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () =>
            Promise.resolve({ mode: 'password', oidc_login_url: null }),
        });
      }
      return Promise.resolve({ ok: false, status: 409, json: () => Promise.resolve({}) });
    });
    vi.stubGlobal('fetch', fetchMock);

    renderSignupPage();
    await waitFor(() => expect(screen.queryByTestId('signup-form')).toBeInTheDocument());

    fireEvent.change(screen.getByTestId('signup-company'), { target: { value: 'Acme' } });
    fireEvent.change(screen.getByTestId('signup-email'), { target: { value: 'exists@co.com' } });
    fireEvent.change(screen.getByTestId('signup-password'), { target: { value: 'password123' } });
    await act(async () => {
      fireEvent.click(screen.getByTestId('signup-submit'));
    });

    await waitFor(() => expect(screen.getByTestId('signup-error')).toBeInTheDocument());
  });

  it('reserved-TLD email (422) shows a validation message, not "HTTP 422"', async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (typeof url === 'string' && url.includes('/auth/config')) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ mode: 'password', oidc_login_url: null }) });
      }
      return Promise.resolve({
        ok: false,
        status: 422,
        json: () => Promise.resolve({ detail: [{ msg: 'value is not a valid email address' }] }),
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    renderSignupPage();
    await waitFor(() => expect(screen.queryByTestId('signup-form')).toBeInTheDocument());

    fireEvent.change(screen.getByTestId('signup-company'), { target: { value: 'Acme' } });
    fireEvent.change(screen.getByTestId('signup-email'), { target: { value: 'a@b.test' } });
    fireEvent.change(screen.getByTestId('signup-password'), { target: { value: 'password123' } });
    await act(async () => {
      fireEvent.click(screen.getByTestId('signup-submit'));
    });

    const err = await screen.findByTestId('signup-error');
    expect(err).toHaveTextContent('Please check the form and try again.');
    expect(err.textContent ?? '').not.toMatch(/HTTP|Could not reach/i);
  });
});
