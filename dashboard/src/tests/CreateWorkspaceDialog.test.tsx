import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { CreateWorkspaceDialog } from '../components/CreateWorkspaceDialog';

function noop() {}

describe('CreateWorkspaceDialog', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('shows a localized error (not "HTTP 4xx") when create fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 422,
        json: () => Promise.resolve({ detail: [{ msg: 'name too long' }] }),
      }),
    );

    render(<CreateWorkspaceDialog open onOpenChange={noop} onCreated={noop} />);
    fireEvent.change(screen.getByTestId('workspace-name-input'), { target: { value: 'X' } });
    await act(async () => {
      fireEvent.submit(screen.getByTestId('create-workspace-form'));
    });

    const err = await screen.findByTestId('create-workspace-error');
    // A real backend validation message is surfaced; never a bare HTTP code.
    expect(err).toHaveTextContent('name too long');
    expect(err.textContent ?? '').not.toMatch(/^HTTP/);
  });

  it('clears a stale error when the dialog is reopened', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 409,
        json: () => Promise.resolve({ detail: { reason: 'conflict' } }),
      }),
    );

    const { rerender } = render(
      <CreateWorkspaceDialog open onOpenChange={noop} onCreated={noop} />,
    );
    fireEvent.change(screen.getByTestId('workspace-name-input'), { target: { value: 'Beta' } });
    await act(async () => {
      fireEvent.submit(screen.getByTestId('create-workspace-form'));
    });
    await waitFor(() => expect(screen.getByTestId('create-workspace-error')).toBeInTheDocument());

    // Close, then reopen → the error must be gone.
    rerender(<CreateWorkspaceDialog open={false} onOpenChange={noop} onCreated={noop} />);
    rerender(<CreateWorkspaceDialog open onOpenChange={noop} onCreated={noop} />);
    await waitFor(() =>
      expect(screen.queryByTestId('create-workspace-error')).not.toBeInTheDocument(),
    );
  });
});
