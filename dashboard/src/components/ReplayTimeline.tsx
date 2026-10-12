import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Pause, Play, SkipBack, SkipForward } from 'lucide-react';
import type { EventEnvelope } from '../api/types';
import { formatRelative } from './formatRelative';
import { useNow } from '../state/useNow';
import styles from './ReplayTimeline.module.css';

interface ReplayTimelineProps {
  total: number;
  cursor: number;
  events: EventEnvelope[];
  onCursor: (cursor: number) => void;
}

const PLAY_INTERVAL_MS = 800;

export function ReplayTimeline({ total, cursor, events, onCursor }: ReplayTimelineProps) {
  const { t } = useTranslation();
  const now = useNow();
  const [playing, setPlaying] = useState(false);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const stopPlay = useCallback(() => {
    setPlaying(false);
    if (intervalRef.current !== null) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
  }, []);

  const startPlay = useCallback(() => {
    if (cursor >= total) {
      onCursor(0);
    }
    setPlaying(true);
  }, [cursor, total, onCursor]);

  const cursorRef = useRef(cursor);
  cursorRef.current = cursor;

  useEffect(() => {
    if (!playing) return;
    intervalRef.current = setInterval(() => {
      const next = cursorRef.current + 1;
      if (next >= total) {
        setPlaying(false);
        if (intervalRef.current !== null) clearInterval(intervalRef.current);
        onCursor(total);
      } else {
        onCursor(next);
      }
    }, PLAY_INTERVAL_MS);
    return () => {
      if (intervalRef.current !== null) clearInterval(intervalRef.current);
    };
  }, [playing, total, onCursor]);

  // Stop playing when reaching end
  useEffect(() => {
    if (playing && cursor >= total) stopPlay();
  }, [cursor, total, playing, stopPlay]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLInputElement>) => {
      if (e.key === 'ArrowLeft') {
        e.preventDefault();
        stopPlay();
        onCursor(Math.max(0, cursor - 1));
      } else if (e.key === 'ArrowRight') {
        e.preventDefault();
        stopPlay();
        onCursor(Math.min(total, cursor + 1));
      }
    },
    [cursor, total, onCursor, stopPlay],
  );

  const handleKeyDownRoot = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === ' ' && e.target === e.currentTarget) {
        e.preventDefault();
        if (playing) stopPlay();
        else startPlay();
      }
    },
    [playing, startPlay, stopPlay],
  );

  const currentEvent = cursor > 0 && cursor <= events.length ? events[cursor - 1] : null;
  const currentTimestamp = currentEvent ? Date.parse(currentEvent.timestamp) : null;
  const relativeTime =
    currentTimestamp !== null && Number.isFinite(currentTimestamp)
      ? formatRelative(currentTimestamp, now, t)
      : null;

  if (total === 0) return null;

  return (
    <div
      className={styles.root}
      onKeyDown={handleKeyDownRoot}
      tabIndex={-1}
      aria-label={t('replay.timeline.label')}
    >
      <div className={styles.controls}>
        <button
          type="button"
          className={styles.iconButton}
          onClick={() => { stopPlay(); onCursor(0); }}
          title={t('replay.timeline.toStart')}
          aria-label={t('replay.timeline.toStart')}
        >
          <SkipBack size={14} />
        </button>
        <button
          type="button"
          className={styles.iconButton}
          onClick={playing ? stopPlay : startPlay}
          title={playing ? t('replay.timeline.pause') : t('replay.timeline.play')}
          aria-label={playing ? t('replay.timeline.pause') : t('replay.timeline.play')}
          data-playing={playing}
        >
          {playing ? <Pause size={14} /> : <Play size={14} />}
        </button>
        <button
          type="button"
          className={styles.iconButton}
          onClick={() => { stopPlay(); onCursor(total); }}
          title={t('replay.timeline.toEnd')}
          aria-label={t('replay.timeline.toEnd')}
        >
          <SkipForward size={14} />
        </button>
      </div>
      <input
        type="range"
        className={styles.slider}
        min={0}
        max={total}
        value={cursor}
        onChange={(e) => {
          stopPlay();
          onCursor(Number(e.target.value));
        }}
        onKeyDown={handleKeyDown}
        aria-label={t('replay.timeline.slider')}
        aria-valuemin={0}
        aria-valuemax={total}
        aria-valuenow={cursor}
        aria-valuetext={t('replay.timeline.position', { cursor, total })}
      />
      <div className={styles.meta}>
        <span className={styles.position}>
          {t('replay.timeline.position', { cursor, total })}
        </span>
        {relativeTime && (
          <span className={styles.timestamp}>{relativeTime}</span>
        )}
      </div>
    </div>
  );
}
