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

  simulateClose(code = 1000, reason = '') {
    this.readyState = 3;
    this.onclose?.(new CloseEvent('close', { code, reason }));
  }
}

// ---- Fixtures --------------------------------------------------------------

function makeConfig(overrides: Partial<SessionConfig> = {}): SessionConfig {
  return {
    session_code: 'AAAA-BBBB',
    session_status: 'pending',
    protocol_version: 1,
    ws_token: '',
    task_id: 'task-01-training-loop',
    task_dir_name: 'training-loop',
    ai_config: { primary_model: 'deepseek-v4-flash', fallback_models: [] },
    bundle_sha256: null,
    created_at: '2026-05-15T10:00:00Z',
    expires_at: '2026-05-15T18:00:00Z',
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

// ---- Tests -----------------------------------------------------------------

describe('SessionPage smoke', () => {
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
  });

  it('shows loading before the REST response', () => {
    vi.stubGlobal('fetch', vi.fn().mockReturnValue(new Promise(() => {})));
    renderSessionPage();
    expect(screen.getByText(/loading session config/i)).toBeInTheDocument();
  });

  it('after REST, shows session_code in the header subtitle', async () => {
    renderSessionPage();
    const codeBtn = await screen.findByTestId('session-code-copy');
    expect(codeBtn).toHaveTextContent('AAAA-BBBB');
  });

  it('after REST, the WS is created automatically', async () => {
    renderSessionPage();
    await screen.findByTestId('session-code-copy');
    await waitFor(() => expect(MockWebSocket.instances).toHaveLength(1));
  });

  it('hello → ConnectionBadge shows "connected", sessionStatus updates', async () => {
    renderSessionPage();
    await screen.findByTestId('session-code-copy');
    await waitFor(() => expect(MockWebSocket.instances).toHaveLength(1));

    act(() => {
      MockWebSocket.instances[0]!.simulateMessage({
        kind: 'hello',
        protocol_version: 1,
        session_status: 'active',
        last_seq: 0,
      });
    });

    expect(screen.getByText('connected')).toBeInTheDocument();
    expect(screen.getByText('active')).toBeInTheDocument();
  });

  it('session_end event → sessionStatus transitions to completed', async () => {
    renderSessionPage();
    await screen.findByTestId('session-code-copy');
    await waitFor(() => expect(MockWebSocket.instances).toHaveLength(1));

    act(() => {
      MockWebSocket.instances[0]!.simulateMessage({
        kind: 'hello', protocol_version: 1, session_status: 'active', last_seq: 0,
      });
      MockWebSocket.instances[0]!.simulateMessage({
        kind: 'event',
        event: {
          id: 'e-end', seq: 1, session_code: 'AAAA-BBBB',
          timestamp: '2026-05-15T10:01:00Z', type: 'session_end', payload: { reason: 'interviewer' },
        },
      });
    });

    expect(screen.getByText('completed')).toBeInTheDocument();
  });

  it('session_end + WS close 4409 → no technical banner shown', async () => {
    renderSessionPage();
    await screen.findByTestId('session-code-copy');
    await waitFor(() => expect(MockWebSocket.instances).toHaveLength(1));

    act(() => {
      MockWebSocket.instances[0]!.simulateMessage({
        kind: 'hello', protocol_version: 1, session_status: 'active', last_seq: 0,
      });
      MockWebSocket.instances[0]!.simulateMessage({
        kind: 'event',
        event: {
          id: 'e-end', seq: 1, session_code: 'AAAA-BBBB',
          timestamp: '2026-05-15T10:01:00Z', type: 'session_end', payload: { reason: 'interviewer' },
        },
      });
      MockWebSocket.instances[0]!.simulateClose(4409, 'Session already ended');
    });

    expect(screen.queryByText(/WS:/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/session already ended/i)).not.toBeInTheDocument();
    expect(screen.getByText('completed')).toBeInTheDocument();
  });

  it('REST 404 → shows the error screen', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: false, status: 404, json: () => Promise.resolve({}) }),
    );
    renderSessionPage();
    await screen.findByRole('heading', { name: 'Session not found' });
  });

  it('fetch uses credentials:include (cookie auth)', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve(makeConfig()),
    });
    vi.stubGlobal('fetch', fetchMock);
    renderSessionPage();
    await screen.findByTestId('session-code-copy');
    const [, options] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(options.credentials).toBe('include');
  });
});

