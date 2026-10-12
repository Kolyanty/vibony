/**
 * SessionPage file history tab tests:
 * 1. Tab bar is not rendered without a selected file.
 * 2. Tab bar appears after file selection; Content tab is active by default.
 * 3. History tab renders the FileDiffHistory toggle with entries.
 * 4. Switching to a different file resets the active tab back to Content.
 */

import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SessionPage } from '../pages/SessionPage';
import type { EventEnvelope, SessionConfig } from '../api/types';

// ---- Monaco mock -----------------------------------------------------------

vi.mock('../components/MonacoEditor', () => ({
  default: ({ path }: { path?: string }) => (
    <div data-testid="monaco-stub" data-path={path} />
  ),
}));

// ---- Fixtures --------------------------------------------------------------

function makeCompletedConfig(): SessionConfig {
  return {
    session_code: 'AAAA-BBBB',
    session_status: 'completed',
    protocol_version: 6,
    ws_token: '',
    task_id: 'task-01-training-loop',
    task_dir_name: 'training-loop',
    ai_config: { primary_model: 'model', fallback_models: [] },
    bundle_sha256: null,
    created_at: '2026-05-21T10:00:00Z',
    expires_at: '2026-05-21T18:00:00Z',
  };
}

function makeFileChangedEvent(overrides: {
  id?: string;
  seq?: number;
  path?: string;
  content?: string;
}): EventEnvelope {
  return {
    id: overrides.id ?? 'ev-fc-1',
    seq: overrides.seq ?? 1,
    session_code: 'AAAA-BBBB',
    timestamp: '2026-05-21T10:00:30Z',
    type: 'file_changed',
    payload: {
      file_path: overrides.path ?? 'train.py',
      source: 'agent',
      diff: null,
      content: overrides.content ?? 'print("hello")\n',
      op: 'modified',
    },
  };
}

function setupFetch(events: EventEnvelope[]) {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation((url: string) => {
      const u = url as string;
      if (u.includes('/events')) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve(events),
        });
      }
      if (u.includes('/files')) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ entries: [] }),
        });
      }
      // Default: session config
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(makeCompletedConfig()),
      });
    }),
  );
}

function renderSessionPage(code = 'AAAA-BBBB') {
  const router = createMemoryRouter(
    [{ path: '/session/:code', element: <SessionPage /> }],
    { initialEntries: [`/session/${code}`] },
  );
  return render(<RouterProvider router={router} />);
}

// ---- Tests -----------------------------------------------------------------

describe('SessionPage file history tab', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('tab bar is not rendered without a selected file', async () => {
    const events: EventEnvelope[] = [makeFileChangedEvent({ path: 'train.py' })];
    setupFetch(events);
    renderSessionPage();

    await screen.findByTestId('session-code-copy');

    expect(screen.queryByTestId('editor-tab-content')).not.toBeInTheDocument();
    expect(screen.queryByTestId('editor-tab-history')).not.toBeInTheDocument();
  });

  it('tab bar appears after file selection; Content tab is active by default', async () => {
    const events: EventEnvelope[] = [makeFileChangedEvent({ path: 'train.py', content: 'x = 1\n' })];
    setupFetch(events);
    renderSessionPage();

    await screen.findByTestId('session-code-copy');
    const fileBtn = await screen.findByTestId('tree-file-train.py');

    act(() => {
      fireEvent.click(fileBtn);
    });

    await waitFor(() =>
      expect(screen.getByTestId('editor-tab-content')).toBeInTheDocument(),
    );
    expect(screen.getByTestId('editor-tab-history')).toBeInTheDocument();

    expect(screen.getByTestId('editor-tab-content')).toHaveAttribute('data-active', 'true');
    expect(screen.getByTestId('editor-tab-history')).toHaveAttribute('data-active', 'false');
  });

  it('History tab renders FileDiffHistory toggle with entries', async () => {
    const events: EventEnvelope[] = [makeFileChangedEvent({ path: 'train.py', content: 'x = 1\n' })];
    setupFetch(events);
    renderSessionPage();

    await screen.findByTestId('session-code-copy');
    const fileBtn = await screen.findByTestId('tree-file-train.py');

    act(() => {
      fireEvent.click(fileBtn);
    });

    await waitFor(() =>
      expect(screen.getByTestId('editor-tab-history')).toBeInTheDocument(),
    );

    act(() => {
      fireEvent.click(screen.getByTestId('editor-tab-history'));
    });

    // FileDiffHistory starts open (defaultOpen=true) and exposes its toggle button.
    await waitFor(() =>
      expect(screen.getByTestId('files-history-toggle')).toBeInTheDocument(),
    );
    expect(screen.getByTestId('editor-tab-history')).toHaveAttribute('data-active', 'true');
  });

  it('baseline file_changed does not mark the file as modified', async () => {
    const events: EventEnvelope[] = [
      {
        id: 'ev-base', seq: 1, session_code: 'AAAA-BBBB', timestamp: '2026-05-21T10:00:00Z',
        type: 'file_changed',
        payload: { file_path: 'data.py', source: 'baseline', diff: null, content: 'x = 1\n', op: 'created' },
      },
      makeFileChangedEvent({ id: 'ev-edit', seq: 2, path: 'train.py', content: 'print("hi")\n' }),
    ];
    setupFetch(events);
    renderSessionPage();

    await screen.findByTestId('session-code-copy');
    // Both files appear in the tree…
    await screen.findByTestId('tree-file-data.py');
    await screen.findByTestId('tree-file-train.py');
    // …but only the edited one carries the modified dot.
    expect(screen.getByTestId('tree-modified-dot-train.py')).toBeInTheDocument();
    expect(screen.queryByTestId('tree-modified-dot-data.py')).not.toBeInTheDocument();
  });

  it('switching to a different file resets the active tab to Content', async () => {
    const events: EventEnvelope[] = [
      makeFileChangedEvent({ id: 'ev-1', seq: 1, path: 'train.py', content: 'x = 1\n' }),
      makeFileChangedEvent({ id: 'ev-2', seq: 2, path: 'model.py', content: 'class M: pass\n' }),
    ];
    setupFetch(events);
    renderSessionPage();

    await screen.findByTestId('session-code-copy');
    const fileBtn = await screen.findByTestId('tree-file-train.py');

    // Select first file, switch to History tab.
    act(() => { fireEvent.click(fileBtn); });
    await waitFor(() => expect(screen.getByTestId('editor-tab-history')).toBeInTheDocument());
    act(() => { fireEvent.click(screen.getByTestId('editor-tab-history')); });
    expect(screen.getByTestId('editor-tab-history')).toHaveAttribute('data-active', 'true');

    // Select the second file — tab must reset to Content.
    const fileBtn2 = await screen.findByTestId('tree-file-model.py');
    act(() => { fireEvent.click(fileBtn2); });

    await waitFor(() =>
      expect(screen.getByTestId('editor-tab-content')).toHaveAttribute('data-active', 'true'),
    );
    expect(screen.getByTestId('editor-tab-history')).toHaveAttribute('data-active', 'false');
  });
});
