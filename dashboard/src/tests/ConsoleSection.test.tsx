import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { CommandLog } from '../components/consoleTabUtils';
import type { EventEnvelope } from '../api/types';

vi.mock('../components/XtermView', () => ({
  default: ({ chunks }: { chunks: { data: string }[] }) => (
    <div data-testid="xterm-stub">{chunks.map((c) => c.data).join('')}</div>
  ),
}));

import { ConsoleSection } from '../components/ConsoleSection';

// jsdom doesn't implement real scroll layout; make scrollTop writable globally.
beforeEach(() => {
  Object.defineProperty(HTMLElement.prototype, 'scrollTop', {
    writable: true,
    configurable: true,
    value: 0,
  });
});

function setScroll(el: HTMLElement, scrollHeight: number, scrollTop: number, clientHeight: number) {
  Object.defineProperty(el, 'scrollHeight', { configurable: true, value: scrollHeight });
  Object.defineProperty(el, 'scrollTop', { configurable: true, writable: true, value: scrollTop });
  Object.defineProperty(el, 'clientHeight', { configurable: true, value: clientHeight });
}

type CmdRunEvent = Extract<EventEnvelope, { type: 'command_run' }>;
type CmdFinEvent = Extract<EventEnvelope, { type: 'command_finished' }>;

function makeLog(id: string, chunks: string[] = []): CommandLog {
  return {
    commandId: id,
    runEvent: {
      id: `run-${id}`,
      seq: 1,
      session_code: 'TEST-0001',
      timestamp: '2026-01-01T00:00:00Z',
      type: 'command_run',
      payload: { command_id: id, command: 'echo test', cwd: '.' },
    } as CmdRunEvent,
    chunks: chunks.map((data, i) => ({ stream: 'stdout' as const, data, seq: i + 2 })),
    finished: null as CmdFinEvent | null,
  };
}

const noop = () => {};

async function renderSection(props: {
  commandLogs?: CommandLog[];
  open?: boolean;
}) {
  const result = render(
    <ConsoleSection
      commandLogs={props.commandLogs ?? []}
      open={props.open ?? true}
      onToggle={noop}
      testIdPrefix="test"
    />,
  );
  await act(async () => {
    await Promise.resolve();
  });
  return result;
}

