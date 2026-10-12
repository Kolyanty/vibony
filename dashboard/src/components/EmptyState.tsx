import { useCallback, useState } from 'react';
import { Copy } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import styles from './EmptyState.module.css';

interface EmptyStateProps {
  code: string;
  /** candidate_token from session creation — used to build the candidate URL. */
  candidateToken?: string;
}

type CopyTarget = 'code' | 'action';

export function EmptyState({ code, candidateToken }: EmptyStateProps) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState<CopyTarget | null>(null);

  const candidateUrl = candidateToken
    ? `${window.location.origin}/candidate?token=${candidateToken}`
    : null;

  const copy = useCallback(async (text: string, target: CopyTarget) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(target);
      window.setTimeout(() => {
        setCopied((curr) => (curr === target ? null : curr));
      }, 1500);
    } catch {
      // clipboard may be unavailable (insecure context, jsdom without a user gesture);
      // silently ignore — the text is always visible for manual selection.
    }
  }, []);

  const codeTitle = copied === 'code' ? t('session.copy.done') : t('empty.copy.code');
  const actionTitle = copied === 'action' ? t('session.copy.done') : t('empty.copy.url');

  return (
    <div className={styles.root} data-testid="dialog-empty-state">
      <h2 className={styles.title}>{t('empty.title')}</h2>
      <p className={styles.hint}>{t('empty.hint.web')}</p>

      <button
        type="button"
        className={styles.codeButton}
        onClick={() => copy(code, 'code')}
        title={codeTitle}
        data-testid="empty-state-code-copy"
      >
        <code className={styles.code}>{code}</code>
        <Copy className={styles.copyIcon} aria-hidden="true" size={18} />
        <span className={styles.sr}>{codeTitle}</span>
      </button>
      {copied === 'code' && (
        <span className={styles.copiedHint} aria-live="polite">
          {t('session.copy.doneHint')}
        </span>
      )}

      <button
        type="button"
        className={styles.commandButton}
        onClick={() => copy(candidateUrl ?? '', 'action')}
        title={actionTitle}
        data-testid="empty-state-url-copy"
      >
        <code className={styles.command}>{candidateUrl ?? '…'}</code>
        <Copy className={styles.copyIcon} aria-hidden="true" size={14} />
        <span className={styles.sr}>{actionTitle}</span>
      </button>
      {copied === 'action' && (
        <span className={styles.copiedHint} aria-live="polite">
          {t('session.copy.doneHint')}
        </span>
      )}
    </div>
  );
}
