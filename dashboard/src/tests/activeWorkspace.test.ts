import { act, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { useActiveWorkspace, trialDaysLeft } from '../state/activeWorkspace';
import type { MembershipInfo } from '../api/auth';

function m(id: string, role: 'admin' | 'interviewer' = 'admin'): MembershipInfo {
  return { workspace_id: id, company_name: id.toUpperCase(), role, plan: 'trial', trial_ends_at: null };
}

describe('trialDaysLeft', () => {
  const now = new Date('2026-06-02T00:00:00Z');

  it('returns null when no deadline', () => {
    expect(trialDaysLeft(null, now)).toBeNull();
  });

  it('rounds up remaining days', () => {
    expect(trialDaysLeft('2026-06-05T00:00:00Z', now)).toBe(3);
    expect(trialDaysLeft('2026-06-02T06:00:00Z', now)).toBe(1);
  });

  it('returns 0 for a lapsed trial', () => {
    expect(trialDaysLeft('2026-06-01T00:00:00Z', now)).toBe(0);
  });
});

describe('useActiveWorkspace', () => {
  afterEach(() => localStorage.clear());

  it('falls back to the first membership when none stored', () => {
    const { result } = renderHook(() => useActiveWorkspace([m('c1'), m('c2')]));
    expect(result.current.activeId).toBe('c1');
    expect(result.current.active?.workspace_id).toBe('c1');
  });

  it('honors a stored id that is still a membership', () => {
    localStorage.setItem('vibony.workspace', 'c2');
    const { result } = renderHook(() => useActiveWorkspace([m('c1'), m('c2')]));
    expect(result.current.activeId).toBe('c2');
  });

  it('snaps a stale stored id back to the first membership', () => {
    localStorage.setItem('vibony.workspace', 'gone');
    const { result } = renderHook(() => useActiveWorkspace([m('c1'), m('c2')]));
    expect(result.current.activeId).toBe('c1');
  });

  it('setActiveId persists the choice', () => {
    const { result } = renderHook(() => useActiveWorkspace([m('c1'), m('c2')]));
    act(() => result.current.setActiveId('c2'));
    expect(result.current.activeId).toBe('c2');
    expect(localStorage.getItem('vibony.workspace')).toBe('c2');
  });

  it('setActiveId ignores an id the user does not belong to (D-8)', () => {
    const { result } = renderHook(() => useActiveWorkspace([m('c1'), m('c2')]));
    act(() => result.current.setActiveId('intruder'));
    expect(result.current.activeId).toBe('c1');
    expect(localStorage.getItem('vibony.workspace')).not.toBe('intruder');
  });
});
