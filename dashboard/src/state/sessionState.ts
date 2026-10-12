// SessionPage state reducer. Pure function: the WS connection, REST and timers
// live outside (in SessionPage / InterviewerSocket) and feed the reducer actions.
//
// The WS logic and hello/event/error frame handling live elsewhere; the actions
// WS_HELLO/WS_EVENT/WS_STATE_CHANGED/WS_FATAL and dedup by seq follow
// docs/protocol/ws-contract.md §Reconnect.

import type {
  EventEnvelope,
  HelloFrame,
  SessionConfig,
  SessionStatus,
} from '../api/types';

export type ConnectionState = 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'closed';

export type FatalReason =
  | 'unauthorized'
  | 'forbidden'
  | 'not_found'
  | 'protocol_mismatch'
  | 'session_ended';

export interface SessionState {
  config: SessionConfig | null;
  sessionStatus: SessionStatus | null;
  protocolVersion: number | null;
  connection: ConnectionState;
  lastSeq: number;
  events: EventEnvelope[];
  fatal: { reason: FatalReason; message: string } | null;
  /** Set to true the first time a session_start event is processed. O(1). */
  sessionStarted: boolean;
}

export const initialSessionState: SessionState = {
  config: null,
  sessionStatus: null,
  protocolVersion: null,
  connection: 'idle',
  lastSeq: 0,
  events: [],
  fatal: null,
  sessionStarted: false,
};

export type SessionAction =
  | { type: 'CONFIG_LOADED'; config: SessionConfig }
  | { type: 'WS_HELLO'; hello: HelloFrame }
  | { type: 'WS_EVENT'; event: EventEnvelope }
  | { type: 'WS_STATE_CHANGED'; connection: ConnectionState }
  | { type: 'WS_FATAL'; reason: FatalReason; message: string };

export function sessionReducer(state: SessionState, action: SessionAction): SessionState {
  switch (action.type) {
    case 'CONFIG_LOADED':
      return {
        ...state,
        config: action.config,
        sessionStatus: action.config.session_status,
        protocolVersion: action.config.protocol_version,
      };

    case 'WS_HELLO':
      return {
        ...state,
        sessionStatus: action.hello.session_status,
        protocolVersion: action.hello.protocol_version,
        connection: 'connected',
      };

    case 'WS_EVENT': {
      // Dedup by seq — after a reconnect the backend replays events > last_seq,
      // but already-seen ones can slip through between request and delivery. Guard.
      if (action.event.seq <= state.lastSeq) {
        return state;
      }
      const sessionStatus = action.event.type === 'session_end' ? 'completed' : state.sessionStatus;
      const sessionStarted = state.sessionStarted || action.event.type === 'session_start';
      return {
        ...state,
        lastSeq: action.event.seq,
        events: [...state.events, action.event],
        sessionStatus,
        sessionStarted,
      };
    }

    case 'WS_STATE_CHANGED':
      return { ...state, connection: action.connection };

    case 'WS_FATAL':
      return {
        ...state,
        connection: 'closed',
        fatal: { reason: action.reason, message: action.message },
      };

    default: {
      const _exhaustive: never = action;
      void _exhaustive;
      return state;
    }
  }
}
