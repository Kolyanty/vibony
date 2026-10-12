import { useTranslation } from 'react-i18next';
import { useNow } from '../state/useNow';
import { formatElapsed } from './formatElapsed';

interface ElapsedProps {
  /** ISO timestamp of the session start (usually `SessionConfig.created_at`). */
  start: string;
  className?: string;
}

export function Elapsed({ start, className }: ElapsedProps) {
  const { t } = useTranslation();
  const now = useNow();
  const startMs = Date.parse(start);
  if (!Number.isFinite(startMs)) {
    return (
      <time className={className} dateTime={start}>
        {start}
      </time>
    );
  }
  const text = formatElapsed(startMs, now, t);
  return (
    <time className={className} dateTime={start} title={new Date(startMs).toLocaleString()}>
      {text}
    </time>
  );
}
