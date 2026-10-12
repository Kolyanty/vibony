import { useMemo, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import {
  parseDiff,
  type DiffHunk,
  type DiffLine,
  type DiffSegment,
  type LineKind,
} from './diffViewUtils';
import styles from './DiffView.module.css';

export type DiffMode = 'auto' | 'unified' | 'side-by-side';

interface DiffViewProps {
  diffText: string;
  mode?: DiffMode;
}

function resolveInitialMode(mode: DiffMode): 'unified' | 'side-by-side' {
  return mode === 'side-by-side' ? 'side-by-side' : 'unified';
}

// ---------------------------------------------------------------------------
// Public component
// ---------------------------------------------------------------------------

export function DiffView({ diffText, mode = 'auto' }: DiffViewProps) {
  const { t } = useTranslation();
  const parsed = useMemo(() => parseDiff(diffText), [diffText]);
  const [activeMode, setActiveMode] = useState<'unified' | 'side-by-side'>(
    () => resolveInitialMode(mode),
  );

  return (
    <div className={styles.container} data-mode={activeMode}>
      {parsed.fileHeaders.length > 0 ? (
        <div className={styles.fileHeaders}>
          {parsed.fileHeaders.map((h, i) => (
            <div key={i} className={styles.fileHeader}>
              {h}
            </div>
          ))}
        </div>
      ) : null}
      <div className={styles.toolbar} data-testid="diff-mode-toggle">
        <button
          className={styles.modeButton}
          data-active={activeMode === 'unified'}
          data-testid="diff-mode-unified"
          onClick={() => setActiveMode('unified')}
        >
          {t('diff.toolbar.unified')}
        </button>
        <button
          className={styles.modeButton}
          data-active={activeMode === 'side-by-side'}
          data-testid="diff-mode-sbs"
          onClick={() => setActiveMode('side-by-side')}
        >
          {t('diff.toolbar.sbs')}
        </button>
      </div>
      {parsed.changedCount === 0 ? (
        <div className={styles.empty} data-testid="diff-empty">
          {t('diff.empty')}
        </div>
      ) : activeMode === 'unified' ? (
        <UnifiedView hunks={parsed.hunks} />
      ) : (
        <SideBySideView hunks={parsed.hunks} />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Unified renderer
// ---------------------------------------------------------------------------

function UnifiedView({ hunks }: { hunks: DiffHunk[] }) {
  return (
    <div className={styles.unified}>
      {hunks.map((h, hi) => (
        <div key={hi} className={styles.hunk}>
          <div className={styles.hunkHeader}>{h.header}</div>
          {h.lines.map((line, li) => (
            <UnifiedRow key={li} line={line} />
          ))}
        </div>
      ))}
    </div>
  );
}

function UnifiedRow({ line }: { line: DiffLine }) {
  const prefix = signFor(line.kind);
  return (
    <div className={styles.unifiedRow} data-kind={line.kind}>
      <span className={styles.gutter} aria-hidden="true">
        {line.oldNo ?? ''}
      </span>
      <span className={styles.gutter} aria-hidden="true">
        {line.newNo ?? ''}
      </span>
      <span className={styles.sign} aria-hidden="true">
        {prefix}
      </span>
      <span className={styles.content}>{renderSegments(line.segments, line.text)}</span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Side-by-side renderer
// ---------------------------------------------------------------------------

interface SBSCellData {
  kind: LineKind;
  text: string;
  lineNo: number | null;
  segments?: DiffSegment[];
}

interface SBSRow {
  left: SBSCellData | null;
  right: SBSCellData | null;
}

function toSBSRows(hunk: DiffHunk): SBSRow[] {
  const rows: SBSRow[] = [];
  let i = 0;
  const lines = hunk.lines;
  while (i < lines.length) {
    const l = lines[i];
    if (l.kind === 'context') {
      rows.push({
        left: { kind: 'context', text: l.text, lineNo: l.oldNo, segments: l.segments },
        right: { kind: 'context', text: l.text, lineNo: l.newNo, segments: l.segments },
      });
      i++;
      continue;
    }
    const removes: DiffLine[] = [];
    while (i < lines.length && lines[i].kind === 'remove') {
      removes.push(lines[i]);
      i++;
    }
    const adds: DiffLine[] = [];
    while (i < lines.length && lines[i].kind === 'add') {
      adds.push(lines[i]);
      i++;
    }
    const max = Math.max(removes.length, adds.length);
    for (let k = 0; k < max; k++) {
      const r = removes[k];
      const a = adds[k];
      rows.push({
        left: r
          ? { kind: 'remove', text: r.text, lineNo: r.oldNo, segments: r.segments }
          : null,
        right: a
          ? { kind: 'add', text: a.text, lineNo: a.newNo, segments: a.segments }
          : null,
      });
    }
  }
  return rows;
}

function SideBySideView({ hunks }: { hunks: DiffHunk[] }) {
  return (
    <div className={styles.sbs}>
      {hunks.map((h, hi) => {
        const rows = toSBSRows(h);
        return (
          <div key={hi} className={styles.hunk}>
            <div className={styles.hunkHeader}>{h.header}</div>
            {rows.map((row, ri) => (
              <div key={ri} className={styles.sbsRow}>
                <SBSCell side="left" cell={row.left} />
                <SBSCell side="right" cell={row.right} />
              </div>
            ))}
          </div>
        );
      })}
    </div>
  );
}

function SBSCell({ side, cell }: { side: 'left' | 'right'; cell: SBSCellData | null }) {
  if (!cell) {
    return (
      <div className={styles.sbsCell} data-kind="empty" data-side={side} aria-hidden="true" />
    );
  }
  const prefix = signFor(cell.kind);
  return (
    <div className={styles.sbsCell} data-kind={cell.kind} data-side={side}>
      <span className={styles.gutter} aria-hidden="true">
        {cell.lineNo ?? ''}
      </span>
      <span className={styles.sign} aria-hidden="true">
        {prefix}
      </span>
      <span className={styles.content}>{renderSegments(cell.segments, cell.text)}</span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function signFor(kind: LineKind): string {
  if (kind === 'add') return '+';
  if (kind === 'remove') return '-';
  return ' ';
}

function renderSegments(segments: DiffSegment[] | undefined, fallback: string): ReactNode {
  if (segments && segments.length > 0) {
    return segments.map((s, i) =>
      s.emphasis ? (
        <span key={i} className={styles.emphasis} data-emphasis="true">
          {s.text}
        </span>
      ) : (
        <span key={i}>{s.text}</span>
      ),
    );
  }
  return fallback;
}
