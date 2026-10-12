import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  endSessionByToken,
  EndSessionError,
  getSessionByCandidateToken,
  SessionConfigError,
} from '../api/rest';
import type { Attendee, BashConfirmRequestFrame, FileTreeEntry } from '../api/types';
import { SessionProvider } from '../state/SessionContext';
import { useSessionState } from '../state/useSessionState';
import { TimeNowProvider } from '../state/TimeNowProvider';
import { CandidateSocket, makeCandidateWsUrl } from '../ws/candidateClient';
import { ConnectionBadge } from '../components/ConnectionBadge';
import { DialogTab } from '../components/DialogTab';
import { FileTree } from '../components/FileTree';
import { LanguageSwitcher } from '../components/LanguageSwitcher';
import { buildTree } from '../components/filesTabUtils';
import { aggregateCommands } from '../components/consoleTabUtils';
import { ConsoleSection } from '../components/ConsoleSection';
import { MobileNotice } from '../components/MobileNotice';
import { ConfirmDialog } from '../components/ConfirmDialog';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import styles from './CandidatePage.module.css';

// Format remaining async seconds as H:MM:SS (drops the hour when < 1h).
function formatRemaining(total: number): string {
  const s = Math.max(0, Math.floor(total));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const mm = String(m).padStart(2, '0');
  const ss = String(sec).padStart(2, '0');
  return h > 0 ? `${h}:${mm}:${ss}` : `${m}:${ss}`;
}

const MonacoEditor = lazy(() => import('../components/MonacoEditor'));

export function CandidatePage() {
  const [searchParams] = useSearchParams();
  const token = searchParams.get('token') ?? '';

  if (!token) {
    return <TokenError />;
  }

  return (
    <SessionProvider>
      <CandidatePageInner token={token} />
    </SessionProvider>
  );
}

type LoadState =
  | { kind: 'loading' }
  | { kind: 'ready' }
  | { kind: 'error'; title: string; message: string };

type EndState =
  | { kind: 'idle' }
  | { kind: 'pending' }
  | { kind: 'error'; message: string };

// Presence gate phase
type PresencePhase =
  | { kind: 'pre_join' }
  | { kind: 'joining' }
  | { kind: 'join_error'; reason: string }
  | { kind: 'joined' };

