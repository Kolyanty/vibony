/**
 * FileDiffHistory: baseline handling.
 * A `source: 'baseline'` entry is not shown as its own change; it serves as the
 * `before` content so the first real edit diffs against the scaffold, producing
 * a small diff instead of "whole file added".
 */
import { render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { FileDiffHistory } from '../components/FileDiffHistory';
import type { FileChangeEntry } from '../components/filesTabUtils';
import userEvent from '@testing-library/user-event';

function entry(o: Partial<FileChangeEntry> & { eventId: string; seq: number }): FileChangeEntry {
  return {
    timestamp: '2026-05-21T10:00:00Z',
    source: 'agent',
    diff: null,
    content: '',
    ...o,
  };
}

function bigContent(lines: number): string {
  return Array.from({ length: lines }, (_, i) => `line${i}`).join('\n');
}

describe('FileDiffHistory large-file lazy diff', () => {
  it('shows "Compute diff" button instead of auto-rendering diff for large files', () => {
    const entries: FileChangeEntry[] = [
      entry({ eventId: 'e1', seq: 1, source: 'agent', content: bigContent(600) }),
    ];
    render(<FileDiffHistory entries={entries} path="big.txt" defaultOpen />);

    expect(screen.getByTestId('compute-diff-btn')).toBeInTheDocument();
    // No diff output (SyntaxHighlighter) until user requests it
    expect(screen.queryByText(/@@/)).not.toBeInTheDocument();
  });

  it('shows Computing state then displays diff after clicking "Compute diff"', async () => {
    const user = userEvent.setup();
    const entries: FileChangeEntry[] = [
      entry({ eventId: 'b', seq: 1, source: 'baseline', content: bigContent(600) }),
      entry({
        eventId: 'e1',
        seq: 2,
        source: 'manual',
        content: bigContent(600).replace('line0', 'CHANGED'),
      }),
    ];
    render(<FileDiffHistory entries={entries} path="big.txt" defaultOpen />);

    const btn = screen.getByTestId('compute-diff-btn');
    await user.click(btn);

    // Immediately after click: button gone (pending state), "Computing…" shown
    expect(screen.queryByTestId('compute-diff-btn')).not.toBeInTheDocument();

    // After setTimeout fires (idle fallback in jsdom): diff appears
    await waitFor(() => {
      expect(screen.getByText(/CHANGED/)).toBeInTheDocument();
    });
  });
});

describe('FileDiffHistory baseline', () => {
  it('excludes the baseline entry from the list and diffs the first edit against it', () => {
    const entries: FileChangeEntry[] = [
      entry({ eventId: 'b', seq: 1, source: 'baseline', content: 'line1\nline2\nline3\n' }),
      entry({ eventId: 'e', seq: 2, source: 'manual', content: 'line1\nCHANGED\nline3\n' }),
    ];
    const { container } = render(
      <FileDiffHistory entries={entries} path="train.py" defaultOpen />,
    );

    // Baseline is not rendered as its own row; only the one real edit shows.
    expect(screen.getByText('manual')).toBeInTheDocument();
    expect(screen.queryByText('baseline')).not.toBeInTheDocument();

    // The diff is computed against the baseline: only line2 changed, line1/line3
    // remain context (not "+" additions of the whole file).
    const text = container.textContent ?? '';
    expect(text).toContain('-line2');
    expect(text).toContain('+CHANGED');
    expect(text).not.toContain('+line1');
  });

  it('renders nothing when only a baseline entry exists', () => {
    const entries: FileChangeEntry[] = [
      entry({ eventId: 'b', seq: 1, source: 'baseline', content: 'x = 1\n' }),
    ];
    const { container } = render(
      <FileDiffHistory entries={entries} path="data.py" defaultOpen />,
    );
    expect(container.firstChild).toBeNull();
  });
});

describe('FileDiffHistory D-15: computed-but-empty vs no-diff-available', () => {
  it('идентичное содержимое (diff вычислен, но пуст) → "(no changes)", не "(no diff attached)"', () => {
    const entries: FileChangeEntry[] = [
      entry({ eventId: 'b', seq: 1, source: 'baseline', content: 'same\nlines\n' }),
      // edit with identical content → patch without +/- lines
      entry({ eventId: 'e', seq: 2, source: 'manual', content: 'same\nlines\n' }),
    ];
    render(<FileDiffHistory entries={entries} path="train.py" defaultOpen />);
    expect(screen.getByText('(no changes)')).toBeInTheDocument();
    expect(screen.queryByText('(no diff attached)')).not.toBeInTheDocument();
  });

  it('нет ни diff, ни content (diff недоступен) → "(no diff attached)"', () => {
    const entries: FileChangeEntry[] = [
      entry({ eventId: 'e', seq: 1, source: 'agent', diff: null, content: undefined }),
    ];
    render(<FileDiffHistory entries={entries} path="train.py" defaultOpen />);
    expect(screen.getByText('(no diff attached)')).toBeInTheDocument();
    expect(screen.queryByText('(no changes)')).not.toBeInTheDocument();
  });
});

describe('FileDiffHistory sandbox reset boundary (A.4)', () => {
  it('a second baseline draws a reset divider and restarts the diff chain', () => {
    const entries: FileChangeEntry[] = [
      entry({ eventId: 'b1', seq: 1, source: 'baseline', content: 'scaffold\n' }),
      entry({ eventId: 'e1', seq: 2, source: 'manual', content: 'edit-one\n' }),
      // sandbox_reset → backend re-emits baseline (clean scaffold again).
      entry({ eventId: 'b2', seq: 3, source: 'baseline', content: 'scaffold\n' }),
      entry({ eventId: 'e2', seq: 4, source: 'manual', content: 'edit-two\n' }),
    ];
    const { container } = render(
      <FileDiffHistory entries={entries} path="train.py" defaultOpen />,
    );

    // Reset boundary is shown.
    expect(screen.getByTestId('history-reset-divider')).toBeInTheDocument();

    // Toggle counts real edits only (2), not the divider.
    expect(screen.getByText('Change history (2)')).toBeInTheDocument();

    const text = container.textContent ?? '';
    // edit-two diffs against the reset baseline (scaffold), not against edit-one.
    expect(text).toContain('+edit-two');
    expect(text).not.toContain('-edit-one');
  });
});
