import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { Copy, Share2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import {
  endSession,
  EndSessionError,
  getEvents,
  getFileContent,
  getFileTree,
  getReplayConfig,
  getSessionConfig,
  postShare,
  resetSandbox,
  ReplayConfigError,
  SessionConfigError,
} from '../api/rest';
import type { Attendee, FileTreeEntry } from '../api/types';
import { useSessionState } from '../state/useSessionState';
import { TimeNowProvider } from '../state/TimeNowProvider';
import { InterviewerSocket, makeInterviewerWsUrl } from '../ws/client';
import { ConnectionBadge } from './ConnectionBadge';
import { Elapsed } from './Elapsed';
import { DialogTab } from './DialogTab';
import { AssessmentTab } from './AssessmentTab';
import { FileTree } from './FileTree';
import { LanguageSwitcher } from './LanguageSwitcher';
import { FileDiffHistory } from './FileDiffHistory';
import { aggregateFileLogs, buildTree } from './filesTabUtils';
import { aggregateCommands } from './consoleTabUtils';
import { ConsoleSection } from './ConsoleSection';
import { ReplayTimeline } from './ReplayTimeline';
import { Timeline } from './Timeline';
import { buildTimeline } from './timelineUtils';
import { MobileNotice } from './MobileNotice';
import { ConfirmDialog } from './ConfirmDialog';
import { replayModifiedPaths, replayTreePaths } from './replayProjection';
import styles from './SessionView.module.css';

const MonacoEditor = lazy(() => import('./MonacoEditor'));

type LoadState =
  | { kind: 'loading' }
  | { kind: 'ready' }
  | { kind: 'error'; message: string; title: string };

type EndState =
  | { kind: 'idle' }
  | { kind: 'pending' }
  | { kind: 'error'; message: string };

/**
 * Unified interviewer IDE / replay view. Live mode (no `shareToken`) connects
 * via WS with cookie auth and renders End/Share/Reset controls. Public replay
 * mode (`shareToken` present) loads config + events via the share-token
 * endpoints, never opens a WS, and hides all live controls — same layout.
 */
export function SessionView({
  code,
  candidateToken,
  shareToken,
}: {
  code: string;
  candidateToken?: string;
  /** Present → public read-only replay (share-token endpoints, no WS, no controls). */
  shareToken?: string;
}) {
  const { t } = useTranslation();
  const { state, dispatch } = useSessionState();
  const isPublicReplay = shareToken !== undefined;
  const [load, setLoad] = useState<LoadState>({ kind: 'loading' });
  const [endState, setEndState] = useState<EndState>({ kind: 'idle' });
  // config.candidate_token is populated after load; location.state token is available immediately.
  const effectiveCandidateToken = useMemo(
    () => state.config?.candidate_token ?? candidateToken,
    [state.config?.candidate_token, candidateToken],
  );

  const [copied, setCopied] = useState(false);
  const [copiedCandidateLink, setCopiedCandidateLink] = useState(false);
  const [shareState, setShareState] = useState<
    'idle' | 'loading' | { shareToken: string } | 'error'
  >('idle');
  const [shareLinkCopied, setShareLinkCopied] = useState(false);
  const [cursor, setCursor] = useState(0);
  const [resetState, setResetState] = useState<'idle' | 'pending' | 'error'>('idle');
  // Confirm dialogs (shadcn AlertDialog) replacing window.confirm.
  const [confirmEnd, setConfirmEnd] = useState(false);
  const [confirmReset, setConfirmReset] = useState(false);

  // Presence
  const [attendees, setAttendees] = useState<Attendee[]>([]);
  const [presenceAlert, setPresenceAlert] = useState(false);

  // Sandbox restored from event log after backend restart.
  const [sandboxRestoredNotice, setSandboxRestoredNotice] = useState<
    { file_count: number; deleted_count: number } | null
  >(null);
  const sandboxRestoredTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

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
  // Accumulate modified-badge state for live sessions (replay uses useMemo below).
  const [liveModifiedPaths, setLiveModifiedPaths] = useState<Set<string>>(new Set());
  const [collapsedDirs, setCollapsedDirs] = useState<Set<string>>(new Set());
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [editorContent, setEditorContent] = useState('');
  const [editorLoading, setEditorLoading] = useState(false);
  const selectedPathRef = useRef<string | null>(null);
  const treeDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Track how many events we've processed — index-based so each new event is O(1), not O(N).
  const lastProcessedIndexRef = useRef(0);

  // Editor tab: 'content' | 'history' — reset to 'content' on file selection.
  const [editorTab, setEditorTab] = useState<'content' | 'history'>('content');

  // Right panel tab: 'dialog' | 'timeline' | 'assessment'. The
  // assessment tab is interviewer-only — never shown in public replay; the
  // timeline needs a cursor, so it only shows for completed/replay sessions.
  const [rightTab, setRightTab] = useState<'dialog' | 'timeline' | 'assessment'>('dialog');
  // Transient deep-link target (a finding's event_seq). Cleared after the flash.
  const [highlightSeq, setHighlightSeq] = useState<number | null>(null);

  // Panel resize state
  const [sidebarWidth, setSidebarWidth] = useState(220);
  const sidebarWidthRef = useRef(sidebarWidth);
  const [rightWidth, setRightWidth] = useState(360);
  const rightWidthRef = useRef(rightWidth);

  // Console panel state
  const [consoleOpen, setConsoleOpen] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    setLoad({ kind: 'loading' });
    const configPromise = isPublicReplay
      ? getReplayConfig(code, shareToken, { signal: controller.signal })
      : getSessionConfig(code, { signal: controller.signal });
    configPromise
      .then((config) => {
        dispatch({ type: 'CONFIG_LOADED', config });
        setLoad({ kind: 'ready' });
      })
      .catch((err: unknown) => {
        if (err instanceof DOMException && err.name === 'AbortError') return;
        if (err instanceof SessionConfigError || err instanceof ReplayConfigError) {
          setLoad({ kind: 'error', title: errorTitle(t, err.reason), message: err.message });
        } else {
          setLoad({
            kind: 'error',
            title: t('error.title.unknown'),
            message: err instanceof Error ? err.message : String(err),
          });
        }
      });
    return () => controller.abort();
  }, [code, dispatch, isPublicReplay, shareToken, t]);

  // state.config.session_status is intentional: it reflects the status at load time and
  // stays stable. state.sessionStatus can flip to 'completed' via session_end event mid-watch
  // — in that case we want WS to stay open (it already delivered the events), not tear down
  // and re-fetch via REST.
  // Public replay is always a completed-session view (read-only, no WS).
  const isCompleted =
    isPublicReplay || (state.config !== null && state.config.session_status === 'completed');

  useEffect(() => {
    if (load.kind !== 'ready') return;
    // Completed sessions (and public replay): backend WS closes immediately with 4409
    // before replaying events. Load the full event log via REST instead.
    if (isCompleted) {
      const controller = new AbortController();
      getEvents(code, { signal: controller.signal, shareToken })
        .then((events) => {
          for (const event of events) {
            dispatch({ type: 'WS_EVENT', event });
          }
          setCursor(events.length);
        })
        .catch((err: unknown) => {
          if (err instanceof DOMException && err.name === 'AbortError') return;
          setLoad({
            kind: 'error',
            title: t('error.title.network'),
            message: err instanceof Error ? err.message : String(err),
          });
        });
      return () => controller.abort();
    }
    const socket = new InterviewerSocket(makeInterviewerWsUrl(code), {
      onHello: (frame) => dispatch({ type: 'WS_HELLO', hello: frame }),
      onEvent: (event) => dispatch({ type: 'WS_EVENT', event }),
      onError: () => {},
      onStateChange: (connection) => dispatch({ type: 'WS_STATE_CHANGED', connection }),
      onFatal: (reason, message) => dispatch({ type: 'WS_FATAL', reason, message }),
      onPresenceUpdated: (att) => setAttendees(att),
      onPresenceAlert: () => setPresenceAlert(true),
    });
    socket.start();
    return () => socket.stop();
  }, [load.kind, code, dispatch, isCompleted, shareToken, t]);

  // Load the file tree from the live sandbox (REST) for a live, connected session.
  // Completed sessions and public replays derive the tree purely from events
  // (replayTreePaths): the sandbox is torn down on completion (force_terminate),
  // so a REST /files call would just 503 (sandbox_unavailable) on every replay open
  // — skip it. sessionStarted gate mirrors CandidatePage: WebFsProxy is only
  // registered after host.start() completes, right before session_start is emitted.
  useEffect(() => {
    if (load.kind !== 'ready' || isPublicReplay || isCompleted) return;
    if (state.connection !== 'connected' || !state.sessionStarted) return;
    const controller = new AbortController();
    getFileTree(code, { signal: controller.signal })
      .then((resp) => setTreeEntries(resp.entries ?? []))
      .catch(() => {});
    return () => controller.abort();
  }, [state.connection, state.sessionStarted, isCompleted, isPublicReplay, load.kind, code]);

  // Keep width refs in sync so resize handlers don't need width in their deps.
  useEffect(() => { sidebarWidthRef.current = sidebarWidth; }, [sidebarWidth]);
  useEffect(() => { rightWidthRef.current = rightWidth; }, [rightWidth]);

  // Process new events: slice from last processed index (O(K) not O(N)).
  useEffect(() => {
    const newEvents = state.events.slice(lastProcessedIndexRef.current);
    lastProcessedIndexRef.current = state.events.length;

    for (const event of newEvents) {
      if (!isCompleted && (event.type === 'file_changed' || event.type === 'command_finished')) {
        if (treeDebounceRef.current) clearTimeout(treeDebounceRef.current);
        treeDebounceRef.current = setTimeout(() => {
          getFileTree(code)
            .then((resp) => setTreeEntries(resp.entries ?? []))
            .catch(() => {});
        }, 300);
      }

      // Update modified-badge state for live sessions.
      if (
        !isCompleted &&
        event.type === 'file_changed' &&
        !event.payload.is_dir &&
        event.payload.source !== 'baseline'
      ) {
        const { file_path, op, content } = event.payload;
        setLiveModifiedPaths((prev) => {
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
      if (!isCompleted && event.type === 'sandbox_reset') {
        // Reset emits only sandbox_reset, no file_changed(deleted) per file, so the
        // file_changed debounce above never fires — re-fetch the tree here so files
        // created during the session disappear without an F5.
        setLiveModifiedPaths(new Set());
        if (treeDebounceRef.current) clearTimeout(treeDebounceRef.current);
        treeDebounceRef.current = setTimeout(() => {
          getFileTree(code)
            .then((resp) => {
              const entries = resp.entries ?? [];
              const paths = entries.filter((e) => e.kind !== 'dir').map((e) => e.path);
              setTreeEntries(entries);
              // The open editor buffer is stale after reset (FS is back to
              // baseline). Re-read the open file from baseline, or drop the
              // selection if it was a file created during the session.
              const open = selectedPathRef.current;
              if (open === null) return;
              if (paths.includes(open)) {
                getFileContent(code, open)
                  .then((content) => setEditorContent(content))
                  .catch(() => {
                    // Re-read failed: don't leave the stale pre-reset buffer on
                    // screen. Drop the selection so the interviewer re-opens the
                    // file (which fetches fresh content) instead of trusting it.
                    setSelectedPath(null);
                    setEditorContent('');
                  });
              } else {
                setSelectedPath(null);
                setEditorContent('');
              }
            })
            .catch(() => {});
        }, 300);
      }
      if (!isCompleted && event.type === 'sandbox_restored') {
        // Sandbox FS replayed from event log after backend restart.
        // Show a transient banner and re-fetch the tree so the interviewer sees
        // restored paths without needing F5.
        setSandboxRestoredNotice(event.payload);
        if (sandboxRestoredTimerRef.current) clearTimeout(sandboxRestoredTimerRef.current);
        sandboxRestoredTimerRef.current = setTimeout(
          () => setSandboxRestoredNotice(null),
          5000,
        );
        if (treeDebounceRef.current) clearTimeout(treeDebounceRef.current);
        treeDebounceRef.current = setTimeout(() => {
          getFileTree(code)
            .then((resp) => setTreeEntries(resp.entries ?? []))
            .catch(() => {});
        }, 300);
      }

      if (
        !isCompleted &&
        event.type === 'file_changed' &&
        event.payload.file_path === selectedPathRef.current
      ) {
        if (typeof event.payload.content === 'string') {
          setEditorContent(event.payload.content);
        } else if (event.payload.content === undefined) {
          getFileContent(code, event.payload.file_path)
            .then((content) => setEditorContent(content))
            .catch(() => {});
        }
      }

      if (event.type === 'command_run') {
        setConsoleOpen(true);
      }
    }
  }, [state.events, code, isCompleted]);

  // Keep ref in sync for event handler closure.
  useEffect(() => {
    selectedPathRef.current = selectedPath;
  }, [selectedPath]);

  // Cleanup debounce on unmount.
  useEffect(() => {
    return () => {
      if (treeDebounceRef.current) clearTimeout(treeDebounceRef.current);
      if (sandboxRestoredTimerRef.current) clearTimeout(sandboxRestoredTimerRef.current);
    };
  }, []);

  const frozenAt = useMemo<number | null>(() => {
    // Freeze the clock once completed (live status flips to 'completed') or always
    // in public replay (the log may have no session_end to flip the status, and the
    // view is read-only). Live path keeps the original status-only gate.
    if (state.sessionStatus !== 'completed' && !isPublicReplay) return null;
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
  }, [state.sessionStatus, isPublicReplay, state.events]);

  const visibleEvents = useMemo(
    () => (isCompleted ? state.events.slice(0, cursor) : state.events),
    [isCompleted, state.events, cursor],
  );

  // Structural timeline over the full log; the cursor's event drives the
  // active highlight, so it's built from state.events (not the cursor slice).
  const timeline = useMemo(() => buildTimeline(state.events), [state.events]);
  const currentSeq =
    cursor > 0 && cursor <= state.events.length ? state.events[cursor - 1]!.seq : null;

  // Map an event seq → cursor index (1-based: visibleEvents = events.slice(0, cursor)),
  // so the cited event becomes the last visible one. null when the seq isn't in the log.
  const seqToCursor = useCallback(
    (seq: number): number | null => {
      const i = state.events.findIndex((e) => e.seq === seq);
      return i < 0 ? null : i + 1;
    },
    [state.events],
  );

  // Timeline row click → move the cursor (editor/tree/console/dialog reconstruct).
  const handleTimelineSelect = useCallback(
    (seq: number) => {
      const c = seqToCursor(seq);
      if (c !== null) setCursor(c);
    },
    [seqToCursor],
  );

  // Deep-link from an AI-assessment finding: move the cursor, open the
  // Timeline tab, and flash the target row.
  const handleJumpToSeq = useCallback(
    (seq: number) => {
      const c = seqToCursor(seq);
      if (c === null) return;
      setCursor(c);
      setRightTab('timeline');
      setHighlightSeq(seq);
    },
    [seqToCursor],
  );

  // Clear the transient flash after the animation ends so a later click on the same
  // finding (once the window has elapsed) re-triggers it.
  useEffect(() => {
    if (highlightSeq === null) return;
    const id = setTimeout(() => setHighlightSeq(null), 1600);
    return () => clearTimeout(id);
  }, [highlightSeq]);

  // During replay, derive tree paths and modified-badge set from events up to cursor
  // (handles op:'deleted' and sandbox_reset). For live sessions, REST treePaths +
  // liveModifiedPaths are used instead.
  const replayTree = useMemo(
    () => (isCompleted ? replayTreePaths(visibleEvents) : null),
    [isCompleted, visibleEvents],
  );
  const replayModified = useMemo<Set<string>>(
    () => (isCompleted ? replayModifiedPaths(visibleEvents) : new Set()),
    [isCompleted, visibleEvents],
  );

  const displayModifiedPaths = isCompleted ? replayModified : liveModifiedPaths;

  // Single aggregation of file_changed events — shared by selectedFileLog and any
  // future consumers, avoiding redundant O(N) scans on every cursor/event change.
  const fileLogs = useMemo(() => aggregateFileLogs(visibleEvents), [visibleEvents]);

  // File change history for the selected path — reads from the shared fileLogs memo.
  const selectedFileLog = useMemo(
    () => (selectedPath !== null ? fileLogs.get(selectedPath) ?? null : null),
    [selectedPath, fileLogs],
  );

  // For completed sessions: merge event-derived paths with REST baseline so scaffold
  // files that were never modified (no file_changed event) still appear in the tree.
  // REST baseline (treePaths) may be empty if the sandbox is gone — that's fine,
  // event-derived paths still work.
  const displayTreePaths = useMemo(() => {
    if (!isCompleted) return treePaths;
    const merged = new Set(replayTree ?? []);
    for (const p of treePaths) merged.add(p);
    return [...merged];
  }, [isCompleted, replayTree, treePaths]);

  // treeDirs (empty dirs) is REST-only — empty dirs are not projected from the event
  // log, so on a completed-session replay with no live sandbox they simply won't show.

  // Stable empty map so buildTree doesn't get a new Map() reference on every render.
  const emptyLogs = useMemo(() => new Map(), []);
  const fileTree = useMemo(
    () =>
      buildTree(
        emptyLogs,
        displayTreePaths.length > 0 ? displayTreePaths : undefined,
        treeDirs.length > 0 ? treeDirs : undefined,
      ),
    [emptyLogs, displayTreePaths, treeDirs],
  );

  const commandLogs = useMemo(() => aggregateCommands(visibleEvents), [visibleEvents]);

  // During replay, update editor when cursor moves (visibleEvents changes).
  useEffect(() => {
    if (!isCompleted || selectedPath === null) return;
    for (let i = visibleEvents.length - 1; i >= 0; i--) {
      const e = visibleEvents[i]!;
      if (e.type === 'file_changed' && e.payload.file_path === selectedPath) {
        if (typeof e.payload.content === 'string') setEditorContent(e.payload.content);
        break;
      }
    }
  }, [isCompleted, visibleEvents, selectedPath]);

  const handleSelectFile = useCallback(
    (path: string) => {
      setSelectedPath(path);
      setEditorTab('content');
      if (isCompleted) {
        // Replay: find last content for this path in visibleEvents.
        for (let i = visibleEvents.length - 1; i >= 0; i--) {
          const e = visibleEvents[i]!;
          if (e.type === 'file_changed' && e.payload.file_path === path) {
            setEditorContent(typeof e.payload.content === 'string' ? e.payload.content : '');
            return;
          }
        }
        setEditorContent('');
        return;
      }
      setEditorLoading(true);
      getFileContent(code, path)
        .then((content) => setEditorContent(content))
        .catch(() => setEditorContent(''))
        .finally(() => setEditorLoading(false));
    },
    [code, isCompleted, visibleEvents],
  );

  const handleToggleDir = useCallback((path: string) => {
    setCollapsedDirs((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }, []);

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

  const handleCopyCode = useCallback(async () => {
    if (state.config === null) return;
    try {
      await navigator.clipboard.writeText(state.config.session_code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // clipboard may be unavailable (insecure context, jsdom without user-gesture);
      // silently ignore — the user still has the visible code to select manually.
    }
  }, [state.config]);

  const handleEnd = useCallback(async () => {
    if (state.sessionStatus === 'completed') return;
    setEndState({ kind: 'pending' });
    try {
      await endSession(code);
      setEndState({ kind: 'idle' });
    } catch (err) {
      const message =
        err instanceof EndSessionError ? err.message :
        err instanceof Error ? err.message :
        String(err);
      setEndState({ kind: 'error', message });
    }
  }, [code, state.sessionStatus]);

  const handleReset = useCallback(async () => {
    setResetState('pending');
    try {
      await resetSandbox(code);
      setResetState('idle');
    } catch {
      setResetState('error');
      window.setTimeout(() => setResetState('idle'), 3000);
    }
  }, [code]);

  const handleShare = useCallback(async () => {
    const copyReplayUrl = async (token: string) => {
      const url = `${window.location.origin}/sessions/${code}/replay?share_token=${token}`;
      try {
        await navigator.clipboard.writeText(url);
        setShareLinkCopied(true);
        window.setTimeout(() => setShareLinkCopied(false), 1500);
      } catch {
        // ignore clipboard errors (insecure context / jsdom)
      }
    };
    if (typeof shareState === 'object') {
      await copyReplayUrl(shareState.shareToken);
      return;
    }
    if (shareState === 'loading') return;
    setShareState('loading');
    try {
      const resp = await postShare(code);
      setShareState({ shareToken: resp.share_token });
      await copyReplayUrl(resp.share_token);
    } catch {
      setShareState('error');
      window.setTimeout(() => setShareState('idle'), 3000);
    }
  }, [code, shareState]);

  const handleCopyCandidateLink = useCallback(async () => {
    if (!effectiveCandidateToken) return;
    const url = `${window.location.origin}/candidate?token=${effectiveCandidateToken}`;
    try {
      await navigator.clipboard.writeText(url);
      setCopiedCandidateLink(true);
      window.setTimeout(() => setCopiedCandidateLink(false), 1500);
    } catch {
      // ignore clipboard errors (insecure context / jsdom)
    }
  }, [effectiveCandidateToken]);

  if (load.kind === 'loading') {
    return (
      <main className={styles.page}>
        {!isPublicReplay && <Link to="/" className={styles.backLink}>{t('team.nav.back')}</Link>}
        <p className={styles.muted}>{isPublicReplay ? t('replay.loading') : t('session.loading')}</p>
      </main>
    );
  }

  if (load.kind === 'error') {
    return (
      <main className={styles.page}>
        {!isPublicReplay && <Link to="/" className={styles.backLink}>{t('team.nav.back')}</Link>}
        <header className={styles.header}>
          <h1>{load.title}</h1>
          <span className={styles.muted}>{code}</span>
        </header>
        <p className={styles.error}>{load.message}</p>
      </main>
    );
  }

  const config = state.config;
  if (config === null) return null;

  // `completed` = live status (from WS events/hello); `isCompleted` = load-time/replay value.
  const completed = state.sessionStatus === 'completed';
  const endDisabled = completed || endState.kind === 'pending';
  const statusLabel = isPublicReplay ? t('replay.status') : (state.sessionStatus ?? 'pending');
  const statusData = isPublicReplay ? 'completed' : (state.sessionStatus ?? 'pending');

  return (
    <TimeNowProvider frozenAt={frozenAt}>
      <MobileNotice />
      <div className={styles.appShell}>
        <header className={styles.header}>
          <div className={styles.headerLeft}>
            {!isPublicReplay && <Link to="/" className={styles.backLink}>{t('team.nav.back')}</Link>}
            <div className={styles.titleRow}>
              <span className={styles.titleText}>
                {isPublicReplay ? t('replay.title') : config.task_id}
              </span>
            </div>
            <div className={styles.subtitle}>
              {isPublicReplay && (
                <>
                  <span>{config.task_id}</span>
                  <span className={styles.dot}>·</span>
                </>
              )}
              <span>{config.ai_config.primary_model}</span>
              <span className={styles.dot}>·</span>
              <span data-testid="session-elapsed">
                {t('session.elapsed.prefix')} <Elapsed start={config.created_at} />
              </span>
              <span className={styles.dot}>·</span>
              <button
                type="button"
                className={styles.codeChip}
                onClick={handleCopyCode}
                title={copied ? t('session.copy.done') : t('session.copy.code')}
                data-testid="session-code-copy"
              >
                <code className={styles.codeInline}>{config.session_code}</code>
                <Copy aria-hidden="true" size={11} />
                <span className={styles.copySr}>
                  {copied ? t('session.copy.done') : t('session.copy.code')}
                </span>
              </button>
              {copied && (
                <span className={styles.copiedHint} aria-live="polite">
                  {t('session.copy.doneHint')}
                </span>
              )}
            </div>
          </div>
          <div className={styles.headerRight}>
            <span className={styles.status} data-status={statusData}>
              {statusLabel}
            </span>
            {!isPublicReplay && <ConnectionBadge state={state.connection} />}
            <LanguageSwitcher />
            {!isPublicReplay && !completed && effectiveCandidateToken && (
              <button
                type="button"
                className={styles.candidateLinkButton}
                onClick={handleCopyCandidateLink}
                data-testid="session-candidate-link-copy"
              >
                <Copy size={13} aria-hidden="true" />
                {copiedCandidateLink ? t('session.candidateLink.copied') : t('session.candidateLink.copy')}
              </button>
            )}
            {!isPublicReplay && !completed && (
              <button
                type="button"
                className={styles.resetButton}
                onClick={() => setConfirmReset(true)}
                disabled={resetState === 'pending'}
                data-testid="session-sandbox-reset-button"
              >
                {resetState === 'pending'
                  ? t('session.sandbox.resetting')
                  : resetState === 'error'
                    ? t('session.sandbox.resetError')
                    : t('session.sandbox.reset')}
              </button>
            )}
            {!isPublicReplay && (
              <button
                type="button"
                className={styles.shareButton}
                onClick={handleShare}
                // The backend only mints a share token for a completed session.
                disabled={shareState === 'loading' || !completed}
                title={!completed ? t('session.share.pendingHint') : undefined}
                data-testid="session-share-button"
              >
                <Share2 size={13} aria-hidden="true" />
                {shareState === 'loading'
                  ? t('session.share.loading')
                  : typeof shareState === 'object'
                    ? (shareLinkCopied ? t('session.share.copied') : t('session.share.copy'))
                    : shareState === 'error'
                      ? t('session.share.error')
                      : t('session.share.button')}
              </button>
            )}
            {!isPublicReplay && (
              <button
                type="button"
                className={styles.endButton}
                onClick={() => setConfirmEnd(true)}
                disabled={endDisabled}
                data-testid="session-end-button"
              >
                {endState.kind === 'pending'
                  ? t('session.end.confirming')
                  : completed
                    ? t('session.end.completed')
                    : t('session.end.button')}
              </button>
            )}
          </div>
        </header>
        <ConfirmDialog
          open={confirmEnd}
          onOpenChange={setConfirmEnd}
          title={t('session.end.confirmTitle')}
          description={t('session.end.confirm')}
          confirmLabel={t('session.end.button')}
          cancelLabel={t('common.cancel')}
          onConfirm={handleEnd}
          destructive
          testId="session-end-confirm"
        />
        <ConfirmDialog
          open={confirmReset}
          onOpenChange={setConfirmReset}
          title={t('session.sandbox.resetConfirmTitle')}
          description={t('session.sandbox.resetConfirm')}
          confirmLabel={t('session.sandbox.reset')}
          cancelLabel={t('common.cancel')}
          onConfirm={handleReset}
          destructive
          testId="session-sandbox-reset-confirm"
        />
        {endState.kind === 'error' && (
          <p className={styles.error} data-testid="session-end-error">
            {t('session.end.error', { message: endState.message })}
          </p>
        )}
        {/* Suppress the reconnect-loop 4409 after the interviewer ends the session:
            sessionStatus is already 'completed' via the session_end event, so the
            subsequent session_ended fatal is expected. CandidatePage intentionally
            shows this fatal — it is the candidate's "Session ended" screen. */}
        {state.fatal && !(state.sessionStatus === 'completed' && state.fatal.reason === 'session_ended') && (
          <p className={styles.error}>
            {t('session.fatal', { message: state.fatal.message || state.fatal.reason })}
          </p>
        )}

        {sandboxRestoredNotice && (
          <p
            className={styles.sandboxRestoredBanner}
            data-testid="session-sandbox-restored-banner"
            role="status"
          >
            {t('session.sandbox.restored', {
              count: sandboxRestoredNotice.file_count,
            })}
          </p>
        )}

        {/* Presence alert */}
        {presenceAlert && (
          <div
            className={styles.presenceAlert}
            role="alert"
            data-testid="presence-alert-badge"
          >
            {t('presence.alert.duplicateConnection')}
            <button
              type="button"
              className={styles.presenceAlertDismiss}
              onClick={() => setPresenceAlert(false)}
              aria-label="Dismiss"
            >
              ×
            </button>
          </div>
        )}

        {/* Presence panel */}
        {attendees.length > 0 && (
          <div className={styles.presencePanel} data-testid="presence-panel">
            <span className={styles.presencePanelTitle}>{t('presence.attendees.title')}:</span>
            {attendees.map((a) => (
              <span
                key={a.name}
                className={styles.presenceAttendee}
                data-active={a.is_active}
                data-role={a.role}
                title={a.is_active ? t('presence.attendee.online') : t('presence.attendee.offline')}
              >
                <span className={styles.presenceDot} />
                {a.name}
                {a.role === 'interviewer' && (
                  <span className={styles.presenceRoleTag} data-testid={`presence-role-${a.name}`}>
                    {t('presence.attendee.role.interviewer')}
                  </span>
                )}
              </span>
            ))}
          </div>
        )}

        {/* CSS Grid: [sidebar] [sb-resizer] [editor] [right-resizer] [right panel] */}
        <div
          className={styles.mainArea}
          style={{ gridTemplateColumns: `${sidebarWidth}px 4px 1fr 4px ${rightWidth}px` }}
        >
          {/* Col 1 Row 1: file tree */}
          <div className={styles.sidebarPanel}>
            <div className={styles.fileTreePanel} data-testid="session-file-tree">
              {displayTreePaths.length > 0 || treeDirs.length > 0 ? (
                <FileTree
                  tree={fileTree}
                  selectedPath={selectedPath}
                  collapsedDirs={collapsedDirs}
                  onSelect={handleSelectFile}
                  onToggleDir={handleToggleDir}
                  modifiedPaths={displayModifiedPaths}
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

          {/* Col 3 Row 1: Monaco editor (read-only) + file history tab */}
          <div className={styles.editorPanel}>
            {selectedPath !== null && (
              <div className={styles.editorTabBar}>
                <button
                  type="button"
                  className={styles.editorTabBtn}
                  data-active={editorTab === 'content'}
                  onClick={() => setEditorTab('content')}
                  data-testid="editor-tab-content"
                >
                  {t('session.file.tabContent')}
                </button>
                <button
                  type="button"
                  className={styles.editorTabBtn}
                  data-active={editorTab === 'history'}
                  onClick={() => setEditorTab('history')}
                  data-testid="editor-tab-history"
                >
                  {t('session.file.tabHistory')}
                </button>
              </div>
            )}
            {(editorTab === 'content' || selectedPath === null) && (
              <div className={styles.editorContent}>
                {editorLoading && (
                  <p className={styles.editorLoading}>{t('candidate.fileLoading')}</p>
                )}
                {!editorLoading && selectedPath !== null && (
                  <Suspense fallback={<p className={styles.editorLoading}>{t('candidate.fileLoading')}</p>}>
                    <MonacoEditor
                      path={selectedPath}
                      content={editorContent}
                      readOnly
                    />
                  </Suspense>
                )}
                {!editorLoading && selectedPath === null && (
                  <p className={styles.editorPlaceholder}>{t('candidate.selectFile')}</p>
                )}
              </div>
            )}
            {editorTab === 'history' && selectedPath !== null && (
              <div className={styles.historyPane}>
                {selectedFileLog !== null ? (
                  <FileDiffHistory
                    entries={selectedFileLog.entries}
                    path={selectedPath}
                    defaultOpen
                  />
                ) : (
                  <p className={styles.editorPlaceholder}>{t('session.file.historyEmpty')}</p>
                )}
              </div>
            )}
          </div>

          {/* Col 4 Row 1: right panel resize handle */}
          <div
            className={styles.rightResizer}
            onMouseDown={handleRightResizerMouseDown}
            title="Drag to resize"
          />

          {/* Col 5 Rows 1-2: AI dialog + timeline (completed/replay) + interviewer-only
              assessment tab. Timeline needs a cursor → only for completed sessions;
              assessment is interviewer-only (never in public replay). */}
          <div className={styles.rightPanel}>
            <div className={styles.editorTabBar} data-testid="right-tab-bar">
              <button
                type="button"
                className={styles.editorTabBtn}
                data-active={rightTab === 'dialog'}
                onClick={() => setRightTab('dialog')}
                data-testid="right-tab-dialog"
              >
                {t('session.rightTab.dialog')}
              </button>
              {isCompleted && (
                <button
                  type="button"
                  className={styles.editorTabBtn}
                  data-active={rightTab === 'timeline'}
                  onClick={() => setRightTab('timeline')}
                  data-testid="right-tab-timeline"
                >
                  {t('session.rightTab.timeline')}
                </button>
              )}
              {!isPublicReplay && (
                <button
                  type="button"
                  className={styles.editorTabBtn}
                  data-active={rightTab === 'assessment'}
                  onClick={() => setRightTab('assessment')}
                  data-testid="right-tab-assessment"
                >
                  {t('session.rightTab.assessment')}
                </button>
              )}
            </div>
            {rightTab === 'timeline' && isCompleted ? (
              <div className={styles.timelineArea} data-testid="session-timeline-panel">
                <Timeline
                  phases={timeline}
                  currentSeq={currentSeq}
                  highlightSeq={highlightSeq}
                  onSelect={handleTimelineSelect}
                />
              </div>
            ) : rightTab === 'assessment' && !isPublicReplay ? (
              <div className={styles.assessmentArea} data-testid="session-assessment-panel">
                <AssessmentTab
                  code={config.session_code}
                  sessionCompleted={state.sessionStatus === 'completed'}
                  onJumpToSeq={handleJumpToSeq}
                />
              </div>
            ) : (
              <div className={styles.chatArea} data-testid="session-chat-panel">
                <DialogTab
                  events={visibleEvents}
                  sessionActive={!isPublicReplay && state.sessionStatus !== 'completed'}
                  sessionCode={config.session_code}
                  sessionStatus={isPublicReplay ? 'completed' : state.sessionStatus}
                  candidateToken={effectiveCandidateToken}
                />
              </div>
            )}
          </div>

          {/* Cols 1-4 Row 2: console output (spans sidebar + resizer + editor + resizer) */}
          <ConsoleSection
            className={styles.consoleSection}
            commandLogs={commandLogs}
            open={consoleOpen}
            onToggle={() => setConsoleOpen((o) => !o)}
            testIdPrefix="session"
          />
        </div>

        {isCompleted && (
          <ReplayTimeline
            total={state.events.length}
            cursor={cursor}
            events={state.events}
            onCursor={setCursor}
          />
        )}
      </div>
    </TimeNowProvider>
  );
}

function errorTitle(
  t: ReturnType<typeof useTranslation>['t'],
  reason: SessionConfigError['reason'] | ReplayConfigError['reason'],
): string {
  switch (reason) {
    case 'unauthorized':
      return t('error.title.unauthorized');
    case 'not_found':
      return t('error.title.notFound');
    case 'completed':
      return t('error.title.completed');
    case 'not_completed':
      return t('replay.error.notCompleted');
    case 'network':
      return t('error.title.network');
    case 'invalid_response':
      return t('error.title.invalidResponse');
    default:
      return t('error.title.unknown');
  }
}