function CandidatePageInner({ token }: { token: string }) {
  const { t } = useTranslation();
  const { state, dispatch } = useSessionState();
  const [load, setLoad] = useState<LoadState>({ kind: 'loading' });
  const [endState, setEndState] = useState<EndState>({ kind: 'idle' });
  const [inputText, setInputText] = useState('');
  // The chat/terminal renders a sent message only when the backend echoes its
  // user_message/command_run event. If the backend rejects the frame (BUSY while
  // an agent turn runs, or a payload error) the input was already cleared, so the
  // message would vanish silently. pendingSendRef holds the last attempt so onError
  // can restore it, and sendNotice surfaces the rejection.
  // Holds a discriminant, not a resolved string — t() is applied at render time so
  // the notice always uses the current language (the onError closure that sets it
  // lives in the socket-setup effect, which captures a stale t otherwise).
  const [sendNotice, setSendNotice] = useState<'busy' | 'failed' | null>(null);
  // Backend agent_status — drives the "AI is working…" indicator for the
  // whole turn so a tool-heavy turn that goes quiet doesn't look idle.
  const [agentWorking, setAgentWorking] = useState(false);
  const pendingSendRef = useRef<string | null>(null);
  const sendNoticeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const socketRef = useRef<CandidateSocket | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const treeDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Ref so WS event handler (closed over [load.kind, token, dispatch]) can read current path.
  const selectedPathRef = useRef<string | null>(null);

  // Presence gate
  const [presencePhase, setPresencePhase] = useState<PresencePhase>({ kind: 'pre_join' });
  const [nameInput, setNameInput] = useState('');
  const joinedNameRef = useRef('');
  const [attendees, setAttendees] = useState<Attendee[]>([]);
  // wsStarted transitions false→true when user first clicks Join; stays true.
  const [wsStarted, setWsStarted] = useState(false);

  // Sandbox reset / expired / restored notice
  const [sandboxResetReason, setSandboxResetReason] = useState<
    'manual' | 'expired' | 'restored' | null
  >(null);
  const sandboxResetTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const sandboxResetTreeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Console panel state
  const [consoleOpen, setConsoleOpen] = useState(false);

  // Bash confirm modal state
  type BashConfirmState =
    | { kind: 'hidden' }
    | { kind: 'pending'; cmd_id: string; cmd: string; prefix: string | null };
  const [bashConfirm, setBashConfirm] = useState<BashConfirmState>({ kind: 'hidden' });

  // File tree + editor state
  const [treeEntries, setTreeEntries] = useState<FileTreeEntry[]>([]);
  const treePaths = useMemo(
    () => treeEntries.filter((e) => e.kind !== 'dir').map((e) => e.path),
    [treeEntries],
  );
  const treeDirs = useMemo(
    () => treeEntries.filter((e) => e.kind === 'dir').map((e) => e.path),
    [treeEntries],
  );
  const [modifiedPaths, setModifiedPaths] = useState<Set<string>>(new Set());
  const [collapsedDirs, setCollapsedDirs] = useState<Set<string>>(new Set());
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [editorContent, setEditorContent] = useState('');
  const [editorLoading, setEditorLoading] = useState(false);
  const [savingPath, setSavingPath] = useState<string | null>(null);

  // Panel resize state
  const [sidebarWidth, setSidebarWidth] = useState(220);
  const sidebarWidthRef = useRef(sidebarWidth);
  const [rightWidth, setRightWidth] = useState(360);
  const rightWidthRef = useRef(rightWidth);

  // Terminal input state
  const [terminalCmd, setTerminalCmd] = useState('');

  // Async countdown + submit confirm. remainingSeconds is server-authoritative
  // (reset on every timer_tick); a local 1s interval interpolates between ticks.
  const [remainingSeconds, setRemainingSeconds] = useState<number | null>(null);
  const [submitModalOpen, setSubmitModalOpen] = useState(false);
  // Live-mode end confirm (shadcn AlertDialog) replacing window.confirm.
  const [confirmEnd, setConfirmEnd] = useState(false);
  // Guard the async submit against a double-click. The ref is the
  // synchronous lock (two clicks in one tick); `submitting` drives the disabled UI.
  const submitSentRef = useRef(false);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    setLoad({ kind: 'loading' });
    getSessionByCandidateToken(token, { signal: controller.signal })
      .then((config) => {
        dispatch({ type: 'CONFIG_LOADED', config });
        setLoad({ kind: 'ready' });
      })
      .catch((err: unknown) => {
        if (err instanceof DOMException && err.name === 'AbortError') return;
        if (err instanceof SessionConfigError) {
          setLoad({
            kind: 'error',
            title: t(`error.title.${configTitleKey(err.reason)}`),
            message: err.message,
          });
        } else {
          setLoad({
            kind: 'error',
            title: t('error.title.unknown'),
            message: err instanceof Error ? err.message : String(err),
          });
        }
      });
    return () => controller.abort();
  }, [token, dispatch, t]);

  useEffect(() => {
    if (load.kind !== 'ready') return;
    // WS starts only after the user submits their name (presence gate).
    if (!wsStarted) return;
    const socket = new CandidateSocket(makeCandidateWsUrl(token), {
      joinName: joinedNameRef.current,
      onSessionJoinOk: () => setPresencePhase({ kind: 'joined' }),
      onSessionJoinError: (reason) => setPresencePhase({ kind: 'join_error', reason }),
      onPresenceUpdated: (att) => setAttendees(att),
      onHello: (hello) => dispatch({ type: 'WS_HELLO', hello }),
      onEvent: (event) => {
        dispatch({ type: 'WS_EVENT', event });
        // session_start: tree loading is handled by the presencePhase/sessionStarted
        // useEffect below — no duplicate request here.
        if (event.type === 'file_changed' || event.type === 'command_finished') {
          if (treeDebounceRef.current) clearTimeout(treeDebounceRef.current);
          treeDebounceRef.current = setTimeout(() => {
            const s = socketRef.current;
            if (!s) return;
            s.sendFsRequest('tree').then((resp) => {
              if (!resp.error && resp.entries) {
                setTreeEntries(resp.entries);
              }
            });
          }, 300);
        }
        // Accumulate modified-badge state from file_changed events.
        // Baseline events seed File History diffs, not "the file was edited".
        if (
          event.type === 'file_changed' &&
          !event.payload.is_dir &&
          event.payload.source !== 'baseline'
        ) {
          const { file_path, op, content } = event.payload;
          setModifiedPaths((prev) => {
            const next = new Set(prev);
            // op='deleted' covers newer events; content===null covers old CLI events (no op).
            if (op === 'deleted' || (!op && content === null)) {
              next.delete(file_path);
            } else {
              next.add(file_path);
            }
            return next;
          });
        }
        // Update editor content immediately when the AI edits the currently open file.
        // Note: if the candidate has unsaved changes within the 1s auto-save window,
        // those are discarded — the sandbox file is the source of truth and the AI's
        // write already persisted there. Full conflict resolution is out of scope.
        if (
          event.type === 'file_changed' &&
          event.payload.file_path === selectedPathRef.current &&
          typeof event.payload.content === 'string'
        ) {
          setEditorContent(event.payload.content);
        }
        if (event.type === 'command_run') {
          setConsoleOpen(true);
        }
        if (event.type === 'sandbox_reset' || event.type === 'sandbox_restored') {
          // For reset: FS is back to baseline → clear local UI state first.
          // For restored: FS has been re-populated from event-log replay onto a fresh sandbox.
          if (event.type === 'sandbox_reset') {
            setTreeEntries([]);
            setModifiedPaths(new Set());
            setSelectedPath(null);
            setEditorContent('');
          }
          // Refresh tree after a short delay (sandbox needs a moment to be ready).
          if (sandboxResetTreeTimerRef.current) clearTimeout(sandboxResetTreeTimerRef.current);
          sandboxResetTreeTimerRef.current = setTimeout(() => {
            const s = socketRef.current;
            if (!s) return;
            s.sendFsRequest('tree').then((resp) => {
              if (!resp.error && resp.entries) {
                setTreeEntries(resp.entries);
              }
            });
          }, 500);
          // Show notice banner for 5 s. Three reasons: expired / manual reset / restored from history.
          const reason: 'manual' | 'expired' | 'restored' =
            event.type === 'sandbox_restored'
              ? 'restored'
              : event.payload.reason === 'expired'
              ? 'expired'
              : 'manual';
          setSandboxResetReason(reason);
          if (sandboxResetTimerRef.current) clearTimeout(sandboxResetTimerRef.current);
          sandboxResetTimerRef.current = setTimeout(() => setSandboxResetReason(null), 5000);
        }
      },
      onError: (frame) => {
        // Surface a rejected candidate frame instead of dropping it silently.
        // BUSY = an agent turn is still running (the assistant ran a long tool
        // sequence); the message was not accepted, so restore the unsent text
        // (without clobbering anything typed since) and tell the candidate.
        const pending = pendingSendRef.current;
        pendingSendRef.current = null;
        if (pending && frame.code === 'BUSY') {
          setInputText((cur) => (cur ? cur : pending));
        }
        setSendNotice(frame.code === 'BUSY' ? 'busy' : 'failed');
        if (sendNoticeTimerRef.current) clearTimeout(sendNoticeTimerRef.current);
        sendNoticeTimerRef.current = setTimeout(() => setSendNotice(null), 6000);
      },
      onAgentStatus: (state) => setAgentWorking(state === 'working'),
      onStateChange: (connection) => dispatch({ type: 'WS_STATE_CHANGED', connection }),
      onFatal: (reason, message) => dispatch({ type: 'WS_FATAL', reason, message }),
      onBashConfirmRequest: (frame: BashConfirmRequestFrame) => {
        setBashConfirm({ kind: 'pending', cmd_id: frame.cmd_id, cmd: frame.cmd, prefix: frame.prefix });
      },
      // Server-authoritative countdown for async sessions.
      onTimerTick: (remaining) => setRemainingSeconds(remaining),
    });
    socketRef.current = socket;
    socket.start();
    return () => {
      socket.stop();
      socketRef.current = null;
      if (treeDebounceRef.current) clearTimeout(treeDebounceRef.current);
      if (sandboxResetTimerRef.current) clearTimeout(sandboxResetTimerRef.current);
      if (sandboxResetTreeTimerRef.current) clearTimeout(sandboxResetTreeTimerRef.current);
      if (sendNoticeTimerRef.current) clearTimeout(sendNoticeTimerRef.current);
    };
  }, [load.kind, token, dispatch, wsStarted]);

  // state.sessionStarted is set by the reducer on first session_start event (O(1)).
  // Used to gate the input area (Bug 4) and the initial file tree load (Bug 5).
  const { sessionStarted } = state;

  // Load initial file tree once joined, connected, AND session_start received.
  // - presencePhase guard: sendFsRequest before join → JOIN_REQUIRED from backend.
  // - sessionStarted guard: WebFsProxy is only registered after host.start()
  //   completes; session_start is emitted right after, so this is the reliable signal.
  useEffect(() => {
    if (state.connection !== 'connected') return;
    if (presencePhase.kind !== 'joined') return;
    if (!sessionStarted) return;
    const socket = socketRef.current;
    if (!socket) return;
    socket.sendFsRequest('tree').then((resp) => {
      if (!resp.error && resp.entries) {
        setTreeEntries(resp.entries);
      }
    });
  }, [state.connection, presencePhase.kind, sessionStarted]);

  // Keep ref in sync for WS event handler closure.
  useEffect(() => {
    selectedPathRef.current = selectedPath;
  }, [selectedPath]);

  // Open a file in the editor.
  const handleSelectFile = useCallback(
    (path: string) => {
      setSelectedPath(path);
      const socket = socketRef.current;
      // Pre-join: socket not yet created; tree is not shown so this is unreachable
      // in normal flow. Guard exists for defensive completeness.
      if (!socket) return;
      setEditorLoading(true);
      socket
        .sendFsRequest('read', path)
        .then((resp) => {
          setEditorContent(resp.error ? '' : (resp.content ?? ''));
        })
        .finally(() => setEditorLoading(false));
    },
    [],
  );

  const handleToggleDir = useCallback((path: string) => {
    setCollapsedDirs((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }, []);

  // Keep width refs in sync so resize handlers don't need them in deps.
  useEffect(() => { sidebarWidthRef.current = sidebarWidth; }, [sidebarWidth]);
  useEffect(() => { rightWidthRef.current = rightWidth; }, [rightWidth]);

  // Panel resize handlers
  const handleSidebarResizerMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startW = sidebarWidthRef.current;
    const onMove = (ev: MouseEvent) =>
      setSidebarWidth(Math.max(120, Math.min(500, startW + ev.clientX - startX)));
    const onUp = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  }, []);

  const handleRightResizerMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startW = rightWidthRef.current;
    const onMove = (ev: MouseEvent) =>
      setRightWidth(Math.max(200, Math.min(700, startW + startX - ev.clientX)));
    const onUp = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  }, []);

  // Terminal input handler
  const handleTerminalKeyDown = useCallback((e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      const cmd = terminalCmd.trim();
      if (!cmd) return;
      socketRef.current?.sendInlineBash(cmd);
      // A terminal BUSY surfaces the notice but must not restore stale chat text.
      pendingSendRef.current = null;
      setSendNotice(null);
      setTerminalCmd('');
      setConsoleOpen(true);
    }
  }, [terminalCmd]);

  // Save file via Monaco Cmd+S.
  const handleSave = useCallback(
    (content: string) => {
      if (!selectedPath) return;
      const socket = socketRef.current;
      if (!socket) return;
      setSavingPath(selectedPath);
      socket
        .sendFsRequest('write', selectedPath, content)
        .then((resp) => {
          if (!resp.error) setEditorContent(content);
        })
        .finally(() => setSavingPath(null));
    },
    [selectedPath],
  );

  const handleSend = useCallback(() => {
    const text = inputText.trim();
    if (!text) return;
    if (text.startsWith('!')) {
      const cmd = text.slice(1).trim();
      if (cmd) socketRef.current?.sendInlineBash(cmd);
    } else {
      socketRef.current?.sendUserMessage(text);
    }
    // Remember the attempt so onError can restore it if the backend rejects it.
    pendingSendRef.current = text;
    setSendNotice(null);
    setInputText('');
    inputRef.current?.focus();
  }, [inputText]);

  const handleBashConfirmChoice = useCallback(
    (choice: 'allow_once' | 'allow_prefix' | 'deny') => {
      if (bashConfirm.kind !== 'pending') return;
      socketRef.current?.sendBashConfirmResponse(bashConfirm.cmd_id, choice);
      setBashConfirm({ kind: 'hidden' });
    },
    [bashConfirm],
  );

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        handleSend();
      }
    },
    [handleSend],
  );

  const handleEnd = useCallback(() => {
    if (!state.config) return;
    setEndState({ kind: 'pending' });
    endSessionByToken(state.config.session_code, token)
      .then(() => setEndState({ kind: 'idle' }))
      .catch((err: unknown) => {
        const message = err instanceof EndSessionError ? err.message : String(err);
        setEndState({ kind: 'error', message });
      });
  }, [state.config, token]);

  // Join handler
  const handleJoin = useCallback(() => {
    const name = nameInput.trim();
    if (!name) return;
    joinedNameRef.current = name;
    if (!wsStarted) {
      // First join: start WS (wsStarted triggers the useEffect)
      setPresencePhase({ kind: 'joining' });
      setWsStarted(true);
    } else {
      // Retry after session_join_error: send on existing socket
      setPresencePhase({ kind: 'joining' });
      socketRef.current?.sendSessionJoin(name);
    }
  }, [nameInput, wsStarted]);

  const emptyLogs = useMemo(() => new Map(), []);
  const fileTree = useMemo(
    () =>
      buildTree(
        emptyLogs,
        treePaths.length > 0 ? treePaths : undefined,
        treeDirs.length > 0 ? treeDirs : undefined,
      ),
    [emptyLogs, treePaths, treeDirs],
  );

  const commandLogs = useMemo(() => aggregateCommands(state.events), [state.events]);

  const frozenAt = useMemo<number | null>(() => {
    if (state.sessionStatus !== 'completed') return null;
    for (let i = state.events.length - 1; i >= 0; i--) {
      const e = state.events[i]!;
      if (e.type === 'session_end') {
        const ts = Date.parse(e.timestamp);
        if (Number.isFinite(ts)) return ts;
        break;
      }
    }
    if (state.events.length > 0) {
      const ts = Date.parse(state.events[state.events.length - 1]!.timestamp);
      if (Number.isFinite(ts)) return ts;
    }
    return Date.now();
  }, [state.sessionStatus, state.events]);

  // Async mode + terminal reason (for the end-screen copy).
  const isAsync = state.config?.mode === 'async';
  const completed = state.sessionStatus === 'completed';
  const endReason = useMemo<string | null>(() => {
    if (!completed) return null;
    for (let i = state.events.length - 1; i >= 0; i--) {
      const e = state.events[i]!;
      if (e.type === 'session_end') return e.payload.reason;
    }
    return null;
  }, [completed, state.events]);

  // Seed the countdown from the session's limit so the pill shows immediately,
  // before the first server timer_tick arrives (started_at ≈ connect time).
  useEffect(() => {
    if (!isAsync || completed) return;
    if (!sessionStarted) return;
    if (remainingSeconds !== null) return;
    const limit = state.config?.time_limit_seconds;
    if (typeof limit === 'number') setRemainingSeconds(limit);
  }, [isAsync, completed, sessionStarted, remainingSeconds, state.config]);

  const hasCountdown = remainingSeconds !== null;

  // Local 1s interpolation between server ticks. Server remains authoritative:
  // each timer_tick resets remainingSeconds via onTimerTick.
  // D-13: pause interpolation while the socket is down. The server clock is
  // authoritative and survives a disconnect (it re-arms from started_at on
  // reconnect), so ticking the pill to 0 during a transient drop would mislead
  // the candidate; the next timer_tick after reconnect resets it to the real value.
  useEffect(() => {
    if (!isAsync || completed) return;
    if (!hasCountdown) return;
    if (state.connection !== 'connected') return;
    const id = setInterval(() => {
      setRemainingSeconds((prev) => (prev === null ? prev : Math.max(0, prev - 1)));
    }, 1000);
    return () => clearInterval(id);
  }, [isAsync, completed, hasCountdown, state.connection]);

  const handleSubmitAsync = useCallback(() => {
    if (submitSentRef.current) return;
    const socket = socketRef.current;
    if (!socket) return; // not connected — let the candidate retry once reconnected
    submitSentRef.current = true;
    setSubmitting(true);
    socket.sendSessionSubmit();
    // Keep the dialog open in its pending state; the server's session_end swaps the
    // whole view to the async end-screen. No setSubmitModalOpen(false) here.
  }, []);

  // Release the submit lock if the socket drops before session_end arrives, so a
  // candidate who lost connection mid-submit can try again after reconnecting.
  // Also clear the stale "AI working" hint — on reconnect the backend
  // re-sends agent_status:working if a turn is still in flight (ws.py connect path).
  useEffect(() => {
    if (state.connection !== 'connected') {
      submitSentRef.current = false;
      setSubmitting(false);
      setAgentWorking(false);
    }
  }, [state.connection]);

  if (load.kind === 'loading') {
    return (
      <div className={styles.page}>
        <p>{t('session.loading')}</p>
      </div>
    );
  }
  if (load.kind === 'error') {
    return (
      <div className={styles.page}>
        <h2>{load.title}</h2>
        <p data-testid="candidate-error-message">{load.message}</p>
      </div>
    );
  }
  if (state.fatal) {
    return (
      <div className={styles.page}>
        <h2>{t(`error.title.${fatalTitleKey(state.fatal.reason)}`)}</h2>
        <p data-testid="candidate-fatal-message">{state.fatal.message}</p>
      </div>
    );
  }

  // Name entry gate — shown before WS is initiated or while joining
  if (presencePhase.kind === 'pre_join' || presencePhase.kind === 'join_error' || presencePhase.kind === 'joining') {
    const isJoining = presencePhase.kind === 'joining';
    const errorMsg =
      presencePhase.kind === 'join_error'
        ? t(
            presencePhase.reason === 'name_taken'
              ? 'presence.join.error.nameTaken'
              : presencePhase.reason === 'duplicate_connection'
                ? 'presence.join.error.duplicateConnection'
                : 'presence.join.error.invalidName',
          )
        : null;
    return (
      <div className={styles.page} data-testid="presence-join-screen">
        <div className={styles.joinCard}>
          <p className={styles.joinTitle}>{t('presence.join.title')}</p>
          {errorMsg && (
            <p className={styles.joinError} role="alert" data-testid="presence-join-error">
              {errorMsg}
            </p>
          )}
          <input
            className={styles.joinInput}
            type="text"
            value={nameInput}
            onChange={(e) => setNameInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') handleJoin(); }}
            placeholder={t('presence.join.placeholder')}
            disabled={isJoining}
            autoFocus
            data-testid="presence-name-input"
          />
          <button
            type="button"
            className={styles.joinButton}
            onClick={handleJoin}
            disabled={!nameInput.trim() || isJoining}
            data-testid="presence-join-button"
          >
            {t('presence.join.button')}
          </button>
        </div>
      </div>
    );
  }

  const sessionEnded = state.sessionStatus === 'completed';
  const isReconnecting = state.connection === 'reconnecting';

  // Dedicated async end-screen (the IDE is gone — nothing left to do).
  // Live sessions keep the read-only IDE + "Session ended" badge instead.
  if (isAsync && sessionEnded) {
    const titleKey =
      endReason === 'timeout'
        ? 'candidate.endScreen.timeoutTitle'
        : endReason === 'submitted'
        ? 'candidate.endScreen.submittedTitle'
        : 'candidate.endScreen.endedTitle';
    const bodyKey =
      endReason === 'timeout'
        ? 'candidate.endScreen.timeoutBody'
        : endReason === 'submitted'
        ? 'candidate.endScreen.submittedBody'
        : 'candidate.endScreen.endedBody';
    return (
      <div className={styles.page}>
        <div className={styles.endScreen} data-testid="candidate-end-screen" data-reason={endReason ?? ''}>
          <h2 className={styles.endScreenTitle}>{t(titleKey)}</h2>
          <p className={styles.endScreenBody}>{t(bodyKey)}</p>
        </div>
      </div>
    );
  }

  return (
    <TimeNowProvider frozenAt={frozenAt}>
      <MobileNotice />
      <div className={styles.appShell}>
        <header className={styles.header}>
          <div className={styles.headerLeft}>
            {isReconnecting && (
              <span
                className={styles.reconnectBanner}
                data-testid="candidate-reconnect-banner"
              >
                {t('candidate.connectionLost')}
              </span>
            )}
            {sandboxResetReason !== null && (
              <span
                className={styles.sandboxResetBanner}
                data-testid="candidate-sandbox-reset-banner"
                data-reason={sandboxResetReason}
              >
                {sandboxResetReason === 'expired'
                  ? t('candidate.sandboxExpired')
                  : sandboxResetReason === 'restored'
                  ? t('candidate.sandboxRestored')
                  : t('candidate.sandboxReset')}
              </span>
            )}
          </div>
          {/* presence panel */}
          {attendees.length > 0 && (
            <div className={styles.presencePanel} data-testid="presence-panel">
              {attendees.map((a) => {
                // Backend sends "Interviewer" or "Interviewer N" for masked records.
                // Reconstruct with the translated prefix so the localized label renders.
                const displayName = a.role === 'interviewer'
                  ? t('presence.attendee.anonymousInterviewer') + a.name.slice('Interviewer'.length)
                  : a.name;
                return (
                  <span
                    key={a.first_seen}
                    className={styles.presenceAttendee}
                    data-active={a.is_active}
                    title={a.is_active ? t('presence.attendee.online') : t('presence.attendee.offline')}
                  >
                    <span className={styles.presenceDot} />
                    {displayName}
                  </span>
                );
              })}
            </div>
          )}

          <div className={styles.headerRight}>
            {/* async countdown pill — hidden for live sessions. */}
            {isAsync && remainingSeconds !== null && (
              <span
                className={styles.timerPill}
                data-testid="candidate-timer"
                data-low={remainingSeconds <= 60}
                title={t('candidate.timer.label')}
              >
                {remainingSeconds <= 0
                  ? t('candidate.timer.timeUp')
                  : formatRemaining(remainingSeconds)}
              </span>
            )}
            <ConnectionBadge state={state.connection} />
            {sessionEnded ? (
              <span className={styles.sessionEnded} data-testid="candidate-session-ended">
                {t('candidate.sessionEnded')}
              </span>
            ) : isAsync ? (
              // Async: Submit opens a confirm dialog → session_submit WS frame.
              <button
                type="button"
                className={styles.endButton}
                onClick={() => setSubmitModalOpen(true)}
                data-testid="candidate-submit-button"
              >
                {t('candidate.submit.button')}
              </button>
            ) : (
              // Live: existing REST /end flow.
              <button
                type="button"
                className={styles.endButton}
                disabled={endState.kind === 'pending'}
                onClick={() => setConfirmEnd(true)}
                data-testid="candidate-end-button"
              >
                {endState.kind === 'pending'
                  ? t('session.end.confirming')
                  : t('candidate.endSession')}
              </button>
            )}
            <LanguageSwitcher />
          </div>
        </header>

        {endState.kind === 'error' && (
          <p className={styles.endError} role="alert" data-testid="candidate-end-error">
            {endState.message}
          </p>
        )}

        {/* CSS Grid: sidebar | sb-resizer | editor | right-resizer | right */}
        <div
          className={styles.mainArea}
          style={{ gridTemplateColumns: `${sidebarWidth}px 4px 1fr 4px ${rightWidth}px` }}
        >
          {/* Col 1 Row 1: file tree */}
          <div className={styles.sidebarPanel}>
            <div className={styles.fileTreePanel} data-testid="candidate-file-tree">
              {treePaths.length > 0 || treeDirs.length > 0 ? (
                <FileTree
                  tree={fileTree}
                  selectedPath={selectedPath}
                  collapsedDirs={collapsedDirs}
                  onSelect={handleSelectFile}
                  onToggleDir={handleToggleDir}
                  modifiedPaths={modifiedPaths}
                />
              ) : (
                <p className={styles.treeEmpty}>{t('candidate.filesEmpty')}</p>
              )}
            </div>
          </div>

          {/* Col 2 Row 1: sidebar resize handle */}
          <div
            className={styles.sidebarResizer}
            onMouseDown={handleSidebarResizerMouseDown}
            title="Drag to resize"
          />

          {/* Col 3 Row 1: Monaco editor */}
          <div className={styles.editorPanel}>
            {editorLoading && (
              <p className={styles.editorLoading}>{t('candidate.fileLoading')}</p>
            )}
            {!editorLoading && selectedPath !== null && (
              <Suspense fallback={<p className={styles.editorLoading}>{t('candidate.fileLoading')}</p>}>
                <MonacoEditor
                  path={selectedPath}
                  content={editorContent}
                  onSave={sessionEnded ? undefined : handleSave}
                  readOnly={sessionEnded}
                />
              </Suspense>
            )}
            {!editorLoading && selectedPath === null && (
              <p className={styles.editorPlaceholder}>{t('candidate.selectFile')}</p>
            )}
            {savingPath !== null && (
              <span className={styles.savingIndicator} data-testid="candidate-saving">
                {t('candidate.saving')}
              </span>
            )}
          </div>

          {/* Col 4 Row 1: right panel resize handle */}
          <div
            className={styles.rightResizer}
            onMouseDown={handleRightResizerMouseDown}
            title="Drag to resize"
          />

          {/* Col 5 Rows 1-2: AI chat + input */}
          <div className={styles.rightPanel}>
            <div className={styles.chatArea}>
              <DialogTab
                events={state.events}
                sessionActive={!sessionEnded}
                sessionStatus={state.sessionStatus}
                forceTyping={agentWorking}
              />
            </div>

            {!sessionEnded && !sessionStarted && (
              <div className={styles.inputArea} data-testid="candidate-ai-initializing">
                <p className={styles.aiInitializing}>{t('candidate.aiInitializing')}</p>
              </div>
            )}
            {!sessionEnded && sessionStarted && (
              <div className={styles.inputArea} data-testid="candidate-input-area">
                {sendNotice && (
                  <p
                    className={styles.sendNotice}
                    role="status"
                    data-testid="candidate-send-notice"
                  >
                    {t(sendNotice === 'busy' ? 'candidate.send.busy' : 'candidate.send.failed')}
                  </p>
                )}
                <textarea
                  ref={inputRef}
                  className={styles.input}
                  value={inputText}
                  onChange={(e) => setInputText(e.target.value)}
                  onKeyDown={handleKeyDown}
                  placeholder={t('candidate.placeholder')}
                  rows={3}
                  data-testid="candidate-input"
                />
                <button
                  type="button"
                  className={styles.sendButton}
                  onClick={handleSend}
                  disabled={!inputText.trim()}
                  data-testid="candidate-send"
                >
                  {t('candidate.send')}
                </button>
              </div>
            )}
          </div>

          {/* Cols 1-4 Row 2: console output (spans sidebar+editor) + terminal input */}
          <ConsoleSection
            className={styles.consoleSection}
            commandLogs={commandLogs}
            open={consoleOpen}
            onToggle={() => setConsoleOpen((o) => !o)}
            testIdPrefix="candidate"
            terminalBar={
              !sessionEnded && sessionStarted ? (
                <div className={styles.terminalBar}>
                  <span className={styles.terminalPrompt}>$</span>
                  <input
                    className={styles.terminalInput}
                    type="text"
                    value={terminalCmd}
                    onChange={(e) => setTerminalCmd(e.target.value)}
                    onKeyDown={handleTerminalKeyDown}
                    placeholder={t('candidate.terminal.placeholder')}
                    spellCheck={false}
                    autoComplete="off"
                    data-testid="terminal-input"
                  />
                </div>
              ) : undefined
            }
          />
        </div>

        {/* Bash confirm modal */}
        {bashConfirm.kind === 'pending' && (
          <div
            className={styles.confirmOverlay}
            role="dialog"
            aria-modal="true"
            data-testid="bash-confirm-modal"
          >
            <div className={styles.confirmModal}>
              <p className={styles.confirmTitle}>{t('candidate.bash.confirm.title')}</p>
              <pre className={styles.confirmCmd}>{bashConfirm.cmd}</pre>
              <div className={styles.confirmButtons}>
                <button
                  type="button"
                  className={styles.confirmAllowOnce}
                  onClick={() => handleBashConfirmChoice('allow_once')}
                  data-testid="bash-confirm-allow-once"
                >
                  {t('candidate.bash.confirm.allowOnce')}
                </button>
                <button
                  type="button"
                  className={styles.confirmAllowPrefix}
                  onClick={() => handleBashConfirmChoice('allow_prefix')}
                  disabled={!bashConfirm.prefix}
                  data-testid="bash-confirm-allow-prefix"
                >
                  {bashConfirm.prefix
                    ? t('candidate.bash.confirm.allowPrefix', { prefix: bashConfirm.prefix })
                    : t('candidate.bash.confirm.allowPrefixDisabled')}
                </button>
                <button
                  type="button"
                  className={styles.confirmDeny}
                  onClick={() => handleBashConfirmChoice('deny')}
                  data-testid="bash-confirm-deny"
                >
                  {t('candidate.bash.confirm.deny')}
                </button>
              </div>
            </div>
          </div>
        )}

        {/* async submit confirm dialog */}
        <Dialog
          open={submitModalOpen}
          onOpenChange={(open) => {
            if (!submitting) setSubmitModalOpen(open);
          }}
        >
          <DialogContent data-testid="submit-confirm-modal" className="sm:max-w-[440px]">
            <DialogHeader>
              <DialogTitle>{t('candidate.submit.confirmTitle')}</DialogTitle>
              <DialogDescription>{t('candidate.submit.confirmBody')}</DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button
                variant="outline"
                disabled={submitting}
                onClick={() => setSubmitModalOpen(false)}
                data-testid="submit-cancel"
              >
                {t('candidate.submit.cancel')}
              </Button>
              <Button onClick={handleSubmitAsync} disabled={submitting} data-testid="submit-confirm">
                {submitting ? t('candidate.submit.submitting') : t('candidate.submit.confirm')}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>

        {/* F1: live-mode end confirm (async uses the submit dialog above). */}
        <ConfirmDialog
          open={confirmEnd}
          onOpenChange={setConfirmEnd}
          title={t('candidate.endConfirmTitle')}
          description={t('candidate.endConfirm')}
          confirmLabel={t('candidate.endSession')}
          cancelLabel={t('common.cancel')}
          onConfirm={handleEnd}
          destructive
          testId="candidate-end-confirm"
        />
      </div>
    </TimeNowProvider>
  );
}

function configTitleKey(reason: string): string {
  switch (reason) {
    case 'unauthorized':
      return 'unauthorized';
    case 'not_found':
      return 'notFound';
    case 'completed':
      return 'completed';
    case 'network':
      return 'network';
    case 'invalid_response':
      return 'invalidResponse';
    default:
      return 'unknown';
  }
}

function fatalTitleKey(reason: string): string {
  switch (reason) {
    case 'unauthorized':
    case 'forbidden':
      return 'unauthorized';
    case 'not_found':
      return 'notFound';
    case 'session_ended':
      return 'completed';
    default:
      return 'unknown';
  }
}

function TokenError() {
  const { t } = useTranslation();
  return (
    <div className={styles.page}>
      <h2>{t('error.title.unauthorized')}</h2>
      <p data-testid="candidate-error-message">{t('error.message.unauthorized')}</p>
    </div>
  );
}
