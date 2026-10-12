import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { EmptyState } from '../components/EmptyState';

describe('EmptyState', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders title "Waiting for the candidate"', () => {
    render(<EmptyState code="AAAA-BBBB" />);
    expect(
      screen.getByRole('heading', { name: /waiting for the candidate/i }),
    ).toBeInTheDocument();
  });

  it('shows the large session code and the candidate link', () => {
    render(<EmptyState code="AAAA-BBBB" candidateToken="tok-123" />);
    const codeBtn = screen.getByTestId('empty-state-code-copy');
    expect(codeBtn).toHaveTextContent('AAAA-BBBB');
    // The candidate URL is the only entry point — no legacy CLI command button.
    expect(screen.queryByTestId('empty-state-command-copy')).toBeNull();
    const urlBtn = screen.getByTestId('empty-state-url-copy');
    expect(urlBtn).toHaveTextContent('/candidate?token=tok-123');
  });

  it('click on the code → navigator.clipboard.writeText(code) and a "copied" hint', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });

    render(<EmptyState code="AAAA-BBBB" candidateToken="tok-123" />);
    fireEvent.click(screen.getByTestId('empty-state-code-copy'));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('AAAA-BBBB'));

    // hint appears
    await waitFor(() => {
      expect(screen.getAllByText(/copied/i).length).toBeGreaterThan(0);
    });
  });

  it('a clipboard.writeText failure does not bubble out', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('nope'));
    Object.assign(navigator, { clipboard: { writeText } });

    render(<EmptyState code="AAAA-BBBB" candidateToken="tok-123" />);
    fireEvent.click(screen.getByTestId('empty-state-code-copy'));
    // wait a microtask
    await Promise.resolve();
    await Promise.resolve();
    // no uncaught error, hint did not appear
    expect(screen.queryByText(/^copied$/i)).toBeNull();
  });

  describe('candidate URL', () => {
    it('uses window.location.origin to build the URL', () => {
      render(<EmptyState code="AAAA-BBBB" candidateToken="tok-abc" />);
      const urlBtn = screen.getByTestId('empty-state-url-copy');
      // jsdom gives origin http://localhost
      expect(urlBtn.textContent).toContain('http://localhost');
      expect(urlBtn.textContent).toContain('/candidate?token=tok-abc');
    });

    it('clicking the URL button copies the full URL', async () => {
      const writeText = vi.fn().mockResolvedValue(undefined);
      Object.assign(navigator, { clipboard: { writeText } });

      render(<EmptyState code="AAAA-BBBB" candidateToken="tok-xyz" />);
      fireEvent.click(screen.getByTestId('empty-state-url-copy'));
      await waitFor(() =>
        expect(writeText).toHaveBeenCalledWith(
          expect.stringContaining('/candidate?token=tok-xyz'),
        ),
      );
    });

    it('without candidateToken it shows a placeholder', () => {
      render(<EmptyState code="AAAA-BBBB" />);
      const urlBtn = screen.getByTestId('empty-state-url-copy');
      expect(urlBtn).toHaveTextContent('…');
    });
  });
});
