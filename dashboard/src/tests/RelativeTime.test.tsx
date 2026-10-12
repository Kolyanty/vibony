import { act, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { RelativeTime } from '../components/RelativeTime';
import { formatRelative } from '../components/formatRelative';
import { TimeNowProvider } from '../state/TimeNowProvider';
import i18next from '../i18n';

const t = i18next.getFixedT('en');

describe('formatRelative (en)', () => {
  const now = Date.parse('2026-05-16T12:00:00Z');

  it('< 5 sec → "just now"', () => {
    expect(formatRelative(now - 0, now, t)).toBe('just now');
    expect(formatRelative(now - 4_000, now, t)).toBe('just now');
  });

  it('5–59 sec → "N seconds ago"', () => {
    expect(formatRelative(now - 5_000, now, t)).toBe('5 seconds ago');
    expect(formatRelative(now - 30_000, now, t)).toBe('30 seconds ago');
  });

  it('1–59 min → "N minute(s) ago"', () => {
    expect(formatRelative(now - 60_000, now, t)).toBe('1 minute ago');
    expect(formatRelative(now - 5 * 60_000, now, t)).toBe('5 minutes ago');
  });

  it('1–23 h → "N hour(s) ago"', () => {
    expect(formatRelative(now - 3_600_000, now, t)).toBe('1 hour ago');
    expect(formatRelative(now - 5 * 3_600_000, now, t)).toBe('5 hours ago');
  });

  it('days+ → "N day(s) ago"', () => {
    expect(formatRelative(now - 86_400_000, now, t)).toBe('1 day ago');
    expect(formatRelative(now - 3 * 86_400_000, now, t)).toBe('3 days ago');
  });

  it('future timestamp → "just now" (no negatives)', () => {
    expect(formatRelative(now + 10_000, now, t)).toBe('just now');
  });
});

describe('formatRelative (ru)', () => {
  const now = Date.parse('2026-05-16T12:00:00Z');
  const rt = i18next.getFixedT('ru');

  it('small intervals → "just now" / "N sec ago"', () => {
    expect(formatRelative(now - 0, now, rt)).toBe('только что');
    expect(formatRelative(now - 30_000, now, rt)).toBe('30 сек назад');
  });

  it('minutes/hours/days', () => {
    expect(formatRelative(now - 5 * 60_000, now, rt)).toBe('5 мин назад');
    expect(formatRelative(now - 3_600_000, now, rt)).toBe('1 ч назад');
    expect(formatRelative(now - 86_400_000, now, rt)).toBe('1 дн назад');
  });
});

describe('RelativeTime component', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-05-16T12:00:00Z'));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('renders <time> with relative text and absolute title', () => {
    render(<RelativeTime timestamp="2026-05-16T11:59:30Z" />);
    const node = screen.getByText('30 seconds ago');
    expect(node.tagName).toBe('TIME');
    expect(node).toHaveAttribute('dateTime', '2026-05-16T11:59:30Z');
    expect(node.title).toBeTruthy();
  });

  it('updates on TimeNowProvider tick', () => {
    render(
      <TimeNowProvider frozenAt={null} intervalMs={1000}>
        <RelativeTime timestamp="2026-05-16T11:59:30Z" />
      </TimeNowProvider>,
    );
    expect(screen.getByText('30 seconds ago')).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(60_000);
    });
    expect(screen.getByText('1 minute ago')).toBeInTheDocument();
  });

  it('frozenAt → relative frozen, no tick updates', () => {
    const frozen = Date.parse('2026-05-16T12:00:00Z');
    render(
      <TimeNowProvider frozenAt={frozen} intervalMs={1000}>
        <RelativeTime timestamp="2026-05-16T11:59:30Z" />
      </TimeNowProvider>,
    );
    expect(screen.getByText('30 seconds ago')).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(60_000);
    });
    expect(screen.getByText('30 seconds ago')).toBeInTheDocument();
  });

  it('invalid timestamp → renders raw string without title', () => {
    render(<RelativeTime timestamp="not-a-date" />);
    const node = screen.getByText('not-a-date');
    expect(node.tagName).toBe('TIME');
    expect(node.title).toBe('');
  });
});
