import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { InterviewerSocket } from '../ws/client';
import type { ConnectionState, FatalReason } from '../state/sessionState';
import type { EventEnvelope, ErrorFrame, HelloFrame } from '../api/types';

// ---- Mock WebSocket --------------------------------------------------------

class MockWebSocket {
  static instances: MockWebSocket[] = [];

  readyState = 0; // CONNECTING
  readonly url: string;

  onopen: ((e: Event) => void) | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;
  onerror: ((e: Event) => void) | null = null;
  onclose: ((e: CloseEvent) => void) | null = null;

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }

  close() {
    this.readyState = 3;
    // Intentionally does not trigger onclose — stop() nullifies it before close().
  }

  simulateOpen() {
    this.readyState = 1;
    this.onopen?.(new Event('open'));
  }

  simulateMessage(data: object) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) }));
  }

  simulateClose(code = 1000, reason = '') {
    this.readyState = 3;
    this.onclose?.(new CloseEvent('close', { code, reason, wasClean: code === 1000 }));
  }
}

// ---- Helpers ---------------------------------------------------------------

interface Captured {
  hello: HelloFrame[];
  events: EventEnvelope[];
  errors: ErrorFrame[];
  states: ConnectionState[];
  fatals: Array<[FatalReason, string]>;
}

function makeSocket(): { socket: InterviewerSocket; cap: Captured } {
  const cap: Captured = { hello: [], events: [], errors: [], states: [], fatals: [] };
  const socket = new InterviewerSocket(
    (lastSeq) => `ws://localhost/sessions/AAAA-BBBB/interviewer?last_seq=${lastSeq}`,
    {
      onHello: (f) => cap.hello.push(f),
      onEvent: (e) => cap.events.push(e),
      onError: (e) => cap.errors.push(e),
      onStateChange: (s) => cap.states.push(s),
      onFatal: (r, m) => cap.fatals.push([r, m]),
    },
  );
  return { socket, cap };
}

function ws0(): MockWebSocket {
  return MockWebSocket.instances[0]!;
}

// ---- Tests -----------------------------------------------------------------

