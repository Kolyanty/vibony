/**
 * CandidatePage bash tests:
 * 1. !cmd input sends inline_bash frame (not user_message).
 * 2. command_run/output/finished events render in console panel.
 * 3. bash_confirm_request frame shows confirm modal.
 * 4. Confirm modal buttons send bash_confirm_response frame.
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
  default: ({ chunks }: { chunks: { data: string }[] }) => (
    <div data-testid="xterm-stub">{chunks.map((c) => c.data).join('')}</div>
  ),
}));

// ---- WebSocket mock --------------------------------------------------------

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  readyState = 0;
  readonly url: string;
  sent: string[] = [];
  onmessage: ((e: MessageEvent) => void) | null = null;
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

describe('CandidatePage bash', () => {
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

  it('!cmd input sends inline_bash frame, not user_message', async () => {
    const ws = await renderAndJoin();

    const input = screen.getByTestId('candidate-input');
    fireEvent.change(input, { target: { value: '!pytest tests/' } });
    fireEvent.keyDown(input, { key: 'Enter', shiftKey: false });

    const frames = ws.sent.map((s) => JSON.parse(s) as Record<string, unknown>);
    const bashFrame = frames.find((f) => f.kind === 'inline_bash');
    expect(bashFrame).toBeDefined();
    expect(bashFrame?.cmd).toBe('pytest tests/');

    const msgFrame = frames.find((f) => f.kind === 'user_message');
    expect(msgFrame).toBeUndefined();
  });

  it('command_run event opens console panel and output appears', async () => {
    const ws = await renderAndJoin();

    // Emit command_run event
    act(() => {
      ws.simulateMessage({
        kind: 'event',
        event: {
          id: 'ev-run',
          seq: 2,
          session_code: 'AAAA-BBBB',
          timestamp: '2026-05-21T10:01:00Z',
          type: 'command_run',
          payload: { command_id: 'cmd1', command: 'pytest tests/', cwd: '.' },
        },
      });
    });

    // Console section should be visible and open automatically
    await waitFor(() => {
      expect(screen.getByTestId('candidate-console-body')).toBeInTheDocument();
    });

    // Emit command_output
    act(() => {
      ws.simulateMessage({
        kind: 'event',
        event: {
          id: 'ev-out',
          seq: 3,
          session_code: 'AAAA-BBBB',
          timestamp: '2026-05-21T10:01:01Z',
          type: 'command_output',
          payload: { command_id: 'cmd1', stream: 'stdout', data: '1 passed\n' },
        },
      });
    });

    await waitFor(() => {
      expect(screen.getByTestId('xterm-stub')).toBeInTheDocument();
    });
  });

  it('bash_confirm_request frame shows confirm modal', async () => {
    const ws = await renderAndJoin();

    // Simulate bash_confirm_request WS frame
    act(() => {
      ws.simulateMessage({
        kind: 'bash_confirm_request',
        cmd_id: 'confirm-1',
        cmd: 'pip install numpy',
        prefix: 'pip install',
        choices: ['allow_once', 'allow_prefix', 'deny'],
      });
    });

    await waitFor(() => {
      expect(screen.getByTestId('bash-confirm-modal')).toBeInTheDocument();
    });
    expect(screen.getByTestId('bash-confirm-allow-once')).toBeInTheDocument();
    expect(screen.getByTestId('bash-confirm-deny')).toBeInTheDocument();
  });

  it('clicking allow_once sends bash_confirm_response and closes modal', async () => {
    const ws = await renderAndJoin();

    act(() => {
      ws.simulateMessage({
        kind: 'bash_confirm_request',
        cmd_id: 'confirm-2',
        cmd: 'pip install numpy',
        prefix: 'pip install',
        choices: ['allow_once', 'allow_prefix', 'deny'],
      });
    });

    await waitFor(() => screen.getByTestId('bash-confirm-modal'));

    fireEvent.click(screen.getByTestId('bash-confirm-allow-once'));

    const frames = ws.sent.map((s) => JSON.parse(s) as Record<string, unknown>);
    const resp = frames.find((f) => f.kind === 'bash_confirm_response');
    expect(resp).toBeDefined();
    expect(resp?.cmd_id).toBe('confirm-2');
    expect(resp?.choice).toBe('allow_once');

    await waitFor(() => {
      expect(screen.queryByTestId('bash-confirm-modal')).not.toBeInTheDocument();
    });
  });
});
