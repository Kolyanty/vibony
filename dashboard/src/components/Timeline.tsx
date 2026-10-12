import { useEffect, useMemo, useRef } from 'react';
import type { TFunction } from 'i18next';
import { useTranslation } from 'react-i18next';
import type { TimelineEntry, TimelinePhase } from './timelineUtils';
import { resolveActiveSeq } from './timelineUtils';
import styles from './Timeline.module.css';

interface TimelineProps {
  phases: TimelinePhase[];
  /** Seq of the cursor's event (last visible). Drives the persistent active highlight. */
  currentSeq: number | null;
  /** Deep-link target — drives a transient flash distinct from the active highlight. */
  highlightSeq: number | null;
  onSelect: (seq: number) => void;
}

function describeEntry(t: TFunction, entry: TimelineEntry): { label: string; text: string } {
  switch (entry.kind) {
    case 'session_start':
      return { label: t('timeline.entry.sessionStart'), text: entry.text };
    case 'session_end':
      return { label: t('timeline.entry.sessionEnd'), text: entry.text };
    case 'assistant_message':
      return { label: t('timeline.entry.assistant'), text: entry.text };
    case 'tool':
      return { label: entry.text, text: '' };
    case 'tool_group':
      return { label: t('timeline.entry.toolGroup', { count: entry.count ?? 1 }), text: '' };
    case 'command':
      return { label: t('timeline.entry.command'), text: entry.text };
    case 'file_saved':
      return { label: t('timeline.entry.fileSaved'), text: entry.text };
    case 'file_deleted':
      return { label: t('timeline.entry.fileDeleted'), text: entry.text };
    case 'diff_proposed':
      return { label: t('timeline.entry.diffProposed'), text: entry.text };
    case 'diff_accepted':
      return { label: t('timeline.entry.diffAccepted'), text: '' };
    case 'diff_rejected':
      return { label: t('timeline.entry.diffRejected'), text: entry.text };
    case 'model_switched':
      return { label: t('timeline.entry.modelSwitched'), text: entry.text };
    case 'sandbox_reset':
      return { label: t('timeline.entry.sandboxReset'), text: '' };
    case 'sandbox_restored':
      return { label: t('timeline.entry.sandboxRestored', { count: entry.text }), text: '' };
    default:
      return { label: entry.kind, text: entry.text };
  }
}

/**
 * Structural session timeline. Vertical list of phases + key events; clicking
 * a row calls `onSelect(seq)` (the parent moves the replay cursor). The active row
 * (cursor position) is highlighted and scrolled into view as the cursor moves;
 * `highlightSeq` flashes a deep-link target.
 */
export function Timeline({ phases, currentSeq, highlightSeq, onSelect }: TimelineProps) {
  const { t } = useTranslation();
  const containerRef = useRef<HTMLDivElement>(null);

  const activeSeq = useMemo(() => resolveActiveSeq(phases, currentSeq), [phases, currentSeq]);
  const flashSeq = useMemo(() => resolveActiveSeq(phases, highlightSeq), [phases, highlightSeq]);

  // Scroll the relevant row into view: a deep-link flash target takes priority,
  // otherwise follow the cursor's active row (scrubber/play/jump). Previously
  // this always scrolled to activeSeq while depending on flashSeq — latent, it
  // only worked because a deep-link moves the cursor first.
  useEffect(() => {
    const target = flashSeq ?? activeSeq;
    if (target === null) return;
    const el = containerRef.current?.querySelector<HTMLElement>(`[data-seq="${target}"]`);
    el?.scrollIntoView({ block: 'nearest' });
  }, [activeSeq, flashSeq]);

  if (phases.length === 0) {
    return (
      <div className={styles.empty} data-testid="timeline-empty">
        {t('timeline.empty')}
      </div>
    );
  }

  return (
    <div className={styles.root} ref={containerRef} data-testid="timeline">
      {phases.map((phase, pi) => (
        <div key={phase.headerSeq ?? `setup-${pi}`} className={styles.phase}>
          <button
            type="button"
            className={styles.phaseHeader}
            data-seq={phase.headerSeq ?? undefined}
            data-active={phase.headerSeq !== null && phase.headerSeq === activeSeq}
            data-flash={phase.headerSeq !== null && phase.headerSeq === flashSeq}
            disabled={phase.headerSeq === null}
            onClick={() => phase.headerSeq !== null && onSelect(phase.headerSeq)}
          >
            <span className={styles.phaseLabel}>{phase.label ?? t('timeline.setup')}</span>
          </button>
          <ul className={styles.entries}>
            {phase.entries.map((entry) => {
              const { label, text } = describeEntry(t, entry);
              return (
                <li key={entry.seq}>
                  <button
                    type="button"
                    className={styles.entry}
                    data-seq={entry.seq}
                    data-active={entry.seq === activeSeq}
                    data-flash={entry.seq === flashSeq}
                    data-kind={entry.kind}
                    onClick={() => onSelect(entry.seq)}
                  >
                    <span className={styles.entryLabel}>{label}</span>
                    {text && <span className={styles.entryText}>{text}</span>}
                    {entry.kind === 'command' && entry.exitCode !== undefined && (
                      <span className={styles.exit} data-fail={entry.exitCode !== 0}>
                        {t('timeline.exit', { code: entry.exitCode })}
                      </span>
                    )}
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </div>
  );
}
