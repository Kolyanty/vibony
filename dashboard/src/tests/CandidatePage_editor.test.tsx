/**
 * CandidatePage editor tests:
 * 1. File tree appears after WS tree request resolves.
 * 2. Clicking a file triggers fs_request{op:'read'} and shows editor.
 * 3. Monaco save emits fs_request{op:'write'}.
 * 4. file_changed event triggers tree refresh (new fs_request{op:'tree'}).
 */

import { act, render, screen, waitFor } from '@testing-library/react';
import { fireEvent } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CandidatePage } from '../pages/CandidatePage';

// ---- Mock MonacoEditor component ---------------------------------------------
// Mock at the component level (not @monaco-editor/react) so we don't pull in
// the real monaco-editor package or its `?worker` imports under jsdom — neither
// of which work outside Vite's worker resolver.

vi.mock('../components/MonacoEditor', () => ({
  default: ({
    path,
    content,
    onSave,
  }: {
    path: string;
    content: string;
    onSave?: (value: string) => void;
  }) => (
    <div data-testid="monaco-stub" data-path={path} data-content={content}>
      {onSave && (
        <button
          type="button"
          data-testid="monaco-save-trigger"
          onClick={() => onSave(content)}
        >
          save
        </button>
      )}
    </div>
  ),
}));

// ---- Mock WebSocket ----------------------------------------------------------

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

// ---- Fixtures ----------------------------------------------------------------

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
 * Pass the presence gate and simulate session_start so the sandbox is
 * considered ready. After this call ws.sent contains
 * [session_join, fs_request{op:'tree'}] at minimum.
 */
async function renderAndJoin(
  token = 'valid-token',
  helloOverrides: Record<string, unknown> = {},
): Promise<MockWebSocket> {
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
    ws.simulateMessage({
      kind: 'hello',
      protocol_version: 6,
      session_status: 'active',
      last_seq: 0,
      ...helloOverrides,
    });
    ws.simulateMessage({ kind: 'session_join_ok' });
    // Simulate session_start so sessionStarted=true → tree request fired.
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

  // Wait for the tree request to be queued.
  await waitFor(() =>
    ws.sent.some(
      (s) => (JSON.parse(s) as Record<string, unknown>).op === 'tree',
    ),
  );

  await waitFor(() => screen.getByTestId('candidate-end-button'));
  return ws;
}

/**
 * Respond to the latest pending fs_request{op:'tree'} with the given paths.
 * After renderAndJoin(), the initial tree request is already in ws.sent.
 */
function respondTree(ws: MockWebSocket, paths: string[]) {
  const treeReq = ws.sent
    .map((s) => JSON.parse(s) as Record<string, unknown>)
    .findLast((f) => f.kind === 'fs_request' && f.op === 'tree');
  if (treeReq) {
    act(() => {
      ws.simulateMessage({
        kind: 'fs_response',
        id: treeReq.id,
        op: 'tree',
        entries: paths.map((p) => ({ path: p, kind: 'file', size: 100 })),
      });
    });
  }
}

// ---- Tests ------------------------------------------------------------------

