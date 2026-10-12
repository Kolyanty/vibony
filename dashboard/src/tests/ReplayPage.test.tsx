import { act, render, screen } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ReplayPage } from '../pages/ReplayPage';
import type { SessionConfig, EventEnvelope } from '../api/types';

function makeConfig(overrides: Partial<SessionConfig> = {}): SessionConfig {
  return {
    session_code: 'AAAA-BBBB',
    session_status: 'completed',
    protocol_version: 2,
    ws_token: 'ws-tok',
    task_id: 'task-01-training-loop',
    task_dir_name: 'training-loop',
    ai_config: { primary_model: 'deepseek-v4-flash', fallback_models: [] },
    bundle_sha256: null,
    created_at: '2026-05-15T10:00:00Z',
    expires_at: '2026-05-15T18:00:00Z',
    ...overrides,
  };
}

function makeEvent(seq: number, type = 'user_message'): EventEnvelope {
  return {
    id: `evt-${seq}`,
    seq,
    session_code: 'AAAA-BBBB',
    timestamp: '2026-05-19T10:00:00Z',
    type,
    payload: { text: `msg ${seq}` },
  } as EventEnvelope;
}

function renderReplayPage(code = 'AAAA-BBBB', shareToken = 'share-tok') {
  const router = createMemoryRouter(
    [{ path: '/sessions/:code/replay', element: <ReplayPage /> }],
    { initialEntries: [`/sessions/${code}/replay?share_token=${shareToken}`] },
  );
  return render(<RouterProvider router={router} />);
}

describe('ReplayPage', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi.fn()
        .mockResolvedValueOnce({
          ok: true,
          status: 200,
          json: () => Promise.resolve(makeConfig()),
        })
        .mockResolvedValueOnce({
          ok: true,
          status: 200,
          json: () => Promise.resolve([makeEvent(1), makeEvent(2)]),
        }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('shows loading state initially', () => {
    vi.stubGlobal('fetch', vi.fn().mockReturnValue(new Promise(() => {})));
    renderReplayPage();
    expect(screen.getByText(/loading session replay/i)).toBeInTheDocument();
  });

  it('renders session code in header after loading', async () => {
    renderReplayPage();
    expect(await screen.findByText('AAAA-BBBB')).toBeInTheDocument();
  });

  it('renders replay title in header', async () => {
    renderReplayPage();
    await screen.findByText('AAAA-BBBB');
    expect(screen.getByText(/session replay/i)).toBeInTheDocument();
  });

  it('renders timeline component after loading events', async () => {
    renderReplayPage();
    await screen.findByText('AAAA-BBBB');
    // Config and events load in two phases (config → ready → events fetch), so the
    // timeline scrubber appears asynchronously once events arrive — await it.
    expect(await screen.findByRole('slider')).toBeInTheDocument();
  });

  it('renders no end session button (read-only)', async () => {
    renderReplayPage();
    await screen.findByText('AAAA-BBBB');
    expect(screen.queryByTestId('session-end-button')).not.toBeInTheDocument();
  });

  it('shows error on 403 (wrong share token)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 403,
        json: () => Promise.resolve({ detail: { reason: 'unauthorized' } }),
      }),
    );
    await act(async () => {
      renderReplayPage('AAAA-BBBB', 'bad-token');
    });
    // Header shows error title (h1), body shows HTTP status
    expect(await screen.findByRole('heading')).toBeInTheDocument();
    expect(screen.queryByText(/loading/i)).not.toBeInTheDocument();
  });

  it('shows missing token error when share_token absent in URL', () => {
    const router = createMemoryRouter(
      [{ path: '/sessions/:code/replay', element: <ReplayPage /> }],
      { initialEntries: ['/sessions/AAAA-BBBB/replay'] },
    );
    render(<RouterProvider router={router} />);
    expect(screen.getByText(/missing share_token/i)).toBeInTheDocument();
  });

  it('shows missing code error when code absent in URL', () => {
    const router = createMemoryRouter(
      [{ path: '/sessions/replay', element: <ReplayPage /> }],
      { initialEntries: ['/sessions/replay?share_token=tok'] },
    );
    render(<RouterProvider router={router} />);
    // path param :code missing — will default to undefined
    expect(screen.getByText(/missing session code/i)).toBeInTheDocument();
  });
});

describe('ReplayPage — file tree projection', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function fileChanged(seq: number, file_path: string, payload: Record<string, unknown>): EventEnvelope {
    return {
      id: `evt-${seq}`,
      seq,
      session_code: 'AAAA-BBBB',
      timestamp: '2026-05-19T10:00:00Z',
      type: 'file_changed',
      payload: { file_path, ...payload },
    } as EventEnvelope;
  }

  it('hides files deleted during the session, keeps survivors', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn()
        .mockResolvedValueOnce({ ok: true, status: 200, json: () => Promise.resolve(makeConfig()) })
        .mockResolvedValueOnce({
          ok: true,
          status: 200,
          json: () =>
            Promise.resolve([
              fileChanged(1, 'keep.py', { content: 'k', op: 'created' }),
              fileChanged(2, 'gone.py', { content: 'g', op: 'created' }),
              fileChanged(3, 'gone.py', { content: null, op: 'deleted' }),
            ]),
        }),
    );
    renderReplayPage();
    expect(await screen.findByText('keep.py')).toBeInTheDocument();
    expect(screen.queryByText('gone.py')).not.toBeInTheDocument();
  });
});
