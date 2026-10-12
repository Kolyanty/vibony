// Provider through which SessionPage shares state/dispatch with nested
// components (DialogTab, then FilesTab/ConsoleTab).
//
// The context and hook live in sibling files so react-refresh doesn't complain
// (`only-export-components` requires that a file with a component export
// nothing but components).

import { useReducer, type ReactNode } from 'react';
import { initialSessionState, sessionReducer } from './sessionState';
import { SessionContext } from './sessionContextInstance';

export function SessionProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(sessionReducer, initialSessionState);
  return (
    <SessionContext.Provider value={{ state, dispatch }}>{children}</SessionContext.Provider>
  );
}