describe('CandidatePage editor', () => {
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

  it('tree request sent on WS connect; file-tree rendered on response', async () => {
    const ws = await renderAndJoin();

    // Initial tree request is already in ws.sent; respond with paths.
    respondTree(ws, ['train.py', 'data.py']);

    await waitFor(() => {
      expect(screen.getByTestId('tree-file-train.py')).toBeInTheDocument();
    });
  });

  it('clicking a file sends fs_request{op:read} and renders editor', async () => {
    const ws = await renderAndJoin();
    respondTree(ws, ['train.py']);

    await waitFor(() => screen.getByTestId('tree-file-train.py'));

    // Click the file.
    fireEvent.click(screen.getByTestId('tree-file-train.py'));

    // Find and respond to the read request.
    await waitFor(() => {
      const frames = ws.sent.map((s) => JSON.parse(s) as Record<string, unknown>);
      return frames.some((f) => f.kind === 'fs_request' && f.op === 'read' && f.path === 'train.py');
    });
    const readReq = ws.sent
      .map((s) => JSON.parse(s) as Record<string, unknown>)
      .find((f) => f.kind === 'fs_request' && f.op === 'read')!;

    act(() => {
      ws.simulateMessage({
        kind: 'fs_response',
        id: readReq.id,
        op: 'read',
        path: 'train.py',
        content: 'import torch\n',
      });
    });

    await waitFor(() => {
      expect(screen.getByTestId('monaco-stub')).toBeInTheDocument();
      expect(screen.getByTestId('monaco-stub')).toHaveAttribute('data-path', 'train.py');
    });
  });

  it('tree loads only after session_start (not at join time)', async () => {
    // Regression fix: the tree request must NOT fire at join time
    // because the WebFsProxy is only registered after host.start() completes.
    // session_start is emitted right after provisioning, so it is the reliable signal.
    // The presencePhase useEffect now depends on sessionStarted (derived from
    // session_start having been received) in addition to presencePhase and connection.
    renderCandidatePage();

    await waitFor(() => screen.getByTestId('presence-name-input'));
    fireEvent.change(screen.getByTestId('presence-name-input'), { target: { value: 'Alice' } });
    act(() => {
      fireEvent.click(screen.getByTestId('presence-join-button'));
    });

    await waitFor(() => MockWebSocket.instances.length > 0);
    const ws = MockWebSocket.instances[0]!;

    // Join completes but session_start not sent yet.
    act(() => {
      ws.readyState = 1;
      ws.simulateMessage({ kind: 'hello', protocol_version: 6, session_status: 'active', last_seq: 0 });
      ws.simulateMessage({ kind: 'session_join_ok' });
    });
    await waitFor(() => screen.getByTestId('candidate-end-button'));

    // No tree request should have been sent yet (sandbox not ready).
    expect(
      ws.sent.some((s) => (JSON.parse(s) as Record<string, unknown>).op === 'tree'),
    ).toBe(false);

    // Backend finishes provisioning → emits session_start.
    act(() => {
      ws.simulateMessage({
        kind: 'event',
        event: {
          id: 'evt-start',
          seq: 1,
          session_code: 'AAAA-BBBB',
          timestamp: '2026-05-21T10:00:01Z',
          type: 'session_start',
          payload: {},
        },
      });
    });

    // Now the tree request must be sent.
    await waitFor(() =>
      ws.sent.some((s) => (JSON.parse(s) as Record<string, unknown>).op === 'tree'),
    );

    // Respond with actual files.
    const treeReq = ws.sent
      .map((s) => JSON.parse(s) as Record<string, unknown>)
      .findLast((f) => f.kind === 'fs_request' && f.op === 'tree')!;
    act(() => {
      ws.simulateMessage({
        kind: 'fs_response',
        id: treeReq.id,
        op: 'tree',
        entries: [{ path: 'train.py', kind: 'file', size: 800 }],
      });
    });

    await waitFor(() => {
      expect(screen.getByTestId('tree-file-train.py')).toBeInTheDocument();
    });
  });

  it('file_changed event triggers a new tree refresh request', async () => {
    const ws = await renderAndJoin();
    respondTree(ws, ['train.py']);

    const beforeCount = ws.sent.filter(
      (s) => (JSON.parse(s) as Record<string, unknown>).op === 'tree',
    ).length;

    // Emit file_changed event.
    act(() => {
      ws.simulateMessage({
        kind: 'event',
        event: {
          id: 'e2',
          seq: 2,
          session_code: 'AAAA-BBBB',
          timestamp: '2026-05-21T10:01:00Z',
          type: 'file_changed',
          payload: { file_path: 'train.py', source: 'agent' },
        },
      });
    });

    await waitFor(() => {
      const treeCount = ws.sent.filter(
        (s) => (JSON.parse(s) as Record<string, unknown>).op === 'tree',
      ).length;
      expect(treeCount).toBeGreaterThan(beforeCount);
    });
  });
});
