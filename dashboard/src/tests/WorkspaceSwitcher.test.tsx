import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { WorkspaceSwitcher } from '../components/WorkspaceSwitcher';
import { CreateWorkspaceDialog } from '../components/CreateWorkspaceDialog';
import type { MembershipInfo } from '../api/auth';

function m(id: string, extra: Partial<MembershipInfo> = {}): MembershipInfo {
  return {
    workspace_id: id,
    company_name: id.toUpperCase(),
    role: 'admin',
    plan: 'trial',
    trial_ends_at: null,
    ...extra,
  };
}

describe('WorkspaceSwitcher', () => {
  it('shows the active workspace name in the trigger', () => {
    render(
      <WorkspaceSwitcher
        memberships={[m('c1'), m('c2')]}
        active={m('c2')}
        onSelect={() => {}}
        onCreate={() => {}}
      />,
    );
    expect(screen.getByTestId('workspace-switcher-trigger')).toHaveTextContent('C2');
  });

  it('renders a trial badge with days left', () => {
    render(
      <WorkspaceSwitcher
        memberships={[m('c1', { trial_ends_at: '2999-01-01T00:00:00Z' })]}
        active={m('c1', { trial_ends_at: '2999-01-01T00:00:00Z' })}
        onSelect={() => {}}
        onCreate={() => {}}
      />,
    );
    expect(screen.getByTestId('workspace-plan-badge')).toHaveTextContent(/Trial/);
  });

  it('renders nothing without an active workspace', () => {
    const { container } = render(
      <WorkspaceSwitcher memberships={[]} active={null} onSelect={() => {}} onCreate={() => {}} />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});

describe('CreateWorkspaceDialog', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('posts the name and returns the new membership', async () => {
    const created = vi.fn();
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string, opts?: RequestInit) => {
        if (url.includes('/workspaces') && opts?.method === 'POST') {
          return Promise.resolve({
            ok: true,
            status: 201,
            json: () =>
              Promise.resolve({
                workspace_id: 'new1',
                company_name: 'New Co',
                role: 'admin',
                plan: 'trial',
                trial_ends_at: '2999-01-01T00:00:00Z',
              }),
          });
        }
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
      }),
    );

    render(<CreateWorkspaceDialog open onOpenChange={() => {}} onCreated={created} />);
    fireEvent.change(screen.getByTestId('workspace-name-input'), { target: { value: 'New Co' } });
    fireEvent.submit(screen.getByTestId('create-workspace-form'));

    await waitFor(() => expect(created).toHaveBeenCalled());
    expect(created.mock.calls[0]![0].workspace_id).toBe('new1');
  });
});