describe('SessionPage header', () => {
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
  });

  it('elapsed time appears in the header: <time dateTime=created_at> + a "running" prefix', async () => {
    renderSessionPage();
    const elapsed = await screen.findByTestId('session-elapsed');
    expect(elapsed).toHaveTextContent(/running/);
    const time = elapsed.querySelector('time');
    expect(time).not.toBeNull();
    expect(time).toHaveAttribute('dateTime', '2026-05-15T10:00:00Z');
  });

  it('clicking session_code calls navigator.clipboard.writeText', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });
    renderSessionPage();
    const btn = await screen.findByTestId('session-code-copy');
    await act(async () => {
      fireEvent.click(btn);
    });
    expect(writeText).toHaveBeenCalledWith('AAAA-BBBB');
  });

  it('End session: confirm + POST /sessions/{code}/end with credentials:include', async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (typeof url === 'string' && url.endsWith('/end')) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve(makeConfig({ session_status: 'completed' })),
        });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(makeConfig()),
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    renderSessionPage();
    fireEvent.click(await screen.findByTestId('session-end-button'));
    // F1: confirm via the AlertDialog (was window.confirm).
    const confirmBtn = await screen.findByTestId('session-end-confirm-confirm');
    await act(async () => {
      fireEvent.click(confirmBtn);
    });

    const endCall = fetchMock.mock.calls.find(([u]) => typeof u === 'string' && u.endsWith('/end'));
    expect(endCall).toBeDefined();
    const [endUrl, endInit] = endCall as [string, RequestInit];
    expect(endUrl).toContain('/sessions/AAAA-BBBB/end');
    expect(endInit.method).toBe('POST');
    expect(endInit.credentials).toBe('include');
  });

  it('cancelling the confirm does not POST', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve(makeConfig()),
    });
    vi.stubGlobal('fetch', fetchMock);

    renderSessionPage();
    fireEvent.click(await screen.findByTestId('session-end-button'));
    // F1: cancel the AlertDialog → no /end POST.
    fireEvent.click(await screen.findByTestId('session-end-confirm-cancel'));

    expect(fetchMock.mock.calls.find(([u]) => typeof u === 'string' && u.endsWith('/end'))).toBeUndefined();
  });

  it('after a session_end event the button is disabled and labeled "session ended"', async () => {
    renderSessionPage();
    const btn = await screen.findByTestId('session-end-button');
    expect(btn).not.toBeDisabled();

    await waitFor(() => expect(MockWebSocket.instances).toHaveLength(1));
    act(() => {
      MockWebSocket.instances[0]!.simulateMessage({
        kind: 'hello', protocol_version: 1, session_status: 'active', last_seq: 0,
      });
      MockWebSocket.instances[0]!.simulateMessage({
        kind: 'event',
        event: {
          id: 'e-end', seq: 1, session_code: 'AAAA-BBBB',
          timestamp: '2026-05-15T10:01:00Z', type: 'session_end', payload: { reason: 'interviewer' },
        },
      });
    });

    expect(btn).toBeDisabled();
    expect(btn).toHaveTextContent(/session ended/i);
  });

  it('REST 401 on /end → shows an error, button re-enabled', async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (typeof url === 'string' && url.endsWith('/end')) {
        return Promise.resolve({
          ok: false,
          status: 401,
          json: () => Promise.resolve({ detail: 'Bad token' }),
        });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(makeConfig()),
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    renderSessionPage();
    const btn = await screen.findByTestId('session-end-button');
    fireEvent.click(btn);
    const confirmBtn = await screen.findByTestId('session-end-confirm-confirm');
    await act(async () => {
      fireEvent.click(confirmBtn);
    });

    const err = await screen.findByTestId('session-end-error');
    expect(err).toHaveTextContent(/Bad token/);
    expect(btn).not.toBeDisabled();
  });
});

