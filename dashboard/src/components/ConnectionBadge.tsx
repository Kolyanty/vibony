import { useTranslation } from 'react-i18next';
import type { ConnectionState } from '../state/sessionState';
import styles from './ConnectionBadge.module.css';

interface Props {
  state: ConnectionState;
}

const KEYS: Record<ConnectionState, string> = {
  idle: 'connection.idle',
  connecting: 'connection.connecting',
  connected: 'connection.connected',
  reconnecting: 'connection.reconnecting',
  closed: 'connection.closed',
};

export function ConnectionBadge({ state }: Props) {
  const { t } = useTranslation();
  return (
    <span className={styles.badge} data-state={state}>
      <span className={styles.dot} />
      {t(KEYS[state])}
    </span>
  );
}
