import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { TaskEditPage } from '../pages/TaskEditPage';

function makeMe() {
  return {
    user: { id: 'u1', email: 'a@acme.com', created_at: '2026-05-01T00:00:00Z' },
    memberships: [{ workspace_id: 'c1', company_name: 'Acme', role: 'admin' as const }],
  };
}

function makeTask(overrides: Record<string, unknown> = {}) {
  return {
    id: 'own1',
    company_id: 'c1',
    slug: 'my-task',
    title: 'My Task',
    runtime_image: 'vibony/sandbox-py:3.12',
    visibility: 'workspace',
    is_template: false,
    can_edit: true,
    created_by: 'u1',
    created_at: '2026-05-02T00:00:00Z',
    updated_at: '2026-05-02T00:00:00Z',
    description_md: '# Brief',
    starter_files: [{ path: 'train.py', content: 'pass\n' }],
    weakness: { blindspots: [{ issue: 'no optimizer state' }] },
    spec: { context: 'ml-infra' },
    ...overrides,
  };
}

function renderAt(path: string) {
  const router = createMemoryRouter(
    [
      { path: '/tasks/new', element: <TaskEditPage /> },
      { path: '/tasks/:id/edit', element: <TaskEditPage /> },
      { path: '/tasks', element: <p data-testid="tasks-page">Tasks</p> },
      { path: '/login', element: <p data-testid="login-page">Login</p> },
    ],
    { initialEntries: [path] },
  );
  return render(<RouterProvider router={router} />);
}

describe('TaskEditPage', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('create: parses YAML weakness/spec into objects in the POST body', async () => {
    let postBody: Record<string, unknown> | null = null;
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string, init?: RequestInit) => {
        if (url.includes('/auth/me')) {
          return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makeMe()) });
        }
        if (url === '/tasks' && init?.method === 'POST') {
          postBody = JSON.parse(init.body as string);
          return Promise.resolve({ ok: true, status: 201, json: () => Promise.resolve(makeTask()) });
        }
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
      }),
    );

    renderAt('/tasks/new');
    await waitFor(() => expect(screen.getByTestId('task-edit-form')).toBeInTheDocument());

    fireEvent.change(screen.getByTestId('slug-input'), { target: { value: 'new-task' } });
    fireEvent.change(screen.getByTestId('title-input'), { target: { value: 'New Task' } });
    fireEvent.change(screen.getByTestId('weakness-input'), {
      target: { value: 'blindspots:\n  - issue: trap one\nstyle:\n  confidence: high\n' },
    });
    fireEvent.change(screen.getByTestId('spec-input'), { target: { value: 'context: do the thing\n' } });

    await act(async () => {
      fireEvent.submit(screen.getByTestId('task-edit-form'));
    });

    await waitFor(() => expect(screen.getByTestId('tasks-page')).toBeInTheDocument());
    expect(postBody).not.toBeNull();
    expect(postBody!.slug).toBe('new-task');
    expect(postBody!.weakness).toEqual({ blindspots: [{ issue: 'trap one' }], style: { confidence: 'high' } });
    expect(postBody!.spec).toEqual({ context: 'do the thing' });
  });

  it('create: invalid YAML blocks submit and shows an error', async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url.includes('/auth/me')) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makeMe()) });
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
    });
    vi.stubGlobal('fetch', fetchMock);

    renderAt('/tasks/new');
    await waitFor(() => expect(screen.getByTestId('task-edit-form')).toBeInTheDocument());
    fireEvent.change(screen.getByTestId('slug-input'), { target: { value: 'x' } });
    fireEvent.change(screen.getByTestId('title-input'), { target: { value: 'X' } });
    // A YAML scalar (not a mapping) is rejected.
    fireEvent.change(screen.getByTestId('weakness-input'), { target: { value: 'just a string' } });

    await act(async () => {
      fireEvent.submit(screen.getByTestId('task-edit-form'));
    });

    expect(screen.getByTestId('task-edit-error')).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([u, o]) => u === '/tasks' && (o as RequestInit)?.method === 'POST')).toBe(false);
  });

  it('edit: loads a task and dumps weakness as YAML', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string) => {
        if (url.includes('/auth/me')) {
          return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makeMe()) });
        }
        if (url.includes('/tasks/own1')) {
          return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makeTask()) });
        }
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
      }),
    );

    renderAt('/tasks/own1/edit');
    await waitFor(() => expect(screen.getByTestId('title-input')).toBeInTheDocument());

    expect((screen.getByTestId('title-input') as HTMLInputElement).value).toBe('My Task');
    expect((screen.getByTestId('slug-input') as HTMLInputElement).disabled).toBe(true);
    const weakness = (screen.getByTestId('weakness-input') as HTMLTextAreaElement).value;
    expect(weakness).toContain('blindspots:');
    expect(weakness).toContain('no optimizer state');
  });

  it('edit: a read-only template shows a notice and disables save', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string) => {
        if (url.includes('/auth/me')) {
          return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(makeMe()) });
        }
        if (url.includes('/tasks/tmpl1')) {
          return Promise.resolve({
            ok: true,
            status: 200,
            json: () => Promise.resolve(makeTask({ id: 'tmpl1', is_template: true, can_edit: false, visibility: 'public' })),
          });
        }
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
      }),
    );

    renderAt('/tasks/tmpl1/edit');
    await waitFor(() => expect(screen.getByTestId('task-readonly-notice')).toBeInTheDocument());
    expect((screen.getByTestId('save-button') as HTMLButtonElement).disabled).toBe(true);
  });
});
