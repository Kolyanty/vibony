import { useCallback, useEffect, useState } from 'react';
import type { MembershipInfo } from '../api/auth';

/*
 * Active-workspace selection. A user can belong to multiple workspaces;
 * the switcher in AppHeader picks the one currently in view. It's a pure client
 * concept: the backend already enforces per-membership access, and the
 * session/task list endpoints return data across all the caller's workspaces, so
 * scoping is a client-side filter. The choice persists in localStorage and is
 * reconciled against the live membership list (a stale id — e.g. after losing
 * access — falls back to the first membership).
 */
const WORKSPACE_KEY = 'vibony.workspace';

export function getStoredWorkspaceId(): string | null {
  try {
    return localStorage.getItem(WORKSPACE_KEY);
  } catch {
    return null;
  }
}

export function storeWorkspaceId(id: string): void {
  try {
    localStorage.setItem(WORKSPACE_KEY, id);
  } catch {
    // ignore (private mode / disabled storage) — falls back to in-memory state
  }
}

export interface ActiveWorkspace {
  activeId: string | null;
  active: MembershipInfo | null;
  setActiveId: (id: string) => void;
}

export function useActiveWorkspace(memberships: MembershipInfo[]): ActiveWorkspace {
  const [storedId, setStoredId] = useState<string | null>(() => getStoredWorkspaceId());

  // Resolve the effective active id *during render* (not in an effect), so the
  // first render after memberships arrive is already scoped — no cross-workspace
  // flash. A stored id that the user no longer belongs to falls back to the first
  // membership.
  const stillValid = storedId != null && memberships.some((m) => m.workspace_id === storedId);
  const activeId = memberships.length === 0 ? null : stillValid ? storedId : memberships[0]!.workspace_id;

  // Persist a corrected fallback so the stored value tracks the resolved one.
  useEffect(() => {
    if (activeId != null && activeId !== storedId) {
      setStoredId(activeId);
      storeWorkspaceId(activeId);
    }
  }, [activeId, storedId]);

  // Only accept an id the caller actually belongs to: a stray id (stale
  // deep-link, manual call) must never be written to localStorage, where it would
  // override the resolved fallback until the next reconcile.
  const setActiveId = useCallback(
    (id: string) => {
      if (!memberships.some((m) => m.workspace_id === id)) return;
      setStoredId(id);
      storeWorkspaceId(id);
    },
    [memberships],
  );

  const active = memberships.find((m) => m.workspace_id === activeId) ?? null;
  return { activeId, active, setActiveId };
}

/** Days remaining on a trial workspace, or null if no deadline / already lapsed. */
export function trialDaysLeft(trialEndsAt: string | null, now: Date = new Date()): number | null {
  if (!trialEndsAt) return null;
  const end = new Date(trialEndsAt).getTime();
  if (Number.isNaN(end)) return null;
  const ms = end - now.getTime();
  if (ms <= 0) return 0;
  return Math.ceil(ms / (24 * 60 * 60 * 1000));
}