describe('ConsoleSection — autoscroll', () => {
  it('D-9: переоткрытие после прокрутки вверх СОХРАНЯЕТ позицию (чанк не дёргает вниз)', async () => {
    const logs1 = [makeLog('c1', ['line1\n'])];
    const { rerender } = await renderSection({ open: true, commandLogs: logs1 });

    const body = screen.getByTestId('test-console-body');
    setScroll(body, 500, 0, 200); // at top
    fireEvent.scroll(body); // atBottomRef → false

    // Close the console
    await act(async () => {
      rerender(<ConsoleSection commandLogs={logs1} open={false} onToggle={noop} testIdPrefix="test" />);
      await Promise.resolve();
    });

    // Reopen — the open effect does NOT reset atBottomRef to true
    await act(async () => {
      rerender(<ConsoleSection commandLogs={logs1} open={true} onToggle={noop} testIdPrefix="test" />);
      await Promise.resolve();
    });

    // Set realistic metrics on the new DOM element
    const reopenedBody = screen.getByTestId('test-console-body');
    setScroll(reopenedBody, 600, 0, 200);

    // New chunk — the user was at the top before closing, autoscroll must not jump down.
    const logs2 = [makeLog('c1', ['line1\n', 'line2\n'])];
    await act(async () => {
      rerender(<ConsoleSection commandLogs={logs2} open={true} onToggle={noop} testIdPrefix="test" />);
      await Promise.resolve();
    });

    expect(reopenedBody.scrollTop).toBe(0);
  });

  it('в нижней позиции: новый chunk довозит вниз', async () => {
    const logs1 = [makeLog('c1', ['line1\n'])];
    const { rerender } = await renderSection({ open: true, commandLogs: logs1 });

    const body = screen.getByTestId('test-console-body');
    setScroll(body, 500, 300, 200); // at bottom (500 - 300 - 200 = 0 < 60)
    fireEvent.scroll(body);

    const logs2 = [makeLog('c1', ['line1\n', 'line2\n'])];
    await act(async () => {
      rerender(
        <ConsoleSection
          commandLogs={logs2}
          open={true}
          onToggle={noop}
          testIdPrefix="test"
        />,
      );
      await Promise.resolve();
    });

    // Was at bottom → after the new chunk scrollTop = scrollHeight
    expect(body.scrollTop).toBe(500);
  });

  it('прокрутил вверх: новый chunk не дёргает', async () => {
    const logs1 = [makeLog('c1', ['line1\n'])];
    const { rerender } = await renderSection({ open: true, commandLogs: logs1 });

    const body = screen.getByTestId('test-console-body');
    setScroll(body, 500, 0, 200); // at top (500 - 0 - 200 = 300 ≥ 60)
    fireEvent.scroll(body);

    const logs2 = [makeLog('c1', ['line1\n', 'line2\n'])];
    await act(async () => {
      rerender(
        <ConsoleSection
          commandLogs={logs2}
          open={true}
          onToggle={noop}
          testIdPrefix="test"
        />,
      );
      await Promise.resolve();
    });

    // Was at top → scrollTop must NOT be overwritten to scrollHeight
    expect(body.scrollTop).toBe(0);
  });

  it('D-9: переоткрытие из нижней позиции → autoscroll сохранён (чанк довозит вниз)', async () => {
    const logs = [makeLog('c1', ['line1\n'])];
    const { rerender } = await renderSection({ open: true, commandLogs: logs });

    const body = screen.getByTestId('test-console-body');
    setScroll(body, 500, 300, 200); // at bottom → atBottomRef = true
    fireEvent.scroll(body);

    // Close the console
    await act(async () => {
      rerender(
        <ConsoleSection commandLogs={logs} open={false} onToggle={noop} testIdPrefix="test" />,
      );
      await Promise.resolve();
    });

    // Reopen: atBottomRef stays true → the next chunk scrolls to bottom
    await act(async () => {
      rerender(
        <ConsoleSection commandLogs={logs} open={true} onToggle={noop} testIdPrefix="test" />,
      );
      await Promise.resolve();
    });

    const reopenedBody = screen.getByTestId('test-console-body');
    setScroll(reopenedBody, 600, 0, 200);

    const logs2 = [makeLog('c1', ['line1\n', 'line2\n'])];
    await act(async () => {
      rerender(<ConsoleSection commandLogs={logs2} open={true} onToggle={noop} testIdPrefix="test" />);
      await Promise.resolve();
    });

    expect(reopenedBody.scrollTop).toBe(600);
  });

  it('скролл вниз возобновляет autoscroll', async () => {
    const logs1 = [makeLog('c1', ['line1\n'])];
    const { rerender } = await renderSection({ open: true, commandLogs: logs1 });

    const body = screen.getByTestId('test-console-body');

    // First scroll up
    setScroll(body, 500, 0, 200);
    fireEvent.scroll(body); // atBottomRef = false

    // Then scroll back to the bottom
    setScroll(body, 500, 300, 200); // at bottom
    fireEvent.scroll(body); // atBottomRef = true

    const logs2 = [makeLog('c1', ['line1\n', 'line2\n'])];
    await act(async () => {
      rerender(
        <ConsoleSection commandLogs={logs2} open={true} onToggle={noop} testIdPrefix="test" />,
      );
      await Promise.resolve();
    });

    // Back at the bottom → autoscroll works again
    expect(body.scrollTop).toBe(500);
  });
});

describe('ConsoleSection — D-14 длительность/код команды', () => {
  function withFinished(log: CommandLog, exitCode: number, durationMs: number): CommandLog {
    return {
      ...log,
      finished: {
        id: `fin-${log.commandId}`,
        seq: 99,
        session_code: 'TEST-0001',
        timestamp: '2026-01-01T00:00:01Z',
        type: 'command_finished',
        payload: { command_id: log.commandId, exit_code: exitCode, duration_ms: durationMs },
      } as CmdFinEvent,
    };
  }

  it('рендерит код выхода и длительность для завершённой команды', async () => {
    const log = withFinished(makeLog('c1', ['out\n']), 0, 1234);
    await renderSection({ open: true, commandLogs: [log] });
    const meta = screen.getByTestId('console-command-meta-c1');
    expect(meta.textContent).toContain('exit 0');
    expect(meta.textContent).toContain('1.2s'); // formatDuration(1234)
    expect(meta.getAttribute('data-fail')).toBe('false');
  });

  it('ненулевой код выхода помечается data-fail="true"', async () => {
    const log = withFinished(makeLog('c2', ['err\n']), 1, 500);
    await renderSection({ open: true, commandLogs: [log] });
    const meta = screen.getByTestId('console-command-meta-c2');
    expect(meta.getAttribute('data-fail')).toBe('true');
    expect(meta.textContent).toContain('exit 1');
  });

  it('незавершённая команда (finished=null) → нет meta-блока', async () => {
    await renderSection({ open: true, commandLogs: [makeLog('c3', ['running\n'])] });
    expect(screen.queryByTestId('console-command-meta-c3')).toBeNull();
  });
});
