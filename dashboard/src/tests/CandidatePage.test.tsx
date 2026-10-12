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
    protocol_version: 6,
    ws_token: '',
    task_id: 'task-01-training-loop',
    task_dir_name: 'training-loop',
    ai_config: { primary_model: 'model', fallback_models: [] },
    bundle_sha256: null,
    created_at: '2026-05-21T10:00:00Z',
    expires_at: '2026-05-21T18:00:00Z',
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

/**
 * Renders the page and passes the presence gate so the main UI is shown.
 * Returns the MockWebSocket instance that was created during the join flow.
 */
async function renderAndJoin(token = 'valid-token'): Promise<MockWebSocket> {
  renderCandidatePage(token);

  // Wait for join screen (config loaded → presence gate)
  await waitFor(() => screen.getByTestId('presence-name-input'));

  fireEvent.change(screen.getByTestId('presence-name-input'), { target: { value: 'Alice' } });
  act(() => {
    fireEvent.click(screen.getByTestId('presence-join-button'));
  });

  // WS is created after join click
  await waitFor(() => MockWebSocket.instances.length > 0);

  const ws = MockWebSocket.instances[0]!;
  act(() => {
    ws.readyState = 1;
    ws.simulateMessage({ kind: 'hello', protocol_version: 6, session_status: 'active', last_seq: 0 });
    ws.simulateMessage({ kind: 'session_join_ok' });
    // Simulate session_start so sessionStarted=true and input area is shown.
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

  // Wait for the tree request triggered by sessionStarted, then resolve it.
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

// ---- Tests -----------------------------------------------------------------

describe('CandidatePage', () => {
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

  it('рендерит input-area после загрузки конфига', async () => {
    await renderAndJoin();
    expect(screen.getByTestId('candidate-end-button')).toBeInTheDocument();
  });

  it('отправляет user_message при клике Send', async () => {
    const ws = await renderAndJoin();

    const input = screen.getByTestId('candidate-input');
    fireEvent.change(input, { target: { value: 'hello AI' } });
    fireEvent.click(screen.getByTestId('candidate-send'));

    // ws.sent may contain session_join + fs_request{op:'tree'} from gate;
    // assert the user_message frame is present among sent frames.
    const frames = ws.sent.map((s) => JSON.parse(s) as Record<string, unknown>);
    const userMsg = frames.find((f) => f.kind === 'user_message');
    expect(userMsg).toMatchObject({ kind: 'user_message', text: 'hello AI' });
  });

  it('surfaces a BUSY rejection and restores the unsent text (no silent drop)', async () => {
    const ws = await renderAndJoin();

    const input = screen.getByTestId('candidate-input') as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: 'second message' } });
    fireEvent.click(screen.getByTestId('candidate-send'));
    // Optimistic clear — the message renders only via the echoed user_message event.
    expect(input.value).toBe('');

    // Backend rejects it (an agent turn is still running). Previously onError was a
    // no-op, so the message vanished silently. Now the candidate gets a notice and
    // the text is restored.
    act(() => {
      ws.simulateMessage({
        kind: 'error',
        code: 'BUSY',
        message: 'A turn is already in progress',
      });
    });

    expect(screen.getByTestId('candidate-send-notice')).toBeInTheDocument();
    expect(input.value).toBe('second message');
  });

  it('agent_status working/idle toggles the AI working indicator', async () => {
    const ws = await renderAndJoin();
    // No turn yet → no indicator.
    expect(screen.queryByTestId('typing-indicator')).not.toBeInTheDocument();

    act(() => {
      ws.simulateMessage({ kind: 'agent_status', state: 'working' });
    });
    expect(screen.getByTestId('typing-indicator')).toBeInTheDocument();

    act(() => {
      ws.simulateMessage({ kind: 'agent_status', state: 'idle' });
    });
    expect(screen.queryByTestId('typing-indicator')).not.toBeInTheDocument();
  });

  it('рендерит входящий assistant_message', async () => {
    const ws = await renderAndJoin();

    act(() => {
      ws.simulateMessage({
        kind: 'event',
        event: {
          id: 'e1',
          seq: 2,
          session_code: 'AAAA-BBBB',
          timestamp: '2026-05-21T10:01:00Z',
          type: 'assistant_message',
          payload: { text: 'Hello candidate!' },
        },
      });
    });

    await waitFor(() => {
      expect(screen.getByText('Hello candidate!')).toBeInTheDocument();
    });
  });

  it('кнопка End session вызывает API с candidate_token', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: () => Promise.resolve(makeConfig()),
      })
      .mockResolvedValueOnce({ ok: true, status: 200 });
    vi.stubGlobal('fetch', fetchMock);

    await renderAndJoin();
    fireEvent.click(screen.getByTestId('candidate-end-button'));
    // Confirm via the AlertDialog (was window.confirm).
    fireEvent.click(await screen.findByTestId('candidate-end-confirm-confirm'));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(2);
      const [url] = fetchMock.mock.calls[1] as [string, ...unknown[]];
      expect(url).toContain('/end');
      expect(url).toContain('candidate_token=valid-token');
    });
  });

  it('при bad token показывает ошибку', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 404,
        json: () => Promise.resolve({ detail: { reason: 'not_found' } }),
      }),
    );
    // Error state is shown before the join screen, so no gate needed.
    renderCandidatePage('bad-token');
    await waitFor(() => {
      expect(screen.getByTestId('candidate-error-message')).toBeInTheDocument();
    });
  });

  it('банер connection-lost появляется при WS reconnecting', async () => {
    const ws = await renderAndJoin();

    // Reconnect banner should not be visible while connected.
    expect(screen.queryByTestId('candidate-reconnect-banner')).toBeNull();

    // Non-terminal close code → reconnecting state (synchronous via _scheduleReconnect).
    act(() => {
      ws.simulateClose(1011);
    });

    expect(screen.getByTestId('candidate-reconnect-banner')).toBeInTheDocument();
  });
});
