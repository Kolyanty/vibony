/**
 * CandidatePage reconnect tests:
 * 1. WS close → "Reconnecting…" banner appears.
 * 2. sandbox_reset event → reset notice banner appears + file tree is cleared.
 */

import { act, render, screen, waitFor } from '@testing-library/react';
import { fireEvent } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CandidatePage } from '../pages/CandidatePage';

// ---- Monaco mock -----------------------------------------------------------

// Mock MonacoEditor at the component level (not @monaco-editor/react) so we
// don't pull in the real monaco-editor package or its `?worker` imports under
// jsdom — neither of which work outside Vite's worker resolver.
vi.mock('../components/MonacoEditor', () => ({
  default: ({ path }: { path?: string }) => (
    <div data-testid="monaco-stub" data-path={path} />
  ),
}));

// ---- XtermView mock --------------------------------------------------------

vi.mock('../components/XtermView', () => ({
  default: () => <div data-testid="xterm-stub" />,
}));

// ---- WebSocket mock --------------------------------------------------------

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  readyState = 0;
  readonly url: string;
  sent: string[] = [];
  onmessage: ((e: MessageEvent) => void) | null = null;
  onclose: ((e: CloseEvent) => void) | null = null;
  onerror: (() => void) | null = null;

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
  simulateClose(code = 1001, reason = '') {
    this.readyState = 3;
    this.onclose?.(new CloseEvent('close', { code, reason }));
  }
}

// ---- Fixtures --------------------------------------------------------------

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

/** Pass the presence gate and return the connected WS. */
async function renderAndJoin(token = 'valid-token'): Promise<MockWebSocket> {
  renderCandidatePage(token);

  await waitFor(() => screen.getByTestId('presence-name-input'));
  fireEvent.change(screen.getByTestId('presence-name-input'), { target: { value: 'Alice' } });
  act(() => {
    fireEvent.click(screen.getByTestId('presence-join-button'));
  });

  await waitFor(() => MockWebSocket.instances.length > 0);

  const ws = MockWebSocket.instances[0]!;
  act(() => {
    ws.readyState = 1;
    ws.simulateMessage({ kind: 'hello', protocol_version: 6, session_status: 'active', last_seq: 0 });
    ws.simulateMessage({ kind: 'session_join_ok' });
    // Simulate session_start so sessionStarted=true.
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

  await waitFor(() => screen.getByTestId('candidate-end-button'));
  return ws;
}

// ---- Tests -----------------------------------------------------------------

describe('CandidatePage reconnect', () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    vi.stubGlobal('WebSocket', MockWebSocket);
    vi.stubGlobal('crypto', {
      randomUUID: vi.fn().mockImplementation(() => `uuid-${Math.random().toString(36).slice(2)}`),
    });
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
  });

  it('WS close shows reconnecting banner', async () => {
    const ws = await renderAndJoin();

    // Reconnect banner should not be visible while connected.
    expect(screen.queryByTestId('candidate-reconnect-banner')).toBeNull();

    // Non-terminal close code → reconnecting state.
    act(() => {
      ws.simulateClose(1006);
    });

    await waitFor(() =>
      expect(screen.getByTestId('candidate-reconnect-banner')).toBeInTheDocument(),
    );
  });

  it('sandbox_reset without reason shows manual reset banner', async () => {
    const ws = await renderAndJoin();

    expect(screen.queryByTestId('candidate-sandbox-reset-banner')).toBeNull();

    act(() => {
      ws.simulateMessage({
        kind: 'event',
        event: {
          id: 'evt-sr-1',
          seq: 2,
          session_code: 'AAAA-BBBB',
          timestamp: '2026-05-21T10:02:00Z',
          type: 'sandbox_reset',
          payload: {},
        },
      });
    });

    await waitFor(() => {
      const banner = screen.getByTestId('candidate-sandbox-reset-banner');
      expect(banner).toBeInTheDocument();
      expect(banner.getAttribute('data-reason')).toBe('manual');
    });
  });

  it('sandbox_reset with reason=expired shows expired banner', async () => {
    const ws = await renderAndJoin();

    expect(screen.queryByTestId('candidate-sandbox-reset-banner')).toBeNull();

    act(() => {
      ws.simulateMessage({
        kind: 'event',
        event: {
          id: 'evt-sr-2',
          seq: 3,
          session_code: 'AAAA-BBBB',
          timestamp: '2026-05-21T11:30:00Z',
          type: 'sandbox_reset',
          payload: { reason: 'expired' },
        },
      });
    });

    await waitFor(() => {
      const banner = screen.getByTestId('candidate-sandbox-reset-banner');
      expect(banner).toBeInTheDocument();
      expect(banner.getAttribute('data-reason')).toBe('expired');
    });
  });

  // After backend restart the sandbox is re-provisioned from event log.
  it('sandbox_restored shows the restored-from-history banner', async () => {
    const ws = await renderAndJoin();

    expect(screen.queryByTestId('candidate-sandbox-reset-banner')).toBeNull();

    act(() => {
      ws.simulateMessage({
        kind: 'event',
        event: {
          id: 'evt-rstr-1',
          seq: 4,
          session_code: 'AAAA-BBBB',
          timestamp: '2026-05-21T12:00:00Z',
          type: 'sandbox_restored',
          payload: { file_count: 5, deleted_count: 1 },
        },
      });
    });

    await waitFor(() => {
      const banner = screen.getByTestId('candidate-sandbox-reset-banner');
      expect(banner).toBeInTheDocument();
      expect(banner.getAttribute('data-reason')).toBe('restored');
    });
  });
});
