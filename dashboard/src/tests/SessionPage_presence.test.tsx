/**
 * SessionPage presence tests:
 * 1. presence_updated WS frame renders the presence panel with attendees.
 * 2. presence_alert WS frame shows an alert badge; clicking dismiss hides it.
 */

import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { SessionPage } from '../pages/SessionPage';
import type { SessionConfig } from '../api/types';

// ---- Mock WebSocket --------------------------------------------------------

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  readyState = 0;
  readonly url: string;
  onopen: ((e: Event) => void) | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;
  onerror: ((e: Event) => void) | null = null;
  onclose: ((e: CloseEvent) => void) | null = null;

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }
  close() {
    this.readyState = 3;
  }
  simulateMessage(data: object) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) }));
  }
}

// ---- Fixtures --------------------------------------------------------------

function makeConfig(overrides: Partial<SessionConfig> = {}): SessionConfig {
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

function renderSessionPage(code = 'AAAA-BBBB') {
  const router = createMemoryRouter(
    [{ path: '/session/:code', element: <SessionPage /> }],
    { initialEntries: [`/session/${code}`] },
  );
  return render(<RouterProvider router={router} />);
}

// ---- Helper ----------------------------------------------------------------

/** Connect the interviewer WS and return the socket instance. */
async function connectWs(): Promise<MockWebSocket> {
  await waitFor(() => expect(MockWebSocket.instances).toHaveLength(1));
  const ws = MockWebSocket.instances[0]!;
  act(() => {
    ws.readyState = 1;
    ws.simulateMessage({ kind: 'hello', protocol_version: 6, session_status: 'active', last_seq: 0 });
  });
  return ws;
}

// ---- Tests -----------------------------------------------------------------

describe('SessionPage presence', () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    vi.stubGlobal('WebSocket', MockWebSocket);
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string) => {
        if (typeof url === 'string' && url.endsWith('/files')) {
          return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ entries: [] }) });
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve(makeConfig()),
        });
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('presence_updated frame renders presence panel with attendees', async () => {
    renderSessionPage();
    await screen.findByTestId('session-code-copy');
    const ws = await connectWs();

    // Before presence_updated, the panel is not shown.
    expect(screen.queryByTestId('presence-panel')).not.toBeInTheDocument();

    act(() => {
      ws.simulateMessage({
        kind: 'presence_updated',
        attendees: [
          { name: 'Alice', is_active: true, joined_at: '2026-05-21T10:00:00Z', last_seen: '2026-05-21T10:00:00Z' },
          { name: 'Bob', is_active: false, joined_at: '2026-05-21T09:55:00Z', last_seen: '2026-05-21T09:59:00Z' },
        ],
      });
    });

    await waitFor(() =>
      expect(screen.getByTestId('presence-panel')).toBeInTheDocument(),
    );
    expect(screen.getByText('Alice')).toBeInTheDocument();
    expect(screen.getByText('Bob')).toBeInTheDocument();
  });

  it('presence_alert shows alert badge; dismiss hides it', async () => {
    renderSessionPage();
    await screen.findByTestId('session-code-copy');
    const ws = await connectWs();

    expect(screen.queryByTestId('presence-alert-badge')).not.toBeInTheDocument();

    act(() => {
      ws.simulateMessage({ kind: 'presence_alert', reason: 'duplicate_connection' });
    });

    await waitFor(() =>
      expect(screen.getByTestId('presence-alert-badge')).toBeInTheDocument(),
    );

    // Clicking dismiss closes the alert.
    fireEvent.click(screen.getByRole('button', { name: /dismiss/i }));

    await waitFor(() =>
      expect(screen.queryByTestId('presence-alert-badge')).not.toBeInTheDocument(),
    );
  });
});

// Presence role badge for interviewers
describe('SessionPage presence role', () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    vi.stubGlobal('WebSocket', MockWebSocket);
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string) => {
        if (typeof url === 'string' && url.endsWith('/files')) {
          return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ entries: [] }) });
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve(makeConfig()),
        });
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('shows role badge for interviewer attendee', async () => {
    renderSessionPage();
    await screen.findByTestId('session-code-copy');
    const ws = await connectWs();

    act(() => {
      ws.simulateMessage({
        kind: 'presence_updated',
        attendees: [
          {
            name: 'john@example.com',
            role: 'interviewer',
            is_active: true,
            first_seen: '2026-05-15T10:00:00Z',
            last_seen: '2026-05-15T10:00:00Z',
          },
        ],
      });
    });

    await screen.findByTestId('presence-panel');
    const roleTag = screen.getByTestId('presence-role-john@example.com');
    expect(roleTag).toBeInTheDocument();
    expect(roleTag.textContent).toMatch(/interviewer/i);
  });

  it('does not show role badge for candidate attendee', async () => {
    renderSessionPage();
    await screen.findByTestId('session-code-copy');
    const ws = await connectWs();

    act(() => {
      ws.simulateMessage({
        kind: 'presence_updated',
        attendees: [
          {
            name: 'Alice',
            role: 'candidate',
            is_active: true,
            first_seen: '2026-05-15T10:00:00Z',
            last_seen: '2026-05-15T10:00:00Z',
          },
        ],
      });
    });

    await screen.findByTestId('presence-panel');
    expect(screen.queryByTestId('presence-role-Alice')).not.toBeInTheDocument();
  });

  it('shows both candidate and interviewer; only interviewer gets role badge', async () => {
    renderSessionPage();
    await screen.findByTestId('session-code-copy');
    const ws = await connectWs();

    act(() => {
      ws.simulateMessage({
        kind: 'presence_updated',
        attendees: [
          {
            name: 'Alice',
            role: 'candidate',
            is_active: true,
            first_seen: '2026-05-15T10:00:00Z',
            last_seen: '2026-05-15T10:00:00Z',
          },
          {
            name: 'john@example.com',
            role: 'interviewer',
            is_active: true,
            first_seen: '2026-05-15T10:00:00Z',
            last_seen: '2026-05-15T10:00:00Z',
          },
        ],
      });
    });

    const panel = await screen.findByTestId('presence-panel');
    expect(panel.textContent).toContain('Alice');
    expect(panel.textContent).toContain('john@example.com');
    expect(screen.getByTestId('presence-role-john@example.com')).toBeInTheDocument();
    expect(screen.queryByTestId('presence-role-Alice')).not.toBeInTheDocument();
  });
});