describe('SessionPage IDE layout', () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    vi.stubGlobal('WebSocket', MockWebSocket);
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string) => {
        if (typeof url === 'string' && url.endsWith('/files')) {
          return Promise.resolve({
            ok: true, status: 200,
            json: () => Promise.resolve({ entries: [] }),
          });
        }
        return Promise.resolve({
          ok: true, status: 200,
          json: () => Promise.resolve(makeConfig()),
        });
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('file tree panel is rendered in IDE layout', async () => {
    renderSessionPage();
    await screen.findByTestId('session-code-copy');
    expect(screen.getByTestId('session-file-tree')).toBeInTheDocument();
  });

  it('console section is rendered in IDE layout', async () => {
    renderSessionPage();
    await screen.findByTestId('session-code-copy');
    expect(screen.getByTestId('session-console-section')).toBeInTheDocument();
    expect(screen.getByTestId('session-console-toggle')).toBeInTheDocument();
  });

  // sandbox_restored event triggers a transient banner on the interviewer.
  it('sandbox_restored event shows the restored-from-history banner', async () => {
    renderSessionPage();
    await screen.findByTestId('session-code-copy');
    await waitFor(() => expect(MockWebSocket.instances).toHaveLength(1));

    expect(screen.queryByTestId('session-sandbox-restored-banner')).not.toBeInTheDocument();

    act(() => {
      MockWebSocket.instances[0]!.simulateMessage({
        kind: 'hello', protocol_version: 6, session_status: 'active', last_seq: 0,
      });
      MockWebSocket.instances[0]!.simulateMessage({
        kind: 'event',
        event: {
          id: 'e-rstr', seq: 1, session_code: 'AAAA-BBBB',
          timestamp: '2026-05-15T10:02:00Z',
          type: 'sandbox_restored',
          payload: { file_count: 7, deleted_count: 0 },
        },
      });
    });

    const banner = await screen.findByTestId('session-sandbox-restored-banner');
    expect(banner).toBeInTheDocument();
    expect(banner.textContent).toMatch(/7/);
  });

  // A Reset sandbox by the interviewer re-fetches the file tree, so files
  // created during the session disappear from the interviewer's tree without an F5.
  it('sandbox_reset event re-fetches the file tree', async () => {
    let resetDone = false;
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string) => {
        if (typeof url === 'string' && url.endsWith('/files')) {
          const entries = resetDone
            ? [{ path: 'train.py', is_dir: false }]
            : [{ path: 'train.py', is_dir: false }, { path: 'newfile.txt', is_dir: false }];
          return Promise.resolve({
            ok: true, status: 200,
            json: () => Promise.resolve({ entries }),
          });
        }
        return Promise.resolve({
          ok: true, status: 200,
          json: () => Promise.resolve(makeConfig({})),
        });
      }),
    );

    renderSessionPage();
    await screen.findByTestId('session-code-copy');
    await waitFor(() => expect(MockWebSocket.instances).toHaveLength(1));

    act(() => {
      MockWebSocket.instances[0]!.simulateMessage({
        kind: 'hello', protocol_version: 6, session_status: 'active', last_seq: 0,
      });
      MockWebSocket.instances[0]!.simulateMessage({
        kind: 'event',
        event: {
          id: 'e-ss', seq: 1, session_code: 'AAAA-BBBB',
          timestamp: '2026-05-15T10:01:00Z',
          type: 'session_start', payload: {},
        },
      });
    });

    // Tree loads with the session-created file present.
    expect(await screen.findByText('newfile.txt')).toBeInTheDocument();

    // Interviewer resets the sandbox → baseline tree from now on.
    resetDone = true;
    act(() => {
      MockWebSocket.instances[0]!.simulateMessage({
        kind: 'event',
        event: {
          id: 'e-rst', seq: 2, session_code: 'AAAA-BBBB',
          timestamp: '2026-05-15T10:02:00Z',
          type: 'sandbox_reset', payload: {},
        },
      });
    });

    // The debounced re-fetch drops the file without an F5.
    await waitFor(() => expect(screen.queryByText('newfile.txt')).not.toBeInTheDocument());
    expect(screen.getByText('train.py')).toBeInTheDocument();
  });

  it('sandbox_reset re-reads the open file content from baseline (A.4)', async () => {
    let resetDone = false;
    let contentFetches = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string) => {
        if (typeof url === 'string' && url.includes('/files/content')) {
          contentFetches += 1;
          // Modified content before reset, baseline content after.
          return Promise.resolve({
            ok: true, status: 200,
            text: () => Promise.resolve(resetDone ? 'baseline\n' : 'baseline\n# manual edit\n'),
          });
        }
        if (typeof url === 'string' && url.endsWith('/files')) {
          return Promise.resolve({
            ok: true, status: 200,
            json: () => Promise.resolve({ entries: [{ path: 'train.py', is_dir: false }] }),
          });
        }
        return Promise.resolve({
          ok: true, status: 200,
          json: () => Promise.resolve(makeConfig({})),
        });
      }),
    );

    renderSessionPage();
    await screen.findByTestId('session-code-copy');
    await waitFor(() => expect(MockWebSocket.instances).toHaveLength(1));

    act(() => {
      MockWebSocket.instances[0]!.simulateMessage({
        kind: 'hello', protocol_version: 6, session_status: 'active', last_seq: 0,
      });
      MockWebSocket.instances[0]!.simulateMessage({
        kind: 'event',
        event: {
          id: 'e-ss', seq: 1, session_code: 'AAAA-BBBB',
          timestamp: '2026-05-15T10:01:00Z',
          type: 'session_start', payload: {},
        },
      });
    });

    // Open train.py → first content fetch.
    const fileNode = await screen.findByText('train.py');
    fireEvent.click(fileNode);
    await waitFor(() => expect(contentFetches).toBe(1));

    // Reset → the open file's stale buffer must be re-read from baseline.
    resetDone = true;
    act(() => {
      MockWebSocket.instances[0]!.simulateMessage({
        kind: 'event',
        event: {
          id: 'e-rst', seq: 2, session_code: 'AAAA-BBBB',
          timestamp: '2026-05-15T10:02:00Z',
          type: 'sandbox_reset', payload: {},
        },
      });
    });

    await waitFor(() => expect(contentFetches).toBe(2));
  });

  // Switching to the History tab must not hide the AI chat panel.
  // The original CSS bug (wide diff in historyPane expanding the 1fr editor column,
  // pushing the chat past the grid's overflow:hidden) is visual and was verified
  // fixed in a real browser — jsdom applies no layout, so this test cannot catch
  // the clip. It guards the structural invariant instead: the chat subtree stays
  // mounted across the Content↔History switch (i.e. the tabs never wrap the split).
  it('History tab keeps the chat panel mounted', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string) => {
        if (typeof url === 'string' && url.includes('/files/content')) {
          return Promise.resolve({
            ok: true, status: 200,
            text: () => Promise.resolve('print("hi")\n'),
          });
        }
        if (typeof url === 'string' && url.endsWith('/files')) {
          return Promise.resolve({
            ok: true, status: 200,
            json: () => Promise.resolve({ entries: [{ path: 'train.py', is_dir: false }] }),
          });
        }
        return Promise.resolve({
          ok: true, status: 200,
          json: () => Promise.resolve(makeConfig({})),
        });
      }),
    );

    renderSessionPage();
    await screen.findByTestId('session-code-copy');
    await waitFor(() => expect(MockWebSocket.instances).toHaveLength(1));

    act(() => {
      MockWebSocket.instances[0]!.simulateMessage({
        kind: 'hello', protocol_version: 6, session_status: 'active', last_seq: 0,
      });
      MockWebSocket.instances[0]!.simulateMessage({
        kind: 'event',
        event: {
          id: 'e-fc', seq: 1, session_code: 'AAAA-BBBB',
          timestamp: '2026-05-15T10:01:00Z',
          type: 'session_start', payload: {},
        },
      });
      MockWebSocket.instances[0]!.simulateMessage({
        kind: 'event',
        event: {
          id: 'e-fc2', seq: 2, session_code: 'AAAA-BBBB',
          timestamp: '2026-05-15T10:01:01Z',
          type: 'file_changed',
          payload: { file_path: 'train.py', op: 'created', content: 'print("hi")\n', source: 'manual' },
        },
      });
    });

    // Chat panel visible in content mode.
    expect(screen.getByTestId('session-chat-panel')).toBeInTheDocument();

    // Select the file → tab bar appears.
    const fileNode = await screen.findByText('train.py');
    fireEvent.click(fileNode);
    const historyTab = await screen.findByTestId('editor-tab-history');

    // Switch to History → chat panel must stay mounted.
    fireEvent.click(historyTab);
    expect(screen.getByTestId('editor-tab-history')).toHaveAttribute('data-active', 'true');
    expect(screen.getByTestId('session-chat-panel')).toBeInTheDocument();
  });

  it('command_run event auto-opens console body', async () => {
    renderSessionPage();
    await screen.findByTestId('session-code-copy');
    await waitFor(() => expect(MockWebSocket.instances).toHaveLength(1));

    expect(screen.queryByTestId('session-console-body')).not.toBeInTheDocument();

    act(() => {
      MockWebSocket.instances[0]!.simulateMessage({
        kind: 'event',
        event: {
          id: 'e-run', seq: 1, session_code: 'AAAA-BBBB',
          timestamp: '2026-05-15T10:01:00Z',
          type: 'command_run',
          payload: { command_id: 'cmd-1', command: 'python train.py', cwd: '.' },
        },
      });
    });

    await waitFor(() =>
      expect(screen.getByTestId('session-console-body')).toBeInTheDocument(),
    );
  });
});

