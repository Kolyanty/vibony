import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, Loader2, RotateCw } from 'lucide-react';
import { getAssessment, regenerateAssessment } from '../api/rest';
import type {
  AssessmentFinding,
  AssessmentFindingCategory,
  AssessmentSeverity,
  SessionAssessment,
} from '../api/types';
import { Button } from './ui/button';
import { Markdown } from './Markdown';

// Poll cadence while the job is pending/running (mirrors the candidate timer's
// "advisory only" cadence — the row is server-authoritative, this just refreshes).
const POLL_INTERVAL_MS = 4000;

type Load =
  | { kind: 'loading' }
  | { kind: 'error' }
  | { kind: 'ready'; data: SessionAssessment };

const CATEGORY_PILL: Record<AssessmentFindingCategory, string> = {
  strength: 'border-[var(--success)] text-[var(--success)]',
  gap: 'border-[var(--warn)] text-[var(--warn)]',
  pattern: 'border-[var(--primary)] text-[var(--primary)]',
};

const SEVERITY_PILL: Record<AssessmentSeverity, string> = {
  blocking: 'border-[var(--destructive)] text-[var(--destructive)]',
  degrading: 'border-[var(--warn)] text-[var(--warn)]',
  info: 'border-[var(--border)] text-[var(--muted-foreground)]',
};

const PILL_BASE =
  'inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium';

/**
 * Interviewer-only AI assessment tab. Fetches the report and
 * polls while the job is pending/running. Never rendered in public replay.
 */
