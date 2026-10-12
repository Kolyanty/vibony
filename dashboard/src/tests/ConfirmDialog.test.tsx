import { act, fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { useState } from 'react';
import { ConfirmDialog } from '../components/ConfirmDialog';

/** Drives ConfirmDialog through a tiny controlled host (open state + a trigger). */
function Host({ onConfirm, destructive }: { onConfirm: () => void; destructive?: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" data-testid="open" onClick={() => setOpen(true)}>
        open
      </button>
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
        title="Delete it?"
        description="This cannot be undone."
        confirmLabel="Delete"
        cancelLabel="Cancel"
        onConfirm={onConfirm}
        destructive={destructive}
        testId="x-confirm"
      />
    </>
  );
}

describe('ConfirmDialog (F1)', () => {
  it('opens, renders title/description, and fires onConfirm + closes on confirm', async () => {
    const onConfirm = vi.fn();
    render(<Host onConfirm={onConfirm} />);

    expect(screen.queryByTestId('x-confirm')).toBeNull();
    fireEvent.click(screen.getByTestId('open'));

    expect(await screen.findByTestId('x-confirm')).toBeInTheDocument();
    expect(screen.getByText('Delete it?')).toBeInTheDocument();
    expect(screen.getByText('This cannot be undone.')).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByTestId('x-confirm-confirm'));
    });
    expect(onConfirm).toHaveBeenCalledTimes(1);
    // Radix closes the AlertDialog after the action.
    expect(screen.queryByTestId('x-confirm')).toBeNull();
  });

  it('cancel closes without firing onConfirm', async () => {
    const onConfirm = vi.fn();
    render(<Host onConfirm={onConfirm} />);
    fireEvent.click(screen.getByTestId('open'));
    await screen.findByTestId('x-confirm');

    await act(async () => {
      fireEvent.click(screen.getByTestId('x-confirm-cancel'));
    });
    expect(onConfirm).not.toHaveBeenCalled();
    expect(screen.queryByTestId('x-confirm')).toBeNull();
  });

  it('destructive styles the confirm action with the destructive variant', async () => {
    render(<Host onConfirm={() => {}} destructive />);
    fireEvent.click(screen.getByTestId('open'));
    const confirm = await screen.findByTestId('x-confirm-confirm');
    expect(confirm.className).toContain('bg-destructive');
  });
});