describe('SessionPage share button', () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    vi.stubGlobal('WebSocket', MockWebSocket);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('share button disabled on active (pending) session (completed-only mint)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: () => Promise.resolve(makeConfig({ session_status: 'pending' })),
      }),
    );
    renderSessionPage();
    const btn = await screen.findByTestId('session-share-button');
    expect(btn).toBeInTheDocument();
    expect(btn).toBeDisabled();
  });

  it('share button appears when session is completed', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string) => ({
        ok: true,
        status: 200,
        json: () => Promise.resolve(
          typeof url === 'string' && url.includes('/events')
            ? []
            : makeConfig({ session_status: 'completed' }),
        ),
      })),
    );
    renderSessionPage();
    const btn = await screen.findByTestId('session-share-button');
    expect(btn).toBeInTheDocument();
    expect(btn).toHaveTextContent(/share replay/i);
  });

  it('click share → calls POST /share and copies link immediately (first click)', async () => {
    const fetchMock = vi.fn().mockImplementation((url: string, opts: RequestInit) => {
      if (typeof url === 'string' && url.includes('/share') && opts?.method === 'POST') {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ share_token: 'tok-abc-123' }),
        });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(
          typeof url === 'string' && url.includes('/events')
            ? []
            : makeConfig({ session_status: 'completed' }),
        ),
      });
    });
    vi.stubGlobal('fetch', fetchMock);
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });

    renderSessionPage();
    const btn = await screen.findByTestId('session-share-button');
    await act(async () => { (btn as HTMLButtonElement).click(); });

    // label switches to "Copied!" immediately on first click
    await screen.findByText(/copied/i);
    const shareCall = fetchMock.mock.calls.find(
      ([u, o]) => typeof u === 'string' && u.includes('/share') && (o as RequestInit)?.method === 'POST',
    );
    expect(shareCall).toBeDefined();
    expect((shareCall![1] as RequestInit).credentials).toBe('include');
    expect(writeText).toHaveBeenCalledWith(
      expect.stringContaining('/replay?share_token=tok-abc-123'),
    );
  });

  it('second click copies without calling POST /share again', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fetchMock = vi.fn().mockImplementation((url: string, opts: RequestInit) => {
      if (typeof url === 'string' && url.includes('/share') && opts?.method === 'POST') {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ share_token: 'tok-xyz-999' }),
        });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(
          typeof url === 'string' && url.includes('/events')
            ? []
            : makeConfig({ session_status: 'completed' }),
        ),
      });
    });
    vi.stubGlobal('fetch', fetchMock);
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });

    renderSessionPage();
    const btn = await screen.findByTestId('session-share-button');
    // first click — generates token + copies
    await act(async () => { (btn as HTMLButtonElement).click(); });
    await screen.findByText(/copied/i);
    // advance past the 1500ms "Copied!" → "Copy replay link" reset
    await act(async () => { vi.advanceTimersByTime(2000); });
    await screen.findByText(/copy replay link/i);
    // second click — copies again without new POST
    await act(async () => { (btn as HTMLButtonElement).click(); });
    await screen.findByText(/copied/i);

    const shareCalls = fetchMock.mock.calls.filter(
      ([u, o]) => typeof u === 'string' && u.includes('/share') && (o as RequestInit)?.method === 'POST',
    );
    expect(shareCalls).toHaveLength(1);
    expect(writeText).toHaveBeenCalledTimes(2);
    vi.useRealTimers();
  });
});
