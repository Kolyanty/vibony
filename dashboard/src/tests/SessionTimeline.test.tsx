import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ReplayPage } from '../pages/ReplayPage';
import { SessionPage } from '../pages/SessionPage';
import type { EventEnvelope, SessionAssessment, SessionConfig } from '../api/types';

function makeConfig(overrides: Partial<SessionConfig> = {}): SessionConfig {
  return {
    session_code: 'AAAA-BBBB',
    session_status: 'completed',
    protocol_version: 7,
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

function ev(seq: number, type: string, payload: Record<string, unknown>): EventEnvelope {
  return {
    id: `e${seq}`,
    seq,
    session_code: 'AAAA-BBBB',
    timestamp: '2026-05-15T10:0' + seq + ':00Z',
    type,
    payload,
  } as EventEnvelope;
}

const EVENTS: EventEnvelope[] = [
  ev(1, 'user_message', { text: 'fix the loop' }),
  ev(2, 'file_changed', { file_path: 'train.py', content: 'x', op: 'modified' }),
  ev(3, 'file_changed', { file_path: 'model.py', content: 'y', op: 'modified' }),
  ev(4, 'session_end', { reason: 'interviewer' }),
];

function jsonOk(body: unknown) {
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

// ---- Public replay: the timeline tab is available, assessment is not ----------

describe('SessionView timeline tab — public replay', () => {
  function renderReplay() {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string) => {
        if (typeof url === 'string' && url.includes('/events')) return jsonOk(EVENTS);
        return jsonOk(makeConfig()); // /replay config
      }),
    );
    const router = createMemoryRouter(
      [{ path: '/sessions/:code/replay', element: <ReplayPage /> }],
      { initialEntries: ['/sessions/AAAA-BBBB/replay?share_token=tok'] },
    );
    return render(<RouterProvider router={router} />);
  }

  it('shows Dialog + Timeline tabs but not the interviewer-only AI assessment', async () => {
    renderReplay();
    await screen.findByText('AAAA-BBBB');
    expect(await screen.findByTestId('right-tab-timeline')).toBeInTheDocument();
    expect(screen.getByTestId('right-tab-dialog')).toBeInTheDocument();
    expect(screen.queryByTestId('right-tab-assessment')).not.toBeInTheDocument();
  });

  it('clicking a timeline row moves the replay cursor (scrubber follows)', async () => {
    renderReplay();
    await screen.findByText('AAAA-BBBB');
    fireEvent.click(await screen.findByTestId('right-tab-timeline'));

    expect(await screen.findByTestId('session-timeline-panel')).toBeInTheDocument();
    // Events load asynchronously (config → ready → events); await the row + scrubber.
    const header = await screen.findByRole('button', { name: 'fix the loop' });
    const slider = await screen.findByRole('slider');
    // cursor starts at the end (all events visible).
    expect(slider).toHaveAttribute('aria-valuenow', String(EVENTS.length));

    // Jump to the opening user message (seq 1) → cursor = 1.
    fireEvent.click(header);
    expect(slider).toHaveAttribute('aria-valuenow', '1');
  });
});

// ---- Interviewer completed session: deep-link from an assessment finding ------

describe('SessionView assessment deep-link', () => {
  const ASSESSMENT: SessionAssessment = {
    status: 'ready',
    summary_md: '## What went well\nSolid.',
    findings: [
      {
        category: 'gap',
        title: 'No optimizer state saved',
        detail: 'Only weights persisted.',
        severity: 'blocking',
        event_seq: 2,
      },
    ],
    model: 'deepseek/deepseek-chat',
    error: null,
    created_at: '2026-06-01T10:00:00Z',
    updated_at: '2026-06-01T10:01:00Z',
  };

  function renderInterviewer() {
    vi.stubGlobal('WebSocket', class { close() {} } as unknown as typeof WebSocket);
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string) => {
        if (typeof url === 'string' && url.includes('/assessment')) return jsonOk(ASSESSMENT);
        if (typeof url === 'string' && url.includes('/events')) return jsonOk(EVENTS);
        if (typeof url === 'string' && url.endsWith('/files')) return jsonOk({ entries: [] });
        return jsonOk(makeConfig());
      }),
    );
    const router = createMemoryRouter(
      [{ path: '/session/:code', element: <SessionPage /> }],
      { initialEntries: ['/session/AAAA-BBBB'] },
    );
    return render(<RouterProvider router={router} />);
  }

  it('clicking a finding seq chip opens the timeline at the cited event', async () => {
    renderInterviewer();
    await screen.findByTestId('session-code-copy');

    // Open the AI assessment tab → the finding (+ its event-seq chip) renders.
    fireEvent.click(await screen.findByTestId('right-tab-assessment'));
    const chip = await screen.findByTestId('assessment-finding-seq');
    expect(chip).toHaveTextContent('event #2');

    // Deep-link: jump to event 2.
    fireEvent.click(chip);

    // Timeline tab is now active and shown.
    await waitFor(() =>
      expect(screen.getByTestId('right-tab-timeline')).toHaveAttribute('data-active', 'true'),
    );
    const panel = screen.getByTestId('session-timeline-panel');

    // Cursor moved from the end (4) to the cited event (2); the target row is flashed.
    expect(screen.getByRole('slider')).toHaveAttribute('aria-valuenow', '2');
    // 'train.py' also appears in the file tree — scope to the timeline panel.
    expect(within(panel).getByText('train.py').closest('button')).toHaveAttribute(
      'data-flash',
      'true',
    );
  });

  it('does not fetch the live /files tree for a completed session', async () => {
    // The sandbox is torn down on completion, so REST /files would just 503; the
    // tree is derived from events instead. Assert SessionView never calls /files.
    const fetchSpy = vi.fn().mockImplementation((url: string) => {
      if (typeof url === 'string' && url.includes('/assessment')) return jsonOk(ASSESSMENT);
      if (typeof url === 'string' && url.includes('/events')) return jsonOk(EVENTS);
      if (typeof url === 'string' && url.endsWith('/files')) return jsonOk({ entries: [] });
      return jsonOk(makeConfig());
    });
    vi.stubGlobal('WebSocket', class { close() {} } as unknown as typeof WebSocket);
    vi.stubGlobal('fetch', fetchSpy);
    const router = createMemoryRouter(
      [{ path: '/session/:code', element: <SessionPage /> }],
      { initialEntries: ['/session/AAAA-BBBB'] },
    );
    render(<RouterProvider router={router} />);
    await screen.findByTestId('session-code-copy');
    // The file tree is built from events (file_changed), not REST — wait for it.
    await screen.findByText('train.py');

    const calledFiles = fetchSpy.mock.calls.some(
      ([url]) => typeof url === 'string' && /\/files(\?|$)/.test(url),
    );
    expect(calledFiles).toBe(false);
  });
});
