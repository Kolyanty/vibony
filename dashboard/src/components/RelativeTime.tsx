import { useTranslation } from 'react-i18next';
import { useNow } from '../state/useNow';
import { formatRelative } from './formatRelative';

interface RelativeTimeProps {
  /** ISO timestamp from the event envelope. */
  timestamp: string;
  className?: string;
}

export function RelativeTime({ timestamp, className }: RelativeTimeProps) {
  const { t } = useTranslation();
  const now = useNow();
  const then = Date.parse(timestamp);
  if (!Number.isFinite(then)) {
    return (
      <time className={className} dateTime={timestamp}>
        {timestamp}
      </time>
    );
  }
  const absolute = new Date(then).toLocaleTimeString();
  const relative = formatRelative(then, now, t);
  return (
    <time className={className} dateTime={timestamp} title={absolute}>
      {relative}
    </time>
  );
}
