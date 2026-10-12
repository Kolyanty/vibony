import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { TasksPage } from '../pages/TasksPage';

function makeMe() {
  return {
    user: { id: 'u1', email: 'a@acme.com', created_at: '2026-05-01T00:00:00Z' },
    memberships: [{ workspace_id: 'c1', company_name: 'Acme', role: 'admin' as const }],
  };
}

function makeTasks() {
  return [
    {
      id: 'own1',
      company_id: 'c1',
      slug: 'my-task',
      title: 'My Task',
      runtime_image: 'vibony/sandbox-py:3.12',
      visibility: 'workspace',
      is_template: false,
      can_edit: true,
      is_favorite: false,
      created_at: '2026-05-02T00:00:00Z',
      updated_at: '2026-05-02T00:00:00Z',
    },
    {
      id: 'tmpl1',
      company_id: 'sys',
      slug: 'task-01-training-loop',
      title: 'Training loop',
      runtime_image: 'vibony/sandbox-py:3.12',
      visibility: 'public',
      is_template: true,
      can_edit: false,
      is_favorite: false,
      created_at: '2026-05-01T00:00:00Z',
      updated_at: '2026-05-01T00:00:00Z',
    },
  ];
}

function renderPage() {
  const router = createMemoryRouter(
    [
      { path: '/tasks', element: <TasksPage /> },
      { path: '/login', element: <p data-testid="login-page">Login</p> },
      { path: '/tasks/new', element: <p data-testid="new-page">New</p> },
      { path: '/tasks/:id/edit', element: <p data-testid="edit-page">Edit</p> },
    ],
    { initialEntries: ['/tasks'] },
  );
  return render(<RouterProvider router={router} />);
}

function stubFetch(
  opts: {
    tasks?: unknown[];
    onDelete?: () => void;
    onFavorite?: (id: string, method: string) => void;
    favoriteStatus?: number;
  } = {},
) {
  const tasks = opts.tasks ?? makeTasks();
  const favoriteStatus = opts.favoriteStatus ?? 204;
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      if (typeof url === 'string' && url.includes('/auth/me')) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makeMe()) });
      }
      // Favorite toggle (PUT/DELETE /tasks/{id}/favorite) — checked before the
      // delete-task branch so a DELETE .../favorite isn't mistaken for a task delete.
      const favMatch = typeof url === 'string' ? url.match(/\/tasks\/([^/]+)\/favorite$/) : null;
      if (favMatch) {
        opts.onFavorite?.(favMatch[1]!, init?.method ?? 'GET');
        return Promise.resolve({
          ok: favoriteStatus < 400,
          status: favoriteStatus,
          json: () => Promise.resolve({ detail: 'nope' }),
        });
      }
      if (typeof url === 'string' && url.includes('/tasks/') && init?.method === 'DELETE') {
        opts.onDelete?.();
        return Promise.resolve({ ok: true, status: 204, json: () => Promise.resolve({}) });
      }
      if (typeof url === 'string' && url.includes('/tasks')) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ tasks }) });
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
    }),
  );
}

