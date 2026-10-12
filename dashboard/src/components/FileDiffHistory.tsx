import { useEffect, useMemo, useRef, useState } from 'react';
import { createPatch } from 'diff';
import { useTranslation } from 'react-i18next';
import { oneLight } from 'react-syntax-highlighter/dist/esm/styles/prism';
import { prism as SyntaxHighlighter } from '../lib/prism';
import { RelativeTime } from './RelativeTime';
import type { FileChangeEntry } from './filesTabUtils';
import styles from './FileDiffHistory.module.css';

// Files larger than this threshold skip auto-diff to avoid blocking the render.
const LARGE_FILE_LINE_THRESHOLD = 500;

function lineCount(content: string | null | undefined): number {
  return typeof content === 'string' ? content.split('\n').length : 0;
}

// A computed diff that carries no add/remove lines (identical content, or an
// empty patch) renders as "no changes" — distinct from never having a diff at
// all. `+++`/`---` are file headers, not changes.
function diffHasChanges(diff: string): boolean {
  return diff.split('\n').some(
    (l) =>
      (l.startsWith('+') && !l.startsWith('+++')) ||
      (l.startsWith('-') && !l.startsWith('---')),
  );
}

interface FileDiffHistoryProps {
  entries: FileChangeEntry[];
  /** File path — used as filename in computed diffs. */
  path: string;
  /** Start expanded. Default false (collapsed). */
  defaultOpen?: boolean;
}

function resolveDiff(
  entry: FileChangeEntry,
  prevContent: string | null | undefined,
  path: string,
): string | null {
  if (entry.diff) return entry.diff;
  if (typeof entry.content === 'string') {
    const before = typeof prevContent === 'string' ? prevContent : '';
    return createPatch(path, before, entry.content);
  }
  return null;
}

type DiffEntry = FileChangeEntry & {
  kind: 'diff';
  resolvedDiff: string | null;
  prevContent: string | null | undefined;
  isLarge: boolean;
  maxLines: number;
};

// A reset boundary, derived from a non-first baseline event (the backend
// re-emits baselines on sandbox_reset). Marks where the diff chain restarts
// from clean scaffold, so the interviewer can tell pre- from post-reset edits.
type ResetMarker = { kind: 'reset'; key: string };

type HistoryItem = DiffEntry | ResetMarker;

// Schedule work during browser idle time; fall back to setTimeout when unavailable
// (e.g. jsdom in tests). Returns a cancel function.
function scheduleIdleWork(fn: () => void): () => void {
  if (typeof requestIdleCallback !== 'undefined') {
    // timeout: 2000ms ensures the diff runs even if the browser stays busy
    const id = requestIdleCallback(fn, { timeout: 2000 });
    return () => cancelIdleCallback(id);
  }
  const id = setTimeout(fn, 0);
  return () => clearTimeout(id);
}

