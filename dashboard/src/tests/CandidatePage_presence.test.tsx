/**
 * CandidatePage presence tests:
 * 1. Join screen shown after config loads (before WS starts).
 * 2. Full join flow: name entry → WS starts → session_join_ok → main UI.
 * 3. session_join_error (name_taken) shows error; retry works.
 * 4. presence_updated frame renders attendees in header.
 */

import { act, render, screen, waitFor } from '@testing-library/react';
import { fireEvent } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CandidatePage } from '../pages/CandidatePage';

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

// ---- Tests -----------------------------------------------------------------

describe('CandidatePage presence', () => {
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

  it('join screen shown after config loads; WS not yet started', async () => {
    renderCandidatePage();

    await waitFor(() =>
      expect(screen.getByTestId('presence-join-screen')).toBeInTheDocument(),
    );
    expect(screen.getByTestId('presence-name-input')).toBeInTheDocument();
    expect(screen.getByTestId('presence-join-button')).toBeInTheDocument();

    // WS must NOT be created before the user clicks join.
    expect(MockWebSocket.instances).toHaveLength(0);
  });

  it('join flow: name → WS starts → session_join_ok → main UI shown', async () => {
    renderCandidatePage();
    await waitFor(() => screen.getByTestId('presence-name-input'));

    // Type a name and click join.
    fireEvent.change(screen.getByTestId('presence-name-input'), { target: { value: 'Bob' } });
    act(() => {
      fireEvent.click(screen.getByTestId('presence-join-button'));
    });

    // WS should be created after join.
    await waitFor(() => expect(MockWebSocket.instances).toHaveLength(1));

    const ws = MockWebSocket.instances[0]!;
    act(() => {
      ws.readyState = 1;
      ws.simulateMessage({ kind: 'hello', protocol_version: 6, session_status: 'active', last_seq: 0 });
    });

    // session_join must be auto-sent on hello.
    await waitFor(() => {
      const frames = ws.sent.map((s) => JSON.parse(s) as Record<string, unknown>);
      return frames.some((f) => f.kind === 'session_join' && f.name === 'Bob');
    });

    // session_join_ok → main UI
    act(() => {
      ws.simulateMessage({ kind: 'session_join_ok' });
    });

    await waitFor(() =>
      expect(screen.getByTestId('candidate-end-button')).toBeInTheDocument(),
    );
    expect(screen.queryByTestId('presence-join-screen')).not.toBeInTheDocument();
  });

  it('session_join_error (name_taken) shows error; retry sends new session_join', async () => {
    renderCandidatePage();
    await waitFor(() => screen.getByTestId('presence-name-input'));

    fireEvent.change(screen.getByTestId('presence-name-input'), { target: { value: 'Alice' } });
    act(() => {
      fireEvent.click(screen.getByTestId('presence-join-button'));
    });

    await waitFor(() => expect(MockWebSocket.instances).toHaveLength(1));
    const ws = MockWebSocket.instances[0]!;

    act(() => {
      ws.readyState = 1;
      ws.simulateMessage({ kind: 'hello', protocol_version: 6, session_status: 'active', last_seq: 0 });
    });
    act(() => {
      ws.simulateMessage({ kind: 'session_join_error', reason: 'name_taken' });
    });

    // Error message should appear.
    await waitFor(() =>
      expect(screen.getByTestId('presence-join-error')).toBeInTheDocument(),
    );

    // Retry with a different name.
    fireEvent.change(screen.getByTestId('presence-name-input'), { target: { value: 'Alice2' } });
    act(() => {
      fireEvent.click(screen.getByTestId('presence-join-button'));
    });

    // A new session_join frame should be sent (without restarting the WS).
    await waitFor(() => {
      const frames = ws.sent.map((s) => JSON.parse(s) as Record<string, unknown>);
      return frames.some((f) => f.kind === 'session_join' && f.name === 'Alice2');
    });
    // WS instance count stays at 1 — no new connection opened.
    expect(MockWebSocket.instances).toHaveLength(1);
  });

  it('session_join_error (duplicate_connection) shows error and disables retry', async () => {
    renderCandidatePage();
    await waitFor(() => screen.getByTestId('presence-name-input'));

    fireEvent.change(screen.getByTestId('presence-name-input'), { target: { value: 'Alice' } });
    act(() => {
      fireEvent.click(screen.getByTestId('presence-join-button'));
    });

    await waitFor(() => expect(MockWebSocket.instances).toHaveLength(1));
    const ws = MockWebSocket.instances[0]!;

    act(() => {
      ws.readyState = 1;
      ws.simulateMessage({ kind: 'hello', protocol_version: 6, session_status: 'active', last_seq: 0 });
    });
    act(() => {
      ws.simulateMessage({ kind: 'session_join_error', reason: 'duplicate_connection' });
    });

    await waitFor(() =>
      expect(screen.getByTestId('presence-join-error')).toBeInTheDocument(),
    );
    expect(screen.getByTestId('presence-join-error').textContent).toMatch(/another tab/i);
  });

  it('presence_updated frame renders attendees in header', async () => {
    renderCandidatePage();
    await waitFor(() => screen.getByTestId('presence-name-input'));

    fireEvent.change(screen.getByTestId('presence-name-input'), { target: { value: 'Carol' } });
    act(() => {
      fireEvent.click(screen.getByTestId('presence-join-button'));
    });

    await waitFor(() => expect(MockWebSocket.instances).toHaveLength(1));
    const ws = MockWebSocket.instances[0]!;

    act(() => {
      ws.readyState = 1;
      ws.simulateMessage({ kind: 'hello', protocol_version: 6, session_status: 'active', last_seq: 0 });
      ws.simulateMessage({ kind: 'session_join_ok' });
    });

    await waitFor(() => screen.getByTestId('candidate-end-button'));

    // Simulate server broadcast with attendees list.
    act(() => {
      ws.simulateMessage({
        kind: 'presence_updated',
        attendees: [
          { name: 'Carol', is_active: true, joined_at: '2026-05-21T10:00:00Z', last_seen: '2026-05-21T10:00:00Z' },
          { name: 'Dave', is_active: false, joined_at: '2026-05-21T09:55:00Z', last_seen: '2026-05-21T09:59:00Z' },
        ],
      });
    });

    await waitFor(() =>
      expect(screen.getByTestId('presence-panel')).toBeInTheDocument(),
    );
    expect(screen.getByText('Carol')).toBeInTheDocument();
    expect(screen.getByText('Dave')).toBeInTheDocument();
  });
});
