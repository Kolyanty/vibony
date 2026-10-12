import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CandidateSocket, makeCandidateWsUrl } from '../ws/candidateClient';
import type { ConnectionState, FatalReason } from '../state/sessionState';
import type { EventEnvelope, ErrorFrame, HelloFrame } from '../api/types';

// ---- Mock WebSocket --------------------------------------------------------

class MockWebSocket {
  static instances: MockWebSocket[] = [];

  readyState = 0; // CONNECTING
  readonly url: string;
  sent: string[] = [];

  onopen: ((e: Event) => void) | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;
  onerror: ((e: Event) => void) | null = null;
  onclose: ((e: CloseEvent) => void) | null = null;

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }

  send(data: string) {
    this.sent.push(data);
  }

  close() {
    this.readyState = 3;
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
  ticks: number[];
}

function makeSocket(): { socket: CandidateSocket; cap: Captured } {
  const cap: Captured = { hello: [], events: [], errors: [], states: [], fatals: [], ticks: [] };
  const socket = new CandidateSocket(
    (lastSeq) =>
      `ws://localhost/sessions/candidate-web?token=test-token&last_seq=${lastSeq}`,
    {
      onHello: (f) => cap.hello.push(f),
      onEvent: (e) => cap.events.push(e),
      onError: (e) => cap.errors.push(e),
      onStateChange: (s) => cap.states.push(s),
      onFatal: (r, m) => cap.fatals.push([r, m]),
      onTimerTick: (r) => cap.ticks.push(r),
    },
  );
  return { socket, cap };
}

function ws0(): MockWebSocket {
  return MockWebSocket.instances[0]!;
}

// ---- Tests -----------------------------------------------------------------

describe('CandidateSocket', () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    vi.stubGlobal('WebSocket', MockWebSocket);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('reconnect передаёт last_seq последнего полученного события', () => {
    vi.useFakeTimers();
    const { socket } = makeSocket();
    socket.start();
    ws0().simulateMessage({
      kind: 'hello',
      protocol_version: 5,
      session_status: 'active',
      last_seq: 0,
    });
    ws0().simulateMessage({
      kind: 'event',
      event: {
        id: 'e1',
        seq: 3,
        session_code: 'AAAA-BBBB',
        timestamp: '2026-05-21T10:00:00Z',
        type: 'user_message',
        payload: { text: 'hi' },
      },
    });
    ws0().simulateClose(1011);
    vi.advanceTimersByTime(1000);
    expect(MockWebSocket.instances[1]?.url).toContain('last_seq=3');
  });

  it('входящие события доставляются через onEvent', () => {
    const { socket, cap } = makeSocket();
    socket.start();
    ws0().simulateMessage({
      kind: 'hello',
      protocol_version: 5,
      session_status: 'active',
      last_seq: 0,
    });
    ws0().simulateMessage({
      kind: 'event',
      event: {
        id: 'e1',
        seq: 5,
        session_code: 'AAAA',
        timestamp: '2026-05-21T10:01:00Z',
        type: 'assistant_message',
        payload: { text: 'ok' },
      },
    });
    expect(cap.events).toHaveLength(1);
    expect(cap.events[0]?.seq).toBe(5);
  });

  it('sendUserMessage сериализует {kind: user_message, text} и отправляет через WS', () => {
    const { socket } = makeSocket();
    socket.start();
    ws0().readyState = 1; // OPEN
    ws0().simulateMessage({
      kind: 'hello',
      protocol_version: 5,
      session_status: 'active',
      last_seq: 0,
    });
    socket.sendUserMessage('hello world');
    expect(ws0().sent).toHaveLength(1);
    expect(JSON.parse(ws0().sent[0]!)).toEqual({ kind: 'user_message', text: 'hello world' });
  });

  it('timer_tick фрейм вызывает onTimerTick с remaining_seconds', () => {
    const { socket, cap } = makeSocket();
    socket.start();
    ws0().readyState = 1;
    ws0().simulateMessage({ kind: 'hello', protocol_version: 7, session_status: 'active', last_seq: 0 });
    ws0().simulateMessage({ kind: 'timer_tick', remaining_seconds: 1840 });
    ws0().simulateMessage({ kind: 'timer_tick', remaining_seconds: 1830 });
    expect(cap.ticks).toEqual([1840, 1830]);
    // timer_tick is not counted as an event — the log doesn't grow.
    expect(cap.events).toHaveLength(0);
  });

  it('sendSessionSubmit сериализует {kind: session_submit}', () => {
    const { socket } = makeSocket();
    socket.start();
    ws0().readyState = 1;
    ws0().simulateMessage({ kind: 'hello', protocol_version: 7, session_status: 'active', last_seq: 0 });
    socket.sendSessionSubmit();
    expect(ws0().sent).toHaveLength(1);
    expect(JSON.parse(ws0().sent[0]!)).toEqual({ kind: 'session_submit' });
  });
});

describe('makeCandidateWsUrl', () => {
  it('строит URL с token и last_seq', () => {
    const buildUrl = makeCandidateWsUrl('my-secret-token');
    const url = buildUrl(7);
    expect(url).toContain('/sessions/candidate-web');
    expect(url).toContain('token=my-secret-token');
    expect(url).toContain('last_seq=7');
  });
});