describe('InterviewerSocket', () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    vi.stubGlobal('WebSocket', MockWebSocket);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('start() creates a WS connection with last_seq=0, moves to connecting', () => {
    const { socket, cap } = makeSocket();
    socket.start();
    expect(cap.states).toContain('connecting');
    expect(MockWebSocket.instances).toHaveLength(1);
    expect(ws0().url).toContain('last_seq=0');
  });

  it('hello frame → onHello is called, state → connected', () => {
    const { socket, cap } = makeSocket();
    socket.start();
    const hello: HelloFrame = { kind: 'hello', protocol_version: 1, session_status: 'active', last_seq: 0 };
    ws0().simulateMessage(hello);
    expect(cap.hello).toHaveLength(1);
    expect(cap.hello[0]).toEqual(hello);
    expect(cap.states.at(-1)).toBe('connected');
  });

  it('event frame → onEvent is called, lastSeq is updated', () => {
    const { socket, cap } = makeSocket();
    socket.start();
    ws0().simulateMessage({ kind: 'hello', protocol_version: 1, session_status: 'active', last_seq: 0 });
    ws0().simulateMessage({
      kind: 'event',
      event: {
        id: 'e1', seq: 5, session_code: 'AAAA-BBBB',
        timestamp: '2026-05-15T10:00:00Z', type: 'user_message', payload: { text: 'hi' },
      },
    });
    expect(cap.events).toHaveLength(1);
    expect(cap.events[0]?.seq).toBe(5);
  });

  it('error frame → onError is called', () => {
    const { socket, cap } = makeSocket();
    socket.start();
    ws0().simulateMessage({ kind: 'error', code: 'PAYLOAD_INVALID', message: 'bad payload' });
    expect(cap.errors).toHaveLength(1);
    expect(cap.errors[0]?.code).toBe('PAYLOAD_INVALID');
  });

  it('unknown kind → ignored', () => {
    const { socket, cap } = makeSocket();
    socket.start();
    ws0().simulateMessage({ kind: 'future_frame', data: 'whatever' });
    expect(cap.hello).toHaveLength(0);
    expect(cap.events).toHaveLength(0);
    expect(cap.errors).toHaveLength(0);
  });

  it('non-terminal close → moves to reconnecting, creates a new connection after 1s', () => {
    vi.useFakeTimers();
    const { socket, cap } = makeSocket();
    socket.start();
    ws0().simulateClose(1011, 'internal error');
    expect(cap.states.at(-1)).toBe('reconnecting');
    expect(MockWebSocket.instances).toHaveLength(1);
    vi.advanceTimersByTime(1000);
    expect(MockWebSocket.instances).toHaveLength(2);
  });

  it('backoff doubles on each failure: 1s → 2s', () => {
    vi.useFakeTimers();
    const { socket } = makeSocket();
    socket.start();
    ws0().simulateClose(1011);
    vi.advanceTimersByTime(1000);
    expect(MockWebSocket.instances).toHaveLength(2);
    MockWebSocket.instances[1]!.simulateClose(1011);
    vi.advanceTimersByTime(1999);
    expect(MockWebSocket.instances).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(MockWebSocket.instances).toHaveLength(3);
  });

  it('backoff resets after a successful hello', () => {
    vi.useFakeTimers();
    const { socket } = makeSocket();
    socket.start();
    ws0().simulateClose(1011);
    vi.advanceTimersByTime(1000); // reconnect attempt 1
    MockWebSocket.instances[1]!.simulateMessage({
      kind: 'hello', protocol_version: 1, session_status: 'active', last_seq: 0,
    });
    MockWebSocket.instances[1]!.simulateClose(1011);
    vi.advanceTimersByTime(1000); // should be back to 1s
    expect(MockWebSocket.instances).toHaveLength(3);
  });

  it('replay: on reconnect passes the last_seq of the last handled event', () => {
    vi.useFakeTimers();
    const { socket } = makeSocket();
    socket.start();
    ws0().simulateMessage({ kind: 'hello', protocol_version: 1, session_status: 'active', last_seq: 0 });
    ws0().simulateMessage({
      kind: 'event',
      event: { id: 'e1', seq: 7, session_code: 'AAAA-BBBB', timestamp: '...', type: 'user_message', payload: { text: 'x' } },
    });
    ws0().simulateClose(1011);
    vi.advanceTimersByTime(1000);
    expect(MockWebSocket.instances[1]?.url).toContain('last_seq=7');
  });

  it('close 4401 → no reconnect, onFatal(unauthorized)', () => {
    const { socket, cap } = makeSocket();
    socket.start();
    ws0().simulateClose(4401, 'Unauthorized');
    expect(cap.states.at(-1)).toBe('closed');
    expect(cap.fatals).toHaveLength(1);
    expect(cap.fatals[0]![0]).toBe('unauthorized');
    expect(MockWebSocket.instances).toHaveLength(1);
  });

  it('close 4403 → no reconnect, onFatal(forbidden)', () => {
    const { socket, cap } = makeSocket();
    socket.start();
    ws0().simulateClose(4403, 'Forbidden');
    expect(cap.states.at(-1)).toBe('closed');
    expect(cap.fatals).toHaveLength(1);
    expect(cap.fatals[0]![0]).toBe('forbidden');
    expect(MockWebSocket.instances).toHaveLength(1);
  });

  it('close 4404 → no reconnect, onFatal(not_found)', () => {
    const { socket, cap } = makeSocket();
    socket.start();
    ws0().simulateClose(4404);
    expect(cap.fatals[0]![0]).toBe('not_found');
    expect(MockWebSocket.instances).toHaveLength(1);
  });

  it('close 4409 → no reconnect, onFatal(session_ended)', () => {
    const { socket, cap } = makeSocket();
    socket.start();
    ws0().simulateClose(4409);
    expect(cap.fatals[0]![0]).toBe('session_ended');
    expect(MockWebSocket.instances).toHaveLength(1);
  });

  it('close 4426 → no reconnect, onFatal(protocol_mismatch)', () => {
    const { socket, cap } = makeSocket();
    socket.start();
    ws0().simulateClose(4426);
    expect(cap.fatals[0]![0]).toBe('protocol_mismatch');
    expect(MockWebSocket.instances).toHaveLength(1);
  });

  it('stop() → state closed, onclose nullified, reconnect does not start', () => {
    vi.useFakeTimers();
    const { socket, cap } = makeSocket();
    socket.start();
    socket.stop();
    expect(cap.states.at(-1)).toBe('closed');
    // Simulate close after stop — onclose nullified, the guard kicks in
    ws0().simulateClose(1011);
    vi.advanceTimersByTime(10000);
    expect(MockWebSocket.instances).toHaveLength(1);
  });

  it('stop() during the backoff timer cancels reconnect', () => {
    vi.useFakeTimers();
    const { socket, cap } = makeSocket();
    socket.start();
    ws0().simulateClose(1011);
    expect(cap.states.at(-1)).toBe('reconnecting');
    socket.stop();
    vi.advanceTimersByTime(5000);
    expect(MockWebSocket.instances).toHaveLength(1);
  });
});
