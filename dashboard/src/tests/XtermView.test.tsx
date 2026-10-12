import { render, cleanup } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const writeSpy = vi.fn();
const disposeSpy = vi.fn();
const openSpy = vi.fn();
const loadAddonSpy = vi.fn();
const fitSpy = vi.fn();
const terminalCtorSpy = vi.fn();
const resizeSpy = vi.fn();
const resetSpy = vi.fn();

vi.mock('@xterm/xterm', () => ({
  Terminal: vi.fn().mockImplementation(function (opts: { cols?: number; rows?: number }) {
    terminalCtorSpy(opts);
    return {
      write: writeSpy,
      dispose: disposeSpy,
      open: openSpy,
      loadAddon: loadAddonSpy,
      resize: resizeSpy,
      reset: resetSpy,
      rows: opts?.rows ?? 30,
      cols: opts?.cols ?? 120,
    };
  }),
}));

vi.mock('@xterm/addon-fit', () => ({
  FitAddon: vi.fn().mockImplementation(function () { return { fit: fitSpy }; }),
}));

// xterm.css — side-effect import; Vite returns an empty module, no explicit mock needed.

import XtermView from '../components/XtermView';

beforeEach(() => {
  writeSpy.mockClear();
  disposeSpy.mockClear();
  openSpy.mockClear();
  loadAddonSpy.mockClear();
  fitSpy.mockClear();
  terminalCtorSpy.mockClear();
  resizeSpy.mockClear();
  resetSpy.mockClear();
});

afterEach(() => {
  cleanup();
});

