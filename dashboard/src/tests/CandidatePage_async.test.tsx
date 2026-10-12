import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CandidatePage } from '../pages/CandidatePage';

// ---- Mock WebSocket --------------------------------------------------------

class MockWebSocket {
  static instances: MockWebSocket[] = [];

  readyState = 0;
  readonly url: string;
  sent: string[] = [];

  onopen: ((e: Event) => void) | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;
  onerror: ((e: Event) => void) | null = null;
  onclose: ((e: CloseEvent) => void) | null = null;

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }

  send(data: string) {
    this.sent.push(data);
  }

  close() {
    this.readyState = 3;
  }

  simulateMessage(data: object) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) }));
  }

  simulateClose(code = 1000, reason = '') {
    this.readyState = 3;
    this.onclose?.(new CloseEvent('close', { code, reason }));
  }
}

// ---- Fixtures ---------------------------------------------------------------

function makeConfig(overrides: Record<string, unknown> = {}) {
  return {
    session_code: 'AAAA-BBBB',
    session_status: 'active',
    protocol_version: 7,
    ws_token: '',
    task_id: 'task-01-training-loop',
    task_dir_name: 'training-loop',
    ai_config: { primary_model: 'model', fallback_models: [] },
    bundle_sha256: null,
    created_at: '2026-05-21T10:00:00Z',
    expires_at: '2026-05-21T18:00:00Z',
    mode: 'async',
    time_limit_seconds: 1800,
    ...overrides,
  };
}

function renderCandidatePage(token = 'valid-token') {
  const router = createMemoryRouter(
    [{ path: '/candidate', element: <CandidatePage /> }],
    { initialEntries: [`/candidate?token=${token}`] },
  );
  return render(<RouterProvider router={router} />);
}

async function renderAndJoin(): Promise<MockWebSocket> {
  renderCandidatePage();

  await waitFor(() => screen.getByTestId('presence-name-input'));
  fireEvent.change(screen.getByTestId('presence-name-input'), { target: { value: 'Alice' } });
  act(() => {
    fireEvent.click(screen.getByTestId('presence-join-button'));
  });

  await waitFor(() => MockWebSocket.instances.length > 0);
  const ws = MockWebSocket.instances[0]!;
  act(() => {
    ws.readyState = 1;
    ws.simulateMessage({ kind: 'hello', protocol_version: 7, session_status: 'active', last_seq: 0 });
    ws.simulateMessage({ kind: 'session_join_ok' });
    ws.simulateMessage({
      kind: 'event',
      event: {
        id: 'e-session-start',
        seq: 1,
        session_code: 'AAAA-BBBB',
        timestamp: '2026-05-21T10:00:00Z',
        type: 'session_start',
        payload: {},
      },
    });
  });

  await waitFor(() =>
    ws.sent.some((s) => (JSON.parse(s) as Record<string, unknown>).op === 'tree'),
  );
  const treeReq0 = ws.sent
    .map((s) => JSON.parse(s) as Record<string, unknown>)
    .find((f) => f.kind === 'fs_request' && f.op === 'tree')!;
  act(() => {
    ws.simulateMessage({ kind: 'fs_response', id: treeReq0.id, entries: [] });
  });

  await waitFor(() => screen.getByTestId('candidate-input-area'));
  return ws;
}

function sendEnd(ws: MockWebSocket, reason: string) {
  act(() => {
    ws.simulateMessage({
      kind: 'event',
      event: {
        id: 'e-end',
        seq: 99,
        session_code: 'AAAA-BBBB',
        timestamp: '2026-05-21T10:30:00Z',
        type: 'session_end',
        payload: { reason },
      },
    });
  });
}

// ---- Tests -----------------------------------------------------------------

