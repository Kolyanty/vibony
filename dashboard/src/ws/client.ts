import type { ConnectionState, FatalReason } from '../state/sessionState';
import type { Attendee, EventEnvelope, ErrorFrame, HelloFrame, ServerFrame } from '../api/types';

const TERMINAL_CODES = new Map<number, FatalReason>([
  [4401, 'unauthorized'],
  [4403, 'forbidden'],
  [4404, 'not_found'],
  [4409, 'session_ended'],
  [4426, 'protocol_mismatch'],
]);

// Backoff delays in ms: 1s, 2s, 4s, 8s, 16s, 30s cap — symmetric with CLI WSEventSink.
const BACKOFF_MS = [1000, 2000, 4000, 8000, 16000, 30000];

export interface InterviewerSocketOptions {
  onHello: (frame: HelloFrame) => void;
  onEvent: (event: EventEnvelope) => void;
  onError: (frame: ErrorFrame) => void;
  onStateChange: (state: ConnectionState) => void;
  onFatal: (reason: FatalReason, message: string) => void;
  // presence
  onPresenceUpdated?: (attendees: Attendee[]) => void;
  onPresenceAlert?: (reason: string) => void;
}

export class InterviewerSocket {
  private ws: WebSocket | null = null;
  private _state: ConnectionState = 'idle';
  private lastSeq = 0;
  private attempt = 0;
  private stopped = false;
  private timer: ReturnType<typeof setTimeout> | null = null;

  constructor(
    private readonly buildUrl: (lastSeq: number) => string,
    private readonly opts: InterviewerSocketOptions,
  ) {}

  start(): void {
    this._connect();
  }

  stop(): void {
    this.stopped = true;
    if (this.timer !== null) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    if (this.ws !== null) {
      this.ws.onclose = null;
      this.ws.close(1000);
      this.ws = null;
    }
    this._setState('closed');
  }

  private _setState(next: ConnectionState): void {
    if (this._state !== next) {
      this._state = next;
      this.opts.onStateChange(next);
    }
  }

  private _connect(): void {
    if (this.stopped) return;
    if (this.attempt === 0) this._setState('connecting');
    const url = this.buildUrl(this.lastSeq);
    const ws = new WebSocket(url);
    this.ws = ws;

    ws.onmessage = (evt: MessageEvent) => {
      let frame: ServerFrame;
      try {
        frame = JSON.parse(evt.data as string) as ServerFrame;
      } catch {
        return;
      }
      this._handleFrame(frame);
    };

    ws.onerror = () => {
      // Close event follows error — handled in onclose.
    };

    ws.onclose = (evt: CloseEvent) => {
      if (this.stopped) return;
      this.ws = null;
      const fatalReason = TERMINAL_CODES.get(evt.code);
      if (fatalReason !== undefined) {
        this._setState('closed');
        this.opts.onFatal(fatalReason, evt.reason);
        return;
      }
      this._scheduleReconnect();
    };
  }

  private _handleFrame(frame: ServerFrame): void {
    switch (frame.kind) {
      case 'hello':
        this.attempt = 0;
        this._setState('connected');
        this.opts.onHello(frame);
        break;
      case 'event':
        if (frame.event.seq > this.lastSeq) {
          this.lastSeq = frame.event.seq;
        }
        this.opts.onEvent(frame.event);
        break;
      case 'error':
        this.opts.onError(frame);
        break;
      case 'presence_updated':
        this.opts.onPresenceUpdated?.(frame.attendees);
        break;
      case 'presence_alert':
        this.opts.onPresenceAlert?.(frame.reason);
        break;
      default:
        // Unknown kind — ignore (forward compatibility).
        break;
    }
  }

  private _scheduleReconnect(): void {
    const delay = BACKOFF_MS[Math.min(this.attempt, BACKOFF_MS.length - 1)]!;
    this.attempt++;
    this._setState('reconnecting');
    this.timer = setTimeout(() => {
      this.timer = null;
      this._connect();
    }, delay);
  }
}

export function makeInterviewerWsUrl(code: string): (lastSeq: number) => string {
  return (lastSeq: number) => {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${proto}//${window.location.host}/sessions/${code}/interviewer?last_seq=${lastSeq}`;
  };
}
