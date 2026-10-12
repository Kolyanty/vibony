import { useEffect, useRef, useState, type ReactNode } from 'react';
import { TimeNowContext } from './timeNowContext';

interface TimeNowProviderProps {
  /**
   * If not null, `useNow` always returns this value and the live tick is
   * disabled. Applied to a completed session so relative time stops
   * drifting after it ends.
   */
  frozenAt: number | null;
  children: ReactNode;
  /** Live-tick interval in ms. Defaults to 30000. A test parameter. */
  intervalMs?: number;
}

export function TimeNowProvider({
  frozenAt,
  children,
  intervalMs = 30_000,
}: TimeNowProviderProps) {
  const [now, setNow] = useState<number>(() => frozenAt ?? Date.now());
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    if (intervalRef.current !== null) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
    if (frozenAt !== null) {
      setNow(frozenAt);
      return;
    }
    setNow(Date.now());
    intervalRef.current = setInterval(() => {
      setNow(Date.now());
    }, intervalMs);
    return () => {
      if (intervalRef.current !== null) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    };
  }, [frozenAt, intervalMs]);

  return <TimeNowContext.Provider value={now}>{children}</TimeNowContext.Provider>;
}