export function AssessmentTab({
  code,
  sessionCompleted,
  onJumpToSeq,
}: {
  code: string;
  /** Generate is only offered for completed sessions (a partial run isn't useful). */
  sessionCompleted: boolean;
  /** Jump the session view to a finding's cited event (deep-link). */
  onJumpToSeq?: (seq: number) => void;
}) {
  const { t } = useTranslation();
  const [load, setLoad] = useState<Load>({ kind: 'loading' });
  const [regenerating, setRegenerating] = useState(false);
  // Guards against setState after unmount — the poll, retry, and regenerate
  // async paths can resolve after the tab is switched away (component gone).
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const fetchOnce = useCallback(
    async (signal?: AbortSignal) => {
      try {
        const data = await getAssessment(code, { signal });
        if (mountedRef.current) setLoad({ kind: 'ready', data });
      } catch (err) {
        if (err instanceof DOMException && err.name === 'AbortError') return;
        if (mountedRef.current) setLoad({ kind: 'error' });
      }
    },
    [code],
  );

  // Initial fetch (aborted on unmount / code change).
  useEffect(() => {
    const ctrl = new AbortController();
    void fetchOnce(ctrl.signal);
    return () => ctrl.abort();
  }, [fetchOnce]);

  // Poll only while the job is in flight; the effect re-subscribes on status
  // change and aborts its in-flight fetch on cleanup (unmount / status change).
  const status = load.kind === 'ready' ? load.data.status : null;
  useEffect(() => {
    if (status !== 'pending' && status !== 'running') return;
    const ctrl = new AbortController();
    const id = setInterval(() => void fetchOnce(ctrl.signal), POLL_INTERVAL_MS);
    return () => {
      ctrl.abort();
      clearInterval(id);
    };
  }, [status, fetchOnce]);

  const handleRegenerate = useCallback(async () => {
    setRegenerating(true);
    try {
      const data = await regenerateAssessment(code);
      if (mountedRef.current) setLoad({ kind: 'ready', data });
    } catch {
      if (mountedRef.current) setLoad({ kind: 'error' });
    } finally {
      if (mountedRef.current) setRegenerating(false);
    }
  }, [code]);

  if (load.kind === 'loading') {
    return (
      <Centered testId="assessment-loading">
        <Loader2 className="size-4 animate-spin" aria-hidden />
        <span>{t('assessment.loading')}</span>
      </Centered>
    );
  }

  if (load.kind === 'error') {
    return (
      <Centered testId="assessment-error">
        <p className="text-sm text-muted-foreground">{t('assessment.error')}</p>
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            setLoad({ kind: 'loading' });
            void fetchOnce();
          }}
          data-testid="assessment-retry"
        >
          {t('assessment.retry')}
        </Button>
      </Centered>
    );
  }

  const { data } = load;

  if (data.status === 'pending' || data.status === 'running') {
    return (
      <Centered testId="assessment-generating">
        <Loader2 className="size-4 animate-spin" aria-hidden />
        <span>{t('assessment.generating')}</span>
      </Centered>
    );
  }

  if (data.status === 'not_started') {
    return (
      <Centered testId="assessment-not-started">
        <p className="text-sm text-muted-foreground">
          {sessionCompleted
            ? t('assessment.notStarted.completed')
            : t('assessment.notStarted.pending')}
        </p>
        {sessionCompleted && (
          <RegenerateButton
            busy={regenerating}
            onClick={handleRegenerate}
            label={t('assessment.generate')}
            testId="assessment-generate"
          />
        )}
      </Centered>
    );
  }

  if (data.status === 'failed') {
    return (
      <Centered testId="assessment-failed">
        <div className="flex items-center gap-2 text-sm font-medium text-[var(--destructive)]">
          <AlertTriangle className="size-4" aria-hidden />
          {t('assessment.failed.title')}
        </div>
        {data.error && (
          <p className="max-w-md text-xs text-muted-foreground">{data.error}</p>
        )}
        <RegenerateButton
          busy={regenerating}
          onClick={handleRegenerate}
          label={t('assessment.regenerate')}
          testId="assessment-regenerate"
        />
      </Centered>
    );
  }

  // status === 'ready'
  return (
    <div
      className="flex h-full flex-col gap-4 overflow-y-auto p-4 text-sm"
      data-testid="assessment-ready"
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs text-muted-foreground" data-testid="assessment-model">
          {data.model ? t('assessment.model', { model: data.model }) : null}
        </span>
        <RegenerateButton
          busy={regenerating}
          onClick={handleRegenerate}
          label={t('assessment.regenerate')}
          testId="assessment-regenerate"
        />
      </div>

      {data.summary_md && (
        <div data-testid="assessment-summary">
          <Markdown source={data.summary_md} />
        </div>
      )}

      <div>
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          {t('assessment.findings.title')}
        </h3>
        {data.findings.length === 0 ? (
          <p className="text-xs text-muted-foreground">{t('assessment.empty.findings')}</p>
        ) : (
          <ul className="flex flex-col gap-2">
            {data.findings.map((f, i) => (
              <FindingCard key={i} finding={f} onJumpToSeq={onJumpToSeq} />
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function FindingCard({
  finding,
  onJumpToSeq,
}: {
  finding: AssessmentFinding;
  onJumpToSeq?: (seq: number) => void;
}) {
  const { t } = useTranslation();
  const seq = finding.event_seq;
  return (
    <li
      className="rounded-lg border border-border bg-card p-3"
      data-testid="assessment-finding"
    >
      <div className="mb-1 flex flex-wrap items-center gap-2">
        <span className={`${PILL_BASE} ${CATEGORY_PILL[finding.category]}`}>
          {t(`assessment.category.${finding.category}`)}
        </span>
        {finding.severity && (
          <span className={`${PILL_BASE} ${SEVERITY_PILL[finding.severity]}`}>
            {t(`assessment.severity.${finding.severity}`)}
          </span>
        )}
        {seq != null &&
          (onJumpToSeq ? (
            // Deep-link — jump the session view to the cited event.
            <button
              type="button"
              className="inline-flex items-center rounded-md bg-muted px-1.5 py-0.5 font-mono text-xs text-muted-foreground transition-colors hover:bg-[var(--primary)] hover:text-[var(--primary-foreground)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--primary)]"
              data-testid="assessment-finding-seq"
              title={t('assessment.eventSeqHint')}
              onClick={() => onJumpToSeq(seq)}
            >
              {t('assessment.eventSeq', { seq })}
            </button>
          ) : (
            <span
              className="inline-flex items-center rounded-md bg-muted px-1.5 py-0.5 font-mono text-xs text-muted-foreground"
              data-testid="assessment-finding-seq"
            >
              {t('assessment.eventSeq', { seq })}
            </span>
          ))}
      </div>
      <p className="font-medium text-foreground">{finding.title}</p>
      <p className="mt-0.5 text-xs text-muted-foreground">{finding.detail}</p>
    </li>
  );
}

function RegenerateButton({
  busy,
  onClick,
  label,
  testId,
}: {
  busy: boolean;
  onClick: () => void;
  label: string;
  testId: string;
}) {
  return (
    <Button
      variant="outline"
      size="sm"
      onClick={onClick}
      disabled={busy}
      data-testid={testId}
    >
      {busy ? (
        <Loader2 className="size-3.5 animate-spin" aria-hidden />
      ) : (
        <RotateCw className="size-3.5" aria-hidden />
      )}
      {label}
    </Button>
  );
}

function Centered({
  children,
  testId,
}: {
  children: React.ReactNode;
  testId: string;
}) {
  return (
    <div
      className="flex h-full flex-col items-center justify-center gap-3 p-6 text-center text-sm text-muted-foreground"
      data-testid={testId}
    >
      {children}
    </div>
  );
}