export function FileDiffHistory({ entries, path, defaultOpen = false }: FileDiffHistoryProps) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(defaultOpen);
  // eventId → diff string, populated when user explicitly requests a large-file diff
  const [manualDiffs, setManualDiffs] = useState<Map<string, string>>(() => new Map());
  // eventIds whose diff is currently being computed (shows "Computing…" state)
  const [pendingIds, setPendingIds] = useState<Set<string>>(() => new Set());
  // cancel functions keyed by eventId — called on unmount to prevent setState on dead component
  const cancelRef = useRef<Map<string, () => void>>(new Map());

  useEffect(() => {
    const cancels = cancelRef.current;
    return () => {
      cancels.forEach((cancel) => cancel());
    };
  }, []);

  // Walk entries in chronological order, building consecutive diffs. Each
  // baseline event reseeds the "before" pointer; the first baseline is the
  // initial scaffold, every later one is a sandbox reset → emit a divider and
  // restart the diff chain from clean scaffold. Reverse for display (newest
  // first). Large entries skip diff computation.
  const { items, diffCount } = useMemo((): { items: HistoryItem[]; diffCount: number } => {
    let prevContent: string | null | undefined;
    let seenBaseline = false;
    let diffs = 0;

    const result: HistoryItem[] = [];
    for (const entry of entries) {
      if (entry.source === 'baseline') {
        if (seenBaseline) {
          result.push({ kind: 'reset', key: `reset-${entry.eventId}` });
        }
        seenBaseline = true;
        prevContent = entry.content;
        continue;
      }
      const maxLines = Math.max(lineCount(entry.content), lineCount(prevContent));
      const large = maxLines > LARGE_FILE_LINE_THRESHOLD;
      result.push({
        ...entry,
        kind: 'diff',
        prevContent,
        isLarge: large,
        maxLines,
        // Skip diff computation for large files — user requests it on demand.
        resolvedDiff: large ? null : resolveDiff(entry, prevContent, path),
      });
      diffs += 1;
      // Advance the "before" pointer only when we have concrete content.
      // null (deleted) → next diff will diff from empty string.
      if (typeof entry.content === 'string' || entry.content === null) {
        prevContent = entry.content;
      }
    }
    return { items: result.slice().reverse(), diffCount: diffs };
  }, [entries, path]);

  // No real edits → nothing to show (a file reset but never touched is not history).
  if (diffCount === 0) {
    return null;
  }

  function handleComputeDiff(entry: DiffEntry) {
    setPendingIds((prev) => new Set(prev).add(entry.eventId));

    const cancel = scheduleIdleWork(() => {
      cancelRef.current.delete(entry.eventId);
      const diff = resolveDiff(entry, entry.prevContent, path) ?? '';
      setManualDiffs((prev) => {
        const next = new Map(prev);
        next.set(entry.eventId, diff);
        return next;
      });
      setPendingIds((prev) => {
        const next = new Set(prev);
        next.delete(entry.eventId);
        return next;
      });
    });

    cancelRef.current.set(entry.eventId, cancel);
  }

  return (
    <section className={styles.root}>
      <button
        type="button"
        className={styles.toggle}
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        data-testid="files-history-toggle"
      >
        <span className={styles.chevron}>{open ? '▼' : '▶'}</span>
        <span>{t('files.history.toggle', { count: diffCount })}</span>
      </button>
      {open ? (
        <ul className={styles.list}>
          {items.map((entry) => {
            if (entry.kind === 'reset') {
              return (
                <li key={entry.key} className={styles.resetDivider} data-testid="history-reset-divider">
                  <span className={styles.resetLabel}>{t('files.history.sandboxReset')}</span>
                </li>
              );
            }
            const displayDiff = entry.resolvedDiff ?? manualDiffs.get(entry.eventId) ?? null;
            const isPending = pendingIds.has(entry.eventId);
            const showLargePlaceholder =
              entry.isLarge && !manualDiffs.has(entry.eventId) && !isPending;
            // A diff string was produced (even an empty one) vs none ever available.
            const diffComputed = entry.resolvedDiff !== null || manualDiffs.has(entry.eventId);
            const hasChanges = displayDiff !== null && diffHasChanges(displayDiff);
            return (
              <li key={entry.eventId} className={styles.entry}>
                <div className={styles.meta}>
                  <RelativeTime timestamp={entry.timestamp} />
                  <span className={styles.metaDot}>·</span>
                  <span className={styles.source} data-source={entry.source}>
                    {entry.source}
                  </span>
                </div>
                {isPending ? (
                  <p className={styles.computingDiff}>{t('files.history.computingDiff')}</p>
                ) : showLargePlaceholder ? (
                  <div className={styles.largePlaceholder}>
                    <span className={styles.largeLabel}>
                      {t('files.history.largeFile', { lines: entry.maxLines })}
                    </span>
                    <button
                      type="button"
                      className={styles.computeDiffBtn}
                      onClick={() => handleComputeDiff(entry)}
                      data-testid="compute-diff-btn"
                    >
                      {t('files.history.computeDiff')}
                    </button>
                  </div>
                ) : hasChanges ? (
                  <SyntaxHighlighter
                    language="diff"
                    style={oneLight}
                    PreTag="div"
                    customStyle={{
                      margin: 0,
                      padding: '8px 10px',
                      background: 'transparent',
                      borderRadius: 4,
                      fontSize: '12px',
                      lineHeight: 1.45,
                      maxHeight: 320,
                      overflow: 'auto',
                    }}
                    codeTagProps={{
                      style: {
                        fontFamily: 'var(--font-mono)',
                        background: 'transparent',
                      },
                    }}
                  >
                    {displayDiff!}
                  </SyntaxHighlighter>
                ) : diffComputed ? (
                  <p className={styles.emptyDiff}>{t('files.history.noChanges')}</p>
                ) : (
                  <p className={styles.emptyDiff}>{t('files.history.emptyDiff')}</p>
                )}
              </li>
            );
          })}
        </ul>
      ) : null}
    </section>
  );
}
