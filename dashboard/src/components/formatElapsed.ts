import type { TFunction } from 'i18next';

/**
 * Formats the time elapsed since `start` into a human-readable string via i18n.
 * Designed around the `TimeNowProvider` tick (30s): seconds are not shown after
 * the first minute, otherwise the digits would jump in 30-second steps.
 *
 * A future `start` (start > now, e.g. clock skew between the client and backend)
 * is treated as "just now".
 */
export function formatElapsed(start: number, now: number, t: TFunction): string {
  const diffSec = Math.floor((now - start) / 1000);
  if (diffSec < 5) return t('time.justNow');
  if (diffSec < 60) return t('elapsed.sec', { count: diffSec });
  if (diffSec < 3600) {
    const m = Math.floor(diffSec / 60);
    return t('elapsed.min', { count: m });
  }
  if (diffSec < 86_400) {
    const h = Math.floor(diffSec / 3600);
    const m = Math.floor((diffSec % 3600) / 60);
    return m === 0 ? t('elapsed.hour', { count: h }) : t('elapsed.hourMin', { hours: h, minutes: m });
  }
  const d = Math.floor(diffSec / 86_400);
  const h = Math.floor((diffSec % 86_400) / 3600);
  return h === 0 ? t('elapsed.day', { count: d }) : t('elapsed.dayHour', { days: d, hours: h });
}
