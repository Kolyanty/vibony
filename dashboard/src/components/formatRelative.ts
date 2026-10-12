import type { TFunction } from 'i18next';

/**
 * Formats the difference between `then` and `now` (both in epoch ms) into a
 * human-readable relative string via i18n. A future `then` (now - then < 0) is
 * treated as "just now" so it doesn't flash negatives on clock skew.
 */
export function formatRelative(then: number, now: number, t: TFunction): string {
  const diffSec = Math.floor((now - then) / 1000);
  if (diffSec < 5) return t('time.justNow');
  if (diffSec < 60) return t('time.secAgo', { count: diffSec });
  if (diffSec < 3600) {
    const m = Math.floor(diffSec / 60);
    return t('time.minAgo', { count: m });
  }
  if (diffSec < 86_400) {
    const h = Math.floor(diffSec / 3600);
    return t('time.hourAgo', { count: h });
  }
  const d = Math.floor(diffSec / 86_400);
  return t('time.dayAgo', { count: d });
}
