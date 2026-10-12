import type { ConnectionState, FatalReason } from '../state/sessionState';
import type {
  Attendee,
  BashConfirmRequestFrame,
  EventEnvelope,
  ErrorFrame,
  FsResponseFrame,
  HelloFrame,
  ServerFrame,
} from '../api/types';

const TERMINAL_CODES = new Map<number, FatalReason>([
  [4401, 'unauthorized'],
  [4403, 'forbidden'],
  [4404, 'not_found'],
  [4409, 'session_ended'],
  [4426, 'protocol_mismatch'],
]);

// Backoff delays in ms: 1s, 2s, 4s, 8s, 16s, 30s cap — symmetric with InterviewerSocket.
const BACKOFF_MS = [1000, 2000, 4000, 8000, 16000, 30000];

export interface CandidateSocketOptions {
  onHello: (frame: HelloFrame) => void;
  onEvent: (event: EventEnvelope) => void;
  onError: (frame: ErrorFrame) => void;
  onStateChange: (state: ConnectionState) => void;
  onFatal: (reason: FatalReason, message: string) => void;
  onBashConfirmRequest?: (frame: BashConfirmRequestFrame) => void;
  // Presence
  joinName?: string | null;
  onSessionJoinOk?: () => void;
  onSessionJoinError?: (reason: string) => void;
  onPresenceUpdated?: (attendees: Attendee[]) => void;
  // Async countdown
  onTimerTick?: (remainingSeconds: number) => void;
  // Agent-working indicator (advisory; never persisted)
  onAgentStatus?: (state: 'working' | 'idle') => void;
}

const FS_REQUEST_TIMEOUT_MS = 15_000;

export class CandidateSocket {
  private ws: WebSocket | null = null;
  private _state: ConnectionState = 'idle';
  private lastSeq = 0;
  private attempt = 0;
  private stopped = false;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private fsRequests = new Map<string, (r: FsResponseFrame) => void>();
  // Stored join name for auto-resend on reconnect
  private joinName: string | null;

  constructor(
    private readonly buildUrl: (lastSeq: number) => string,
    private readonly opts: CandidateSocketOptions,
  ) {
    this.joinName = opts.joinName ?? null;
  }

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
    this._rejectFsRequests('connection closed');
  }

  sendUserMessage(text: string): void {
    this._sendFrame({ kind: 'user_message', text });
  }

  sendInlineBash(cmd: string): void {
    this._sendFrame({ kind: 'inline_bash', cmd });
  }

  sendBashConfirmResponse(cmd_id: string, choice: string): void {
    this._sendFrame({ kind: 'bash_confirm_response', cmd_id, choice });
  }

  sendSessionJoin(name: string): void {
    this.joinName = name;
    this._sendFrame({ kind: 'session_join', name });
  }

  // Candidate ends an async session early. Backend replies with
  // NOT_ASYNC on a live session (ignored) and broadcasts session_end otherwise.
  sendSessionSubmit(): void {
    this._sendFrame({ kind: 'session_submit' });
  }

  setJoinName(name: string): void {
    this.joinName = name;
  }

  sendFsRequest(op: string, path?: string, content?: string): Promise<FsResponseFrame> {
    // Fail fast when WS is not open — avoids a 15-second timeout stall during
    // reconnection. Callers that gate on state.connection === 'connected' won't
    // hit this, but handleSelectFile can be triggered by a user click at any time.
    if (this.ws === null || this.ws.readyState !== 1) {
      return Promise.resolve({ kind: 'fs_response', id: '', error: 'not_connected' });
    }
    return new Promise((resolve) => {
      const id = crypto.randomUUID();
      const timer = setTimeout(() => {
        if (this.fsRequests.delete(id)) {
          resolve({ kind: 'fs_response', id, error: 'timeout' });
        }
      }, FS_REQUEST_TIMEOUT_MS);
      this.fsRequests.set(id, (response) => {
        clearTimeout(timer);
        resolve(response);
      });
      this._sendFrame({
        kind: 'fs_request',
        id,
        op,
        ...(path !== undefined && { path }),
        ...(content !== undefined && { content }),
      });
    });
  }

  private _rejectFsRequests(reason: string): void {
    for (const [id, cb] of this.fsRequests) {
      cb({ kind: 'fs_response', id, error: reason });
    }
    this.fsRequests.clear();
  }

  private _sendFrame(frame: unknown): void {
    // 1 === WebSocket.OPEN
    if (this.ws !== null && this.ws.readyState === 1) {
      this.ws.send(JSON.stringify(frame));
    }
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
        this._rejectFsRequests('connection closed');
        this.opts.onFatal(fatalReason, evt.reason);
        return;
      }
      this._rejectFsRequests('reconnecting');
      this._scheduleReconnect();
    };
  }

  private _handleFrame(frame: ServerFrame): void {
    switch (frame.kind) {
      case 'hello':
        this.attempt = 0;
        this._setState('connected');
        this.opts.onHello(frame);
        // Auto-send session_join after hello (reconnect or initial join).
        if (this.joinName !== null) {
          this._sendFrame({ kind: 'session_join', name: this.joinName });
        }
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
      case 'fs_response': {
        const cb = this.fsRequests.get(frame.id);
        if (cb) {
          this.fsRequests.delete(frame.id);
          cb(frame);
        }
        break;
      }
      case 'bash_confirm_request':
        this.opts.onBashConfirmRequest?.(frame);
        break;
      case 'session_join_ok':
        this.opts.onSessionJoinOk?.();
        break;
      case 'session_join_error':
        this.opts.onSessionJoinError?.(frame.reason);
        break;
      case 'presence_updated':
        this.opts.onPresenceUpdated?.(frame.attendees);
        break;
      case 'timer_tick':
        this.opts.onTimerTick?.(frame.remaining_seconds);
        break;
      case 'agent_status':
        this.opts.onAgentStatus?.(frame.state);
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

export function makeCandidateWsUrl(token: string): (lastSeq: number) => string {
  return (lastSeq: number) => {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${proto}//${window.location.host}/sessions/candidate-web?token=${encodeURIComponent(token)}&last_seq=${lastSeq}`;
  };
}