describe('TasksPage', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('renders own task (editable) + template (read-only)', async () => {
    stubFetch();
    renderPage();
    await waitFor(() => expect(screen.getByTestId('tasks-table')).toBeInTheDocument());

    expect(screen.getByTestId('task-row-own1')).toBeInTheDocument();
    expect(screen.getByTestId('task-edit-own1')).toBeInTheDocument();
    expect(screen.getByTestId('task-delete-own1')).toBeInTheDocument();

    // Template row: no edit/delete controls.
    expect(screen.getByTestId('task-row-tmpl1')).toBeInTheDocument();
    expect(screen.queryByTestId('task-edit-tmpl1')).not.toBeInTheDocument();
  });

  it('empty state when no tasks', async () => {
    stubFetch({ tasks: [] });
    renderPage();
    await waitFor(() => expect(screen.getByTestId('tasks-empty')).toBeInTheDocument());
  });

  it('delete flow removes the row', async () => {
    const onDelete = vi.fn();
    stubFetch({ onDelete });
    renderPage();
    await waitFor(() => expect(screen.getByTestId('task-row-own1')).toBeInTheDocument());

    fireEvent.click(screen.getByTestId('task-delete-own1'));
    expect(screen.getByTestId('delete-modal')).toBeInTheDocument();
    await act(async () => {
      fireEvent.click(screen.getByTestId('delete-confirm'));
    });

    await waitFor(() => expect(screen.queryByTestId('task-row-own1')).not.toBeInTheDocument());
    expect(onDelete).toHaveBeenCalled();
    // Template row remains.
    expect(screen.getByTestId('task-row-tmpl1')).toBeInTheDocument();
  });

  it('new task button navigates to /tasks/new', async () => {
    stubFetch();
    renderPage();
    await waitFor(() => expect(screen.getByTestId('new-task-button')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('new-task-button'));
    await waitFor(() => expect(screen.getByTestId('new-page')).toBeInTheDocument());
  });

  it('star toggles a favorite (optimistic PUT, then DELETE)', async () => {
    const favCalls: Array<[string, string]> = [];
    stubFetch({ onFavorite: (id, method) => favCalls.push([id, method]) });
    renderPage();
    await waitFor(() => expect(screen.getByTestId('task-row-own1')).toBeInTheDocument());

    const star = screen.getByTestId('task-favorite-own1');
    expect(star).toHaveAttribute('aria-pressed', 'false');

    // Favorite → optimistic pressed + PUT.
    await act(async () => {
      fireEvent.click(star);
    });
    await waitFor(() => expect(screen.getByTestId('task-favorite-own1')).toHaveAttribute('aria-pressed', 'true'));
    expect(favCalls).toContainEqual(['own1', 'PUT']);

    // Unfavorite → back to not-pressed + DELETE.
    await act(async () => {
      fireEvent.click(screen.getByTestId('task-favorite-own1'));
    });
    await waitFor(() => expect(screen.getByTestId('task-favorite-own1')).toHaveAttribute('aria-pressed', 'false'));
    expect(favCalls).toContainEqual(['own1', 'DELETE']);
  });

  it('gathers favorited tasks into a Favorites section at the top', async () => {
    const tasks = makeTasks().map((t) => (t.id === 'own1' ? { ...t, is_favorite: true } : t));
    stubFetch({ tasks });
    renderPage();
    await waitFor(() => expect(screen.getByTestId('tasks-favorites-section')).toBeInTheDocument());

    const section = screen.getByTestId('tasks-favorites-section');
    // The favorited task lives in the Favorites section; the non-favorite does not.
    expect(within(section).getByTestId('task-row-own1')).toBeInTheDocument();
    expect(within(section).queryByTestId('task-row-tmpl1')).not.toBeInTheDocument();
    expect(screen.getByText('Favorites')).toBeInTheDocument();
    expect(screen.getByText('Other tasks')).toBeInTheDocument();
  });

  it('searches the catalog (debounced ?q=) and shows a search-empty message', async () => {
    const calls: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string) => {
        if (typeof url === 'string' && url.includes('/auth/me')) {
          return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makeMe()) });
        }
        if (typeof url === 'string' && url.includes('/tasks')) {
          calls.push(url);
          const hasQuery = url.includes('?q=');
          return Promise.resolve({
            ok: true,
            status: 200,
            json: () => Promise.resolve({ tasks: hasQuery ? [] : makeTasks() }),
          });
        }
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
      }),
    );
    renderPage();
    await waitFor(() => expect(screen.getByTestId('task-row-own1')).toBeInTheDocument());

    fireEvent.change(screen.getByTestId('tasks-search'), { target: { value: 'zephyr' } });

    // The debounced ?q= request fires; no results → the search-empty message shows.
    await waitFor(() => expect(screen.getByTestId('tasks-empty')).toBeInTheDocument());
    expect(screen.getByTestId('tasks-empty')).toHaveTextContent('No tasks match your search');
    expect(calls.some((u) => u.includes('q=zephyr'))).toBe(true);
  });

  it('reverts the star + shows an error when the favorite request fails', async () => {
    stubFetch({ favoriteStatus: 500 });
    renderPage();
    await waitFor(() => expect(screen.getByTestId('task-row-own1')).toBeInTheDocument());

    await act(async () => {
      fireEvent.click(screen.getByTestId('task-favorite-own1'));
    });

    // Optimistic flip is rolled back and the error surfaces.
    await waitFor(() => expect(screen.getByTestId('tasks-error')).toBeInTheDocument());
    expect(screen.getByTestId('task-favorite-own1')).toHaveAttribute('aria-pressed', 'false');
  });
});