describe('XtermView', () => {
  it('writes all chunks on mount', () => {
    const chunks = [
      { stream: 'stdout' as const, data: 'hello\n', seq: 1 },
      { stream: 'stdout' as const, data: 'world\n', seq: 2 },
    ];
    render(<XtermView chunks={chunks} cols={80} rows={24} />);
    expect(writeSpy).toHaveBeenCalledTimes(2);
    expect(writeSpy.mock.calls[0]![0]).toBe('hello\n');
    expect(writeSpy.mock.calls[1]![0]).toBe('world\n');
  });

  // rows serves as the initial terminal size; actual height is adjusted after
  // writing chunks based on output content.
  it('passes cols/rows to Terminal constructor as initial values', () => {
    render(<XtermView chunks={[]} cols={100} rows={50} />);
    expect(terminalCtorSpy).toHaveBeenCalledTimes(1);
    const opts = terminalCtorSpy.mock.calls[0]![0] as { cols: number; rows: number };
    expect(opts.cols).toBe(100);
    expect(opts.rows).toBe(50);
  });

  it('uses defaults when cols/rows omitted', () => {
    render(<XtermView chunks={[]} />);
    const opts = terminalCtorSpy.mock.calls[0]![0] as { cols: number; rows: number };
    expect(opts.cols).toBe(120);
    expect(opts.rows).toBe(30);
  });

  it('writes only the delta when chunks array grows', () => {
    const c1 = [{ stream: 'stdout' as const, data: 'a', seq: 1 }];
    const { rerender } = render(<XtermView chunks={c1} cols={80} rows={24} />);
    expect(writeSpy).toHaveBeenCalledTimes(1);
    const c2 = [
      ...c1,
      { stream: 'stdout' as const, data: 'b', seq: 2 },
      { stream: 'stdout' as const, data: 'c', seq: 3 },
    ];
    rerender(<XtermView chunks={c2} cols={80} rows={24} />);
    expect(writeSpy).toHaveBeenCalledTimes(3);
    expect(writeSpy.mock.calls[1]![0]).toBe('b');
    expect(writeSpy.mock.calls[2]![0]).toBe('c');
  });

  it('shrinking the chunk set (replay rewind) → reset + rewrite', () => {
    const c2 = [
      { stream: 'stdout' as const, data: 'a\n', seq: 1 },
      { stream: 'stdout' as const, data: 'b\n', seq: 2 },
    ];
    const { rerender } = render(<XtermView chunks={c2} cols={80} rows={24} />);
    expect(writeSpy).toHaveBeenCalledTimes(2);
    expect(resetSpy).not.toHaveBeenCalled();

    writeSpy.mockClear();
    // The cursor moved back — the command has fewer chunks left.
    const c1 = [{ stream: 'stdout' as const, data: 'a\n', seq: 1 }];
    rerender(<XtermView chunks={c1} cols={80} rows={24} />);

    expect(resetSpy).toHaveBeenCalledTimes(1);
    // After reset only the current (single) chunk is rewritten.
    expect(writeSpy).toHaveBeenCalledTimes(1);
    expect(writeSpy.mock.calls[0]![0]).toBe('a\n');
  });

  it('same length → neither reset nor extra write', () => {
    const chunks = [{ stream: 'stdout' as const, data: 'a\n', seq: 1 }];
    const { rerender } = render(<XtermView chunks={chunks} cols={80} rows={24} />);
    writeSpy.mockClear();
    resetSpy.mockClear();
    // New array of the same length (same command, same cursor).
    rerender(<XtermView chunks={[{ stream: 'stdout' as const, data: 'a\n', seq: 1 }]} cols={80} rows={24} />);
    expect(resetSpy).not.toHaveBeenCalled();
    expect(writeSpy).not.toHaveBeenCalled();
  });

  it('disposes Terminal on unmount', () => {
    const { unmount } = render(<XtermView chunks={[]} cols={80} rows={24} />);
    expect(disposeSpy).not.toHaveBeenCalled();
    unmount();
    expect(disposeSpy).toHaveBeenCalledTimes(1);
  });

  it('loads FitAddon and attempts to fit', () => {
    render(<XtermView chunks={[]} cols={80} rows={24} />);
    expect(loadAddonSpy).toHaveBeenCalledTimes(1);
    expect(fitSpy).toHaveBeenCalledTimes(1);
  });

  it('resizes to actual line count after writing chunks', () => {
    // 5 output lines (5 \n) → cursor after the last \n = 6 visible rows.
    const chunks = Array.from({ length: 5 }, (_, i) => ({
      stream: 'stdout' as const,
      data: `line${i + 1}\n`,
      seq: i + 1,
    }));
    render(<XtermView chunks={chunks} cols={80} rows={30} />);
    expect(resizeSpy).toHaveBeenCalledWith(80, 6);
  });

  it('resizes to 1 row for empty output', () => {
    render(<XtermView chunks={[]} cols={80} rows={30} />);
    // no content → targetRows = 1, terminal.rows = 30 → resize called
    expect(resizeSpy).toHaveBeenCalledWith(80, 1);
  });

  it('caps resize at MAX_VISIBLE_ROWS (24) for long output', () => {
    // 30 lines → capped at 24
    const chunks = Array.from({ length: 30 }, (_, i) => ({
      stream: 'stdout' as const,
      data: `line${i + 1}\n`,
      seq: i + 1,
    }));
    render(<XtermView chunks={chunks} cols={80} rows={30} />);
    expect(resizeSpy).toHaveBeenCalledWith(80, 24);
  });

  it('resizes on delta write when chunk count grows', () => {
    const c1 = [{ stream: 'stdout' as const, data: 'first\n', seq: 1 }];
    const { rerender } = render(<XtermView chunks={c1} cols={80} rows={30} />);
    resizeSpy.mockClear();

    const c2 = [
      ...c1,
      { stream: 'stdout' as const, data: 'second\n', seq: 2 },
      { stream: 'stdout' as const, data: 'third\n', seq: 3 },
    ];
    rerender(<XtermView chunks={c2} cols={80} rows={30} />);
    // 3 \n → cursor visible row = 4 (content + empty line under the cursor).
    expect(resizeSpy).toHaveBeenCalledWith(80, 4);
  });
});
