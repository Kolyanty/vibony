import { useState, type ReactNode } from 'react';
import { createPatch } from 'diff';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';
import { oneLight } from 'react-syntax-highlighter/dist/esm/styles/prism';
import { prism as SyntaxHighlighter, resolveLang } from '../lib/prism';
import type { EventEnvelope, ToolCallPayload, ToolResultPayload, PermissionDecision } from '../api/types';
import { Avatar } from './Avatar';
import { DiffView } from './DiffView';
import { RelativeTime } from './RelativeTime';
import styles from './ToolCallBlock.module.css';

type ToolCallEvent = Extract<EventEnvelope, { type: 'tool_call' }>;
type ToolResultEvent = Extract<EventEnvelope, { type: 'tool_result' }>;

interface ToolCallBlockProps {
  call: ToolCallEvent;
  result?: ToolResultEvent;
  isLatest?: boolean;
}

// Exhaustive map ensures every non-auto_allow PermissionDecision has an i18n key.
// TypeScript will error if a new decision value is added without a key here.
const PERMISSION_BADGE_KEYS = {
  user_allow: 'tool.bash.permission.user_allow',
  user_deny: 'tool.bash.permission.user_deny',
  policy_deny: 'tool.bash.permission.policy_deny',
} as const satisfies Record<Exclude<PermissionDecision, 'auto_allow'>, string>;

// Thresholds for "heavy" output. When the block is the last one in the dialog
// (isLatest), the thresholds are ignored — the interviewer sees the latest
// output expanded right away.
const EDIT_DIFF_COLLAPSE_THRESHOLD = 20;
const BASH_STREAM_COLLAPSE_THRESHOLD = 30;

// ---------------------------------------------------------------------------
// Public component
// ---------------------------------------------------------------------------

