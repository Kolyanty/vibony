import { useTranslation } from 'react-i18next';
import { Avatar } from './Avatar';
import styles from './TypingIndicator.module.css';

export function TypingIndicator() {
  const { t } = useTranslation();
  return (
    <div className={styles.row} data-testid="typing-indicator" aria-live="polite">
      <Avatar role="ai" />
      <div className={styles.bubble} role="status" aria-label={t('dialog.typing')}>
        <span className={styles.dot} aria-hidden="true" />
        <span className={styles.dot} aria-hidden="true" />
        <span className={styles.dot} aria-hidden="true" />
      </div>
    </div>
  );
}
