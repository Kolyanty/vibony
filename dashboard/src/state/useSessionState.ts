import { useContext } from 'react';
import { SessionContext, type SessionContextValue } from './sessionContextInstance';

export function useSessionState(): SessionContextValue {
  const ctx = useContext(SessionContext);
  if (ctx === null) {
    throw new Error('useSessionState must be used inside <SessionProvider>');
  }
  return ctx;
}
