import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { VerifyEmailBanner } from '../components/VerifyEmailBanner';
import { VerifyEmailPage } from '../pages/VerifyEmailPage';

describe('VerifyEmailBanner', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('resends the verification email and shows the sent state', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 202, json: () => Promise.resolve({}) });
    vi.stubGlobal('fetch', fetchMock);

    render(<VerifyEmailBanner email="a@acme.com" />);
    expect(screen.getByTestId('verify-email-banner')).toHaveTextContent('a@acme.com');

    await act(async () => {
      fireEvent.click(screen.getByTestId('verify-email-resend'));
    });
    await waitFor(() => expect(screen.getByTestId('verify-email-banner')).toHaveTextContent(/sent/i));
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/auth/verify-email/resend'),
      expect.objectContaining({ method: 'POST' }),
    );
  });

  it('can be dismissed', () => {
    render(<VerifyEmailBanner email="a@acme.com" />);
    fireEvent.click(screen.getByTestId('verify-email-dismiss'));
    expect(screen.queryByTestId('verify-email-banner')).not.toBeInTheDocument();
  });
});

describe('VerifyEmailPage', () => {
  afterEach(() => vi.unstubAllGlobals());

  function renderAt(path: string) {
    const router = createMemoryRouter(
      [
        { path: '/verify-email', element: <VerifyEmailPage /> },
        { path: '/', element: <p>Home</p> },
      ],
      { initialEntries: [path] },
    );
    return render(<RouterProvider router={router} />);
  }

  it('verifies a token and shows success', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, status: 200, json: () => Promise.resolve({}) }));
    renderAt('/verify-email?token=good');
    await waitFor(() => expect(screen.getByTestId('verify-email-ok')).toBeInTheDocument());
  });

  it('shows an error on a 400 (invalid/expired) token', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 400, json: () => Promise.resolve({ detail: { reason: 'invalid_token' } }) }));
    renderAt('/verify-email?token=bad');
    await waitFor(() => expect(screen.getByTestId('verify-email-error')).toBeInTheDocument());
  });

  it('shows an error when no token is present', async () => {
    vi.stubGlobal('fetch', vi.fn());
    renderAt('/verify-email');
    await waitFor(() => expect(screen.getByTestId('verify-email-error')).toBeInTheDocument());
  });
});
