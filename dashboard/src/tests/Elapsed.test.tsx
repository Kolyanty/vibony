import { act, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Elapsed } from '../components/Elapsed';
import { formatElapsed } from '../components/formatElapsed';
import { TimeNowProvider } from '../state/TimeNowProvider';
import i18next from '../i18n';

const t = i18next.getFixedT('en');

describe('formatElapsed (en)', () => {
  const start = Date.parse('2026-05-15T10:00:00Z');

  it('< 5 sec → "just now"', () => {
    expect(formatElapsed(start, start, t)).toBe('just now');
    expect(formatElapsed(start, start + 4_000, t)).toBe('just now');
  });

  it('5–59 sec → "N sec"', () => {
    expect(formatElapsed(start, start + 5_000, t)).toBe('5 sec');
    expect(formatElapsed(start, start + 30_000, t)).toBe('30 sec');
  });

  it('1–59 min → "N min"', () => {
    expect(formatElapsed(start, start + 60_000, t)).toBe('1 min');
    expect(formatElapsed(start, start + 5 * 60_000, t)).toBe('5 min');
  });

  it('1h+ → "N h" or "N h M min"', () => {
    expect(formatElapsed(start, start + 3_600_000, t)).toBe('1 h');
    expect(formatElapsed(start, start + 3_600_000 + 23 * 60_000, t)).toBe('1 h 23 min');
    expect(formatElapsed(start, start + 5 * 3_600_000, t)).toBe('5 h');
  });

  it('days+ → "N day(s)" or "N d K h"', () => {
    expect(formatElapsed(start, start + 86_400_000, t)).toBe('1 day');
    expect(formatElapsed(start, start + 86_400_000 + 5 * 3_600_000, t)).toBe('1 d 5 h');
    expect(formatElapsed(start, start + 3 * 86_400_000, t)).toBe('3 days');
  });

  it('future start → "just now"', () => {
    expect(formatElapsed(start, start - 10_000, t)).toBe('just now');
  });
});

describe('formatElapsed (ru)', () => {
  const start = Date.parse('2026-05-15T10:00:00Z');
  const rt = i18next.getFixedT('ru');

  it('< 5 sec → "just now"', () => {
    expect(formatElapsed(start, start, rt)).toBe('только что');
  });

  it('seconds/minutes/hours/days', () => {
    expect(formatElapsed(start, start + 30_000, rt)).toBe('30 сек');
    expect(formatElapsed(start, start + 5 * 60_000, rt)).toBe('5 мин');
    expect(formatElapsed(start, start + 3_600_000, rt)).toBe('1 ч');
    expect(formatElapsed(start, start + 86_400_000, rt)).toBe('1 дн');
    expect(formatElapsed(start, start + 86_400_000 + 5 * 3_600_000, rt)).toBe('1 дн 5 ч');
  });
});

describe('Elapsed component', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-05-15T10:05:00Z'));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('renders <time> with elapsed text and absolute title', () => {
    render(<Elapsed start="2026-05-15T10:00:00Z" />);
    const node = screen.getByText('5 min');
    expect(node.tagName).toBe('TIME');
    expect(node).toHaveAttribute('dateTime', '2026-05-15T10:00:00Z');
    expect(node.title).toBeTruthy();
  });

  it('updates on TimeNowProvider tick', () => {
    render(
      <TimeNowProvider frozenAt={null} intervalMs={1000}>
        <Elapsed start="2026-05-15T10:00:00Z" />
      </TimeNowProvider>,
    );
    expect(screen.getByText('5 min')).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(60_000);
    });
    expect(screen.getByText('6 min')).toBeInTheDocument();
  });

  it('frozenAt → text stays on tick', () => {
    const frozen = Date.parse('2026-05-15T10:05:00Z');
    render(
      <TimeNowProvider frozenAt={frozen} intervalMs={1000}>
        <Elapsed start="2026-05-15T10:00:00Z" />
      </TimeNowProvider>,
    );
    expect(screen.getByText('5 min')).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(120_000);
    });
    expect(screen.getByText('5 min')).toBeInTheDocument();
  });

  it('invalid start → outputs raw string', () => {
    render(<Elapsed start="not-a-date" />);
    const node = screen.getByText('not-a-date');
    expect(node.tagName).toBe('TIME');
  });
});