export function ToolCallBlock({ call, result, isLatest = false }: ToolCallBlockProps) {
  const { t } = useTranslation();
  const header = renderHeader(t, call.payload);
  return (
    <div className={styles.row}>
      <Avatar role="tool" size="sm" />
      <div className={styles.bubble}>
        <div className={styles.meta}>
          <span className={styles.role}>{t('tool.role')}</span>
          <span className={styles.toolName}>{call.payload.tool_name}</span>
          <RelativeTime className={styles.time} timestamp={call.timestamp} />
        </div>
        <div className={styles.header}>{header}</div>
        {result ? renderResult(t, call.payload, result.payload, isLatest) : (
          <span className={styles.pending}>{t('tool.pending')}</span>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Header (tool_call payload)
// ---------------------------------------------------------------------------

function renderHeader(t: TFunction, p: ToolCallPayload) {
  const input = p.input ?? {};
  switch (p.tool_name) {
    case 'Read':
      return renderReadHeader(input);
    case 'Edit':
      return renderEditHeader(input);
    case 'Write':
      return renderWriteHeader(input);
    case 'Bash':
      return renderBashHeader(t, input, p.permission_decision);
    case 'Glob':
      return renderGlobHeader(input);
    case 'Grep':
      return renderGrepHeader(input);
    default:
      return renderFallbackHeader(input);
  }
}

function renderReadHeader(input: Record<string, unknown>) {
  const path = strOrEmpty(input.path);
  const offset = numOrNull(input.offset);
  const limit = numOrNull(input.limit);
  let range = '';
  if (offset !== null || limit !== null) {
    const start = (offset ?? 0) + 1;
    if (limit !== null) {
      range = ` (lines ${start}–${start + limit - 1})`;
    } else {
      range = ` (from line ${start})`;
    }
  }
  return (
    <code className={styles.headerLine}>
      <span className={styles.arrow}>→ </span>read {path}
      {range}
    </code>
  );
}

function renderEditHeader(input: Record<string, unknown>) {
  const path = strOrEmpty(input.path);
  const replaceAll = Boolean(input.replace_all);
  return (
    <code className={styles.headerLine}>
      <span className={styles.arrow}>→ </span>edit {path}
      {replaceAll ? <span className={styles.flag}> (replace_all)</span> : null}
    </code>
  );
}

function renderWriteHeader(input: Record<string, unknown>) {
  const path = strOrEmpty(input.path);
  return (
    <code className={styles.headerLine}>
      <span className={styles.arrow}>→ </span>write {path}
    </code>
  );
}

function renderBashHeader(
  t: TFunction,
  input: Record<string, unknown>,
  decision?: PermissionDecision,
) {
  const command = strOrEmpty(input.command);
  const cwd = strOrEmpty(input.cwd);
  const showBadge = decision && decision !== 'auto_allow';
  return (
    <div>
      <div className={styles.bashHeaderLine}>
        <SyntaxHighlighter
          language="bash"
          style={oneLight}
          PreTag="div"
          customStyle={preStyle}
          codeTagProps={codeStyleProps}
        >
          {`$ ${command}`}
        </SyntaxHighlighter>
        {showBadge ? (
          <span
            className={styles.permissionBadge}
            data-decision={decision}
            role="status"
          >
            {t(PERMISSION_BADGE_KEYS[decision])}
          </span>
        ) : null}
      </div>
      {cwd && cwd !== '.' ? (
        <div className={styles.cwd}>cwd: {cwd}</div>
      ) : null}
    </div>
  );
}

function renderGlobHeader(input: Record<string, unknown>) {
  const pattern = strOrEmpty(input.pattern);
  return (
    <code className={styles.headerLine}>
      <span className={styles.arrow}>→ </span>glob {pattern}
    </code>
  );
}

function renderGrepHeader(input: Record<string, unknown>) {
  const pattern = strOrEmpty(input.pattern);
  const path = strOrEmpty(input.path);
  const glob = strOrEmpty(input.glob);
  const filters: string[] = [];
  if (path) filters.push(`in ${path}`);
  if (glob) filters.push(`glob=${glob}`);
  return (
    <code className={styles.headerLine}>
      <span className={styles.arrow}>→ </span>grep {pattern}
      {filters.length > 0 ? <span className={styles.dim}> {filters.join(' ')}</span> : null}
    </code>
  );
}

function renderFallbackHeader(input: Record<string, unknown>) {
  return <pre className={styles.fallbackInput}>{JSON.stringify(input, null, 2)}</pre>;
}

// ---------------------------------------------------------------------------
// Result (tool_result payload)
// ---------------------------------------------------------------------------

function renderResult(t: TFunction, call: ToolCallPayload, p: ToolResultPayload, isLatest: boolean) {
  if (p.is_error) {
    return renderError(t, p.output);
  }
  switch (call.tool_name) {
    case 'Edit':
      return renderEditResult(t, call, p, isLatest);
    case 'Write':
      return renderWriteResult(t, call, p, isLatest);
    case 'Read':
      return renderReadResult(t, p, isLatest);
    case 'Bash':
      return renderBashResult(t, p, isLatest);
    case 'Glob':
      return renderGlobResult(t, 'Glob', p);
    case 'Grep':
      return renderGrepResult(t, p);
    default:
      return renderGenericOk(t, p);
  }
}

function renderError(t: TFunction, output: string) {
  return (
    <div className={styles.errorPanel} role="alert">
      <span className={styles.errorLabel}>{t('tool.result.error')}</span>
      <pre className={styles.errorBody}>{output || t('tool.result.noMessage')}</pre>
    </div>
  );
}

function renderEditResult(t: TFunction, call: ToolCallPayload, p: ToolResultPayload, isLatest: boolean) {
  // Prefer the backend diff. If it's empty/absent, generate a client-side
  // diff from old_string/new_string. If that's missing too — generic ok.
  const backendDiff = p.diff ?? '';
  if (backendDiff) {
    return renderDiffResult(t, backendDiff, isLatest);
  }
  const oldStr = strOrEmpty(call.input?.old_string);
  const newStr = strOrEmpty(call.input?.new_string);
  if (oldStr || newStr) {
    const path = strOrEmpty(call.input?.path) || 'edit';
    const patch = createPatch(path, oldStr, newStr, '', '', { context: 3 });
    return renderDiffResult(t, patch, isLatest);
  }
  return renderGenericOk(t, p);
}

function renderWriteResult(t: TFunction, call: ToolCallPayload, p: ToolResultPayload, isLatest: boolean) {
  const backendDiff = p.diff ?? '';
  if (backendDiff) {
    return renderDiffResult(t, backendDiff, isLatest);
  }
  const content = strOrEmpty(call.input?.content);
  const path = strOrEmpty(call.input?.path);
  if (content) {
    const lang = languageFromPath(path);
    const contentLines = countLines(content);
    const heavy = contentLines > EDIT_DIFF_COLLAPSE_THRESHOLD;
    const body = (
      <div className={styles.resultBlock}>
        <SyntaxHighlighter
          language={lang}
          style={oneLight}
          PreTag="div"
          customStyle={preStyle}
          codeTagProps={codeStyleProps}
        >
          {content}
        </SyntaxHighlighter>
      </div>
    );
    if (!heavy || isLatest) {
      return body;
    }
    return (
      <CollapsibleResult
        summary={
          <span className={styles.collapseLabel}>
            {t('tool.result.contentSummary', { count: contentLines })}
          </span>
        }
        defaultExpanded={false}
        testId="tool-result-write"
      >
        {body}
      </CollapsibleResult>
    );
  }
  return renderGenericOk(t, p);
}

function renderReadResult(t: TFunction, p: ToolResultPayload, isLatest: boolean) {
  const text = p.output ?? '';
  if (!text) {
    return <div className={styles.resultOk}>{t('tool.result.read', { count: 0 })}</div>;
  }
  const lines = text.endsWith('\n')
    ? text.slice(0, -1).split('\n')
    : text.split('\n');
  return (
    <CollapsibleResult
      summary={
        <span className={styles.resultOk}>{t('tool.result.read', { count: lines.length })}</span>
      }
      defaultExpanded={isLatest}
      testId="tool-result-read"
    >
      <pre className={styles.readPreview}>{text}</pre>
    </CollapsibleResult>
  );
}

function renderBashResult(t: TFunction, p: ToolResultPayload, isLatest: boolean) {
  const exit = p.exit_code;
  const duration = p.duration_ms;
  const timedOut = Boolean(p.timed_out);
  const stdout = p.stdout ?? '';
  const stderr = p.stderr ?? '';
  const headParts: string[] = [];
  if (typeof exit === 'number') headParts.push(`exit ${exit}`);
  if (typeof duration === 'number') headParts.push(formatDuration(duration));
  if (timedOut) headParts.push('TIMED OUT');
  const headerLabel = headParts.join(' · ') || t('tool.result.generic');
  const ok = exit === 0 && !timedOut;

  // Old event without extra fields: show `output` as-is.
  if (!stdout && !stderr && typeof exit !== 'number') {
    return (
      <div className={styles.resultBlock}>
        <pre className={styles.bashFallback}>{p.output || t('tool.result.noOutput')}</pre>
      </div>
    );
  }

  return (
    <div
      className={styles.bashResult}
      data-status={ok ? 'ok' : 'fail'}
    >
      <div className={styles.bashHeader}>
        <span className={styles.bashLabel}>bash</span>
        <span className={ok ? styles.exitOk : styles.exitFail}>{headerLabel}</span>
      </div>
      {stdout ? <BashStream text={stdout} isLatest={isLatest} variant="stdout" /> : null}
      {stderr ? (
        <div className={styles.bashStderrWrap}>
          <span className={styles.bashStderrLabel}>stderr</span>
          <BashStream text={stderr} isLatest={isLatest} variant="stderr" />
        </div>
      ) : null}
      {!stdout && !stderr ? (
        <pre className={styles.bashStdout}>{t('tool.result.noOutput')}</pre>
      ) : null}
    </div>
  );
}

interface BashStreamProps {
  text: string;
  isLatest: boolean;
  variant: 'stdout' | 'stderr';
}

function BashStream({ text, isLatest, variant }: BashStreamProps) {
  const { t } = useTranslation();
  const lines = countLines(text);
  const heavy = lines > BASH_STREAM_COLLAPSE_THRESHOLD;
  const [expanded, setExpanded] = useState(!heavy || isLatest);
  const baseClass = variant === 'stderr' ? styles.bashStderr : styles.bashStdout;
  const fadedClass = variant === 'stderr' ? styles.bashStderrFaded : styles.bashStdoutFaded;
  if (expanded) {
    return <pre className={baseClass}>{text}</pre>;
  }
  return (
    <>
      <pre className={fadedClass}>{text}</pre>
      <button
        type="button"
        className={styles.bashExpandButton}
        data-variant={variant}
        onClick={() => setExpanded(true)}
        data-testid={`bash-${variant}-expand`}
      >
        {t('tool.result.expand', { count: lines })}
      </button>
    </>
  );
}

function renderGlobResult(t: TFunction, label: 'Glob' | 'Grep', p: ToolResultPayload) {
  const stripped = (p.output ?? '').replace(/\n+$/, '');
  if (!stripped) {
    return (
      <div className={styles.resultOk}>{t('tool.result.matches', { tool: label, count: 0 })}</div>
    );
  }
  const count = stripped.split('\n').length;
  return (
    <div className={styles.resultBlock}>
      <div className={styles.resultOk}>{t('tool.result.matches', { tool: label, count })}</div>
      <pre className={styles.matchList}>{stripped}</pre>
    </div>
  );
}

function renderGrepResult(t: TFunction, p: ToolResultPayload) {
  return renderGlobResult(t, 'Grep', p);
}

function renderGenericOk(t: TFunction, p: ToolResultPayload) {
  const text = (p.output ?? '').trim();
  if (text) {
    return (
      <div className={styles.resultBlock}>
        <span className={styles.resultLabel}>{t('tool.result.generic')}</span>
        <pre className={styles.genericOutput}>{p.output}</pre>
      </div>
    );
  }
  return <div className={styles.resultOk}>{t('tool.result.ok')}</div>;
}

function renderDiff(diffText: string) {
  return (
    <div className={styles.resultBlock}>
      <DiffView diffText={diffText} />
    </div>
  );
}

function renderDiffResult(t: TFunction, diffText: string, isLatest: boolean) {
  const lines = countLines(diffText);
  const heavy = lines > EDIT_DIFF_COLLAPSE_THRESHOLD;
  if (!heavy || isLatest) {
    return renderDiff(diffText);
  }
  return (
    <CollapsibleResult
      summary={
        <span className={styles.collapseLabel}>
          {t('tool.result.diffSummary', { count: lines })}
        </span>
      }
      defaultExpanded={false}
      testId="tool-result-diff"
    >
      {renderDiff(diffText)}
    </CollapsibleResult>
  );
}

interface CollapsibleResultProps {
  summary: ReactNode;
  defaultExpanded: boolean;
  testId?: string;
  children: ReactNode;
}

function CollapsibleResult({ summary, defaultExpanded, testId, children }: CollapsibleResultProps) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  return (
    <div
      className={styles.collapsible}
      data-testid={testId}
      data-expanded={expanded ? 'true' : 'false'}
    >
      <button
        type="button"
        className={styles.collapseToggle}
        onClick={() => setExpanded((e) => !e)}
        aria-expanded={expanded}
      >
        <span className={styles.collapseChevron} aria-hidden="true">
          {expanded ? '▼' : '▶'}
        </span>
        {summary}
      </button>
      {expanded ? <div className={styles.collapseBody}>{children}</div> : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function strOrEmpty(v: unknown): string {
  return typeof v === 'string' ? v : '';
}

function numOrNull(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

function countLines(text: string): number {
  if (!text) return 0;
  const stripped = text.endsWith('\n') ? text.slice(0, -1) : text;
  return stripped.split('\n').length;
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  if (ms < 10_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.round(ms / 1000)}s`;
}

function languageFromPath(path: string): string {
  return resolveLang(path) ?? 'text';
}

// `background: transparent` is deliberate — the bubble already sits on `--muted`,
// and a grey block on top gave "grey on grey", especially bad for the bash header.
// The inner <code> is transparent too (otherwise the oneLight theme bleeds its
// background through on every line).
const preStyle = {
  margin: 0,
  padding: '8px 10px',
  background: 'transparent',
  borderRadius: 4,
  fontSize: '12px',
  lineHeight: 1.45,
  maxHeight: 320,
  overflow: 'auto' as const,
};

const codeStyleProps = {
  style: {
    fontFamily: 'var(--font-mono)' as const,
    background: 'transparent',
  },
};
