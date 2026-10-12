import { Suspense, lazy, useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { formatDuration, type CommandLog } from './consoleTabUtils';
import { isNearBottom } from './scrollUtils';
import styles from './ConsoleSection.module.css';

const XtermView = lazy(() => import('./XtermView'));

interface ConsoleSectionProps {
  /** CSS class for the outer div — used by the page to set grid-column/row and border-top. */
  className?: string;
  commandLogs: CommandLog[];
  open: boolean;
  onToggle: () => void;
  testIdPrefix: string;
  /** Optional slot rendered after the console body (e.g. terminal input bar). */
  terminalBar?: React.ReactNode;
}

export function ConsoleSection({
  className,
  commandLogs,
  open,
  onToggle,
  testIdPrefix,
  terminalBar,
}: ConsoleSectionProps) {
  const { t } = useTranslation();
  const [consoleHeight, setConsoleHeight] = useState(240);
  const consoleHeightRef = useRef(consoleHeight);
  const bodyRef = useRef<HTMLDivElement>(null);
  const atBottomRef = useRef(true);

  useEffect(() => {
    consoleHeightRef.current = consoleHeight;
  }, [consoleHeight]);

  // On open, jump to the latest output only if the user was last at the bottom.
  // We deliberately do NOT reset atBottomRef here: a user who scrolled up keeps
  // that intent across a close/reopen (incl. a command_run auto-open), so new
  // output won't yank them back down (D-9). On the very first open atBottomRef
  // defaults to true → we follow.
  useEffect(() => {
    if (!open || !bodyRef.current) return;
    if (atBottomRef.current) {
      bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
    }
  }, [open]);

  // Follow new output only while already at the bottom.
  // bodyRef.current is null when closed, so the null-check covers !open implicitly.
  useEffect(() => {
    if (!atBottomRef.current || !bodyRef.current) return;
    bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
  }, [commandLogs]);

  const handleResizerMouseDown = useCallback((e: React.MouseEvent) => {
    if (!open) return;
    e.preventDefault();
    const startY = e.clientY;
    const startH = consoleHeightRef.current;
    const onMove = (ev: MouseEvent) =>
      setConsoleHeight(Math.max(80, Math.min(600, startH + startY - ev.clientY)));
    const onUp = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  }, [open]);

  return (
    <div
      className={`${styles.root}${className ? ` ${className}` : ''}`}
      data-testid={`${testIdPrefix}-console-section`}
    >
      <div
        className={styles.consoleResizer}
        data-open={open ? '' : undefined}
        onMouseDown={handleResizerMouseDown}
        title="Drag to resize console"
      />
      <button
        type="button"
        className={styles.consoleSectionToggle}
        onClick={onToggle}
        data-testid={`${testIdPrefix}-console-toggle`}
      >
        {t('candidate.console.toggle', { count: commandLogs.length })}
        <span className={styles.consoleChevron}>{open ? '▲' : '▼'}</span>
      </button>
      {open && (
        <div
          ref={bodyRef}
          className={styles.consoleSectionBody}
          style={{ height: consoleHeight }}
          data-testid={`${testIdPrefix}-console-body`}
          onScroll={() => {
            if (bodyRef.current) atBottomRef.current = isNearBottom(bodyRef.current);
          }}
        >
          {commandLogs.length === 0 ? (
            <p className={styles.consoleEmpty}>{t('candidate.console.empty')}</p>
          ) : (
            commandLogs.map((log) => (
              <div
                key={log.commandId}
                className={styles.consoleCommand}
                data-testid={`console-command-${log.commandId}`}
              >
                <div className={styles.consoleCommandHeader}>
                  <span className={styles.consoleCommandText}>
                    $ {log.runEvent.payload.command}
                  </span>
                  {log.finished && (
                    <span
                      className={styles.consoleCommandMeta}
                      data-fail={log.finished.payload.exit_code !== 0}
                      data-testid={`console-command-meta-${log.commandId}`}
                    >
                      {t('console.finished', {
                        code: log.finished.payload.exit_code,
                        duration: formatDuration(log.finished.payload.duration_ms),
                      })}
                    </span>
                  )}
                </div>
                {log.chunks.length > 0 && (
                  <Suspense fallback={null}>
                    <XtermView chunks={log.chunks} />
                  </Suspense>
                )}
              </div>
            ))
          )}
        </div>
      )}
      {terminalBar}
    </div>
  );
}
