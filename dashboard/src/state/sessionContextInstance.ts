import { createContext, type Dispatch } from 'react';
import type { SessionAction, SessionState } from './sessionState';

export interface SessionContextValue {
  state: SessionState;
  dispatch: Dispatch<SessionAction>;
}

export const SessionContext = createContext<SessionContextValue | null>(null);
