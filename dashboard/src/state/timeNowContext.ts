import { createContext } from 'react';

/**
 * Global "current tick" in epoch ms. The provider sets the value and updates
 * it every N ms; components read it via `useNow` and re-render.
 *
 * `null` means "the provider is not mounted" — `useNow` then returns
 * `Date.now()` as a snapshot at render time. Handy so that simple tests
 * rendering `MessageRow` directly don't need a wrapper.
 */
export const TimeNowContext = createContext<number | null>(null);
