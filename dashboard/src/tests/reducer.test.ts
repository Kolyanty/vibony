import { describe, expect, it } from 'vitest';
import {
  initialSessionState,
  sessionReducer,
  type SessionState,
} from '../state/sessionState';
import type {
  EventEnvelope,
  HelloFrame,
  SessionConfig,
} from '../api/types';

function makeConfig(overrides: Partial<SessionConfig> = {}): SessionConfig {
  return {
    session_code: 'AAAA-BBBB',
    session_status: 'pending',
    protocol_version: 1,
    ws_token: 'AAAA-BBBB',
    task_id: 'task-01-training-loop',
    task_dir_name: 'training-loop',
    ai_config: {
      primary_model: 'deepseek-v4-flash',
      fallback_models: [],
    },
    bundle_sha256: null,
    created_at: '2026-05-14T18:00:00Z',
    expires_at: '2026-05-15T02:00:00Z',
    ...overrides,
  };
}

function userMessage(seq: number, text = 'hi'): EventEnvelope {
  return {
    id: `id-${seq}`,
    seq,
    session_code: 'AAAA-BBBB',
    timestamp: '2026-05-14T18:00:00.000Z',
    type: 'user_message',
    payload: { text },
  };
}

function sessionEnd(seq: number): EventEnvelope {
  return {
    id: `id-${seq}`,
    seq,
    session_code: 'AAAA-BBBB',
    timestamp: '2026-05-14T18:00:00.000Z',
    type: 'session_end',
    payload: { reason: 'interviewer' },
  };
}

describe('sessionReducer', () => {
  it('CONFIG_LOADED fills config, sessionStatus and protocolVersion', () => {
    const config = makeConfig({ session_status: 'active', protocol_version: 1 });
    const next = sessionReducer(initialSessionState, {
      type: 'CONFIG_LOADED',
      config,
    });
    expect(next.config).toBe(config);
    expect(next.sessionStatus).toBe('active');
    expect(next.protocolVersion).toBe(1);
    expect(next.connection).toBe('idle');
  });

  it('WS_HELLO sets connection=connected and updates the status', () => {
    const hello: HelloFrame = {
      kind: 'hello',
      protocol_version: 1,
      session_status: 'active',
      last_seq: 7,
    };
    const next = sessionReducer(initialSessionState, { type: 'WS_HELLO', hello });
    expect(next.connection).toBe('connected');
    expect(next.sessionStatus).toBe('active');
    expect(next.protocolVersion).toBe(1);
    // hello itself does not move lastSeq — the events that follow do
    // (see ws-contract.md §Reconnect: replay by seq arrives as separate event frames).
    expect(next.lastSeq).toBe(0);
  });

  it('WS_EVENT pushes the event and moves lastSeq', () => {
    const state = sessionReducer(initialSessionState, {
      type: 'WS_EVENT',
      event: userMessage(1, 'first'),
    });
    expect(state.lastSeq).toBe(1);
    expect(state.events).toHaveLength(1);
    expect(state.events[0]?.seq).toBe(1);

    const state2 = sessionReducer(state, { type: 'WS_EVENT', event: userMessage(2, 'second') });
    expect(state2.lastSeq).toBe(2);
    expect(state2.events).toHaveLength(2);
  });

  it('WS_EVENT drops an event with seq <= lastSeq (replay dedup)', () => {
    const state: SessionState = {
      ...initialSessionState,
      lastSeq: 5,
      events: [userMessage(5, 'fifth')],
    };
    const sameSeq = sessionReducer(state, { type: 'WS_EVENT', event: userMessage(5, 'dup') });
    expect(sameSeq).toBe(state);

    const older = sessionReducer(state, { type: 'WS_EVENT', event: userMessage(3, 'older') });
    expect(older).toBe(state);
  });

  it('sessionStarted false by default; true after session_start; not reset', () => {
    expect(initialSessionState.sessionStarted).toBe(false);

    const withStart = sessionReducer(initialSessionState, {
      type: 'WS_EVENT',
      event: {
        id: 'id-1',
        seq: 1,
        session_code: 'AAAA-BBBB',
        timestamp: '2026-05-14T18:00:00.000Z',
        type: 'session_start',
        payload: { task_id: 'task-01', ai_config: { model: 'claude-3-5-sonnet' }, protocol_version: 6, cli_version: '0.0.0' },
      },
    });
    expect(withStart.sessionStarted).toBe(true);

    // Must stay true after subsequent events.
    const withMsg = sessionReducer(withStart, { type: 'WS_EVENT', event: userMessage(2) });
    expect(withMsg.sessionStarted).toBe(true);
  });

  it('WS_EVENT session_end switches sessionStatus to completed', () => {
    const start = sessionReducer(initialSessionState, {
      type: 'CONFIG_LOADED',
      config: makeConfig({ session_status: 'active' }),
    });
    const ended = sessionReducer(start, { type: 'WS_EVENT', event: sessionEnd(10) });
    expect(ended.sessionStatus).toBe('completed');
    expect(ended.lastSeq).toBe(10);
    expect(ended.events).toHaveLength(1);
  });

  it('WS_STATE_CHANGED updates only connection', () => {
    const base = sessionReducer(initialSessionState, {
      type: 'WS_EVENT',
      event: userMessage(1),
    });
    const reconnecting = sessionReducer(base, {
      type: 'WS_STATE_CHANGED',
      connection: 'reconnecting',
    });
    expect(reconnecting.connection).toBe('reconnecting');
    expect(reconnecting.events).toEqual(base.events);
    expect(reconnecting.lastSeq).toBe(base.lastSeq);
  });

  it('WS_FATAL transitions to closed and records the reason', () => {
    const next = sessionReducer(initialSessionState, {
      type: 'WS_FATAL',
      reason: 'protocol_mismatch',
      message: 'update the CLI',
    });
    expect(next.connection).toBe('closed');
    expect(next.fatal).toEqual({ reason: 'protocol_mismatch', message: 'update the CLI' });
  });
});
