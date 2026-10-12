import { useTranslation } from 'react-i18next';
import type { EventEnvelope } from '../api/types';
import { Avatar } from './Avatar';
import { Markdown } from './Markdown';
import { RelativeTime } from './RelativeTime';
import styles from './MessageRow.module.css';

type MessageEvent = Extract<EventEnvelope, { type: 'user_message' | 'assistant_message' }>;

interface MessageRowProps {
  event: MessageEvent;
}

export function MessageRow({ event }: MessageRowProps) {
  const { t } = useTranslation();
  const avatarRole = event.type === 'user_message' ? 'candidate' : 'ai';
  const roleKey = event.type === 'user_message' ? 'role.user' : 'role.ai';
  return (
    <div className={styles.row} data-role={event.type}>
      <Avatar role={avatarRole} />
      <div className={styles.bubble}>
        <div className={styles.meta}>
          <span className={styles.role}>{t(roleKey)}</span>
          <RelativeTime className={styles.time} timestamp={event.timestamp} />
        </div>
        {event.type === 'assistant_message' ? (
          <Markdown source={event.payload.text} />
        ) : (
          <p className={styles.text}>{event.payload.text}</p>
        )}
      </div>
    </div>
  );
}
