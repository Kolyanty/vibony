import { useContext } from 'react';
import { TimeNowContext } from './timeNowContext';

/**
 * Returns the "current time" in epoch ms. If a `TimeNowProvider` is mounted
 * higher in the tree, returns the shared tick (updated once every 30s). Outside
 * a provider — a `Date.now()` snapshot at render time (no live updates), so that
 * simple component unit tests don't need a wrapper.
 */
export function useNow(): number {
  const v = useContext(TimeNowContext);
  return v ?? Date.now();
}