describe('CandidatePage async', () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    vi.stubGlobal('WebSocket', MockWebSocket);
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: () => Promise.resolve(makeConfig()),
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('timer pill renders and updates on timer_tick', async () => {
    const ws = await renderAndJoin();

    // Seeded from time_limit_seconds (1800 = 30:00) before the first tick.
    const pill = screen.getByTestId('candidate-timer');
    expect(pill.textContent).toBe('30:00');

    act(() => ws.simulateMessage({ kind: 'timer_tick', remaining_seconds: 125 }));
    expect(screen.getByTestId('candidate-timer').textContent).toBe('2:05');

    act(() => ws.simulateMessage({ kind: 'timer_tick', remaining_seconds: 59 }));
    const low = screen.getByTestId('candidate-timer');
    expect(low.textContent).toBe('0:59');
    expect(low.getAttribute('data-low')).toBe('true');
  });

  it('Submit opens the modal and sends session_submit', async () => {
    const ws = await renderAndJoin();

    fireEvent.click(screen.getByTestId('candidate-submit-button'));
    await waitFor(() => screen.getByTestId('submit-confirm-modal'));

    fireEvent.click(screen.getByTestId('submit-confirm'));

    const frames = ws.sent.map((s) => JSON.parse(s) as Record<string, unknown>);
    expect(frames.some((f) => f.kind === 'session_submit')).toBe(true);
  });

  it('double-click on confirm does not send a second session_submit', async () => {
    const ws = await renderAndJoin();

    fireEvent.click(screen.getByTestId('candidate-submit-button'));
    await waitFor(() => screen.getByTestId('submit-confirm-modal'));

    const confirm = screen.getByTestId('submit-confirm') as HTMLButtonElement;
    // Rapid double-click: ref-guard + the disabled button both keep it to one send.
    fireEvent.click(confirm);
    fireEvent.click(confirm);

    const submits = ws.sent
      .map((s) => JSON.parse(s) as Record<string, unknown>)
      .filter((f) => f.kind === 'session_submit');
    expect(submits.length).toBe(1);
    // Confirm is locked after the first click.
    expect((screen.getByTestId('submit-confirm') as HTMLButtonElement).disabled).toBe(true);
  });

  it('shows the end screen on session_end(reason: submitted)', async () => {
    const ws = await renderAndJoin();
    sendEnd(ws, 'submitted');

    await waitFor(() => screen.getByTestId('candidate-end-screen'));
    expect(screen.getByTestId('candidate-end-screen').getAttribute('data-reason')).toBe('submitted');
    // IDE is gone — no input area on the end-screen.
    expect(screen.queryByTestId('candidate-input-area')).toBeNull();
  });

  it('shows the end screen on session_end(reason: timeout)', async () => {
    const ws = await renderAndJoin();
    sendEnd(ws, 'timeout');

    await waitFor(() => screen.getByTestId('candidate-end-screen'));
    expect(screen.getByTestId('candidate-end-screen').getAttribute('data-reason')).toBe('timeout');
  });

  it('countdown pauses on WS disconnect and does not tick to 0', async () => {
    // Fake timers from the start so the 1s interpolation interval is created under
    // them; advanceTimersByTimeAsync flushes the async load/join round-trips too.
    vi.useFakeTimers();
    const flush = () => act(async () => { await vi.advanceTimersByTimeAsync(1); });

    renderCandidatePage();
    await flush(); // config load
    fireEvent.change(screen.getByTestId('presence-name-input'), { target: { value: 'Alice' } });
    act(() => fireEvent.click(screen.getByTestId('presence-join-button')));
    await flush(); // WS opens

    const ws = MockWebSocket.instances[0]!;
    act(() => {
      ws.readyState = 1;
      ws.simulateMessage({ kind: 'hello', protocol_version: 7, session_status: 'active', last_seq: 0 });
      ws.simulateMessage({ kind: 'session_join_ok' });
      ws.simulateMessage({
        kind: 'event',
        event: {
          id: 'e-session-start', seq: 1, session_code: 'AAAA-BBBB',
          timestamp: '2026-05-21T10:00:00Z', type: 'session_start', payload: {},
        },
      });
    });
    await flush();
    const treeReq = ws.sent
      .map((s) => JSON.parse(s) as Record<string, unknown>)
      .find((f) => f.kind === 'fs_request' && f.op === 'tree')!;
    act(() => ws.simulateMessage({ kind: 'fs_response', id: treeReq.id, entries: [] }));
    await flush();

    act(() => ws.simulateMessage({ kind: 'timer_tick', remaining_seconds: 120 }));
    expect(screen.getByTestId('candidate-timer').textContent).toBe('2:00');

    // Connected: the local 1s interpolation decrements between server ticks.
    act(() => vi.advanceTimersByTime(2000));
    expect(screen.getByTestId('candidate-timer').textContent).toBe('1:58');

    // Disconnect → the server clock is authoritative and survives the drop, so the
    // pill must freeze rather than tick to 0 during the outage.
    act(() => ws.simulateClose(1011));
    act(() => vi.advanceTimersByTime(10000));
    expect(screen.getByTestId('candidate-timer').textContent).toBe('1:58');
  });

  it('live session: no timer or Submit button (End only)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: () => Promise.resolve(makeConfig({ mode: 'live', time_limit_seconds: null })),
      }),
    );
    await renderAndJoin();

    expect(screen.queryByTestId('candidate-timer')).toBeNull();
    expect(screen.queryByTestId('candidate-submit-button')).toBeNull();
    expect(screen.getByTestId('candidate-end-button')).toBeInTheDocument();
  });
});
