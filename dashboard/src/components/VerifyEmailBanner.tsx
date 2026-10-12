import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { X } from 'lucide-react';
import { resendVerification } from '../api/auth';
import { Button } from '@/components/ui/button';

/*
 * Non-blocking email-verification banner. Shown on the dashboard home
 * when the current user's email is unverified. Dismissible for the session (returns
 * on reload); offers a one-click resend.
 */
export function VerifyEmailBanner({ email }: { email: string }) {
  const { t } = useTranslation();
  const [dismissed, setDismissed] = useState(false);
  const [state, setState] = useState<'idle' | 'sending' | 'sent' | 'error'>('idle');

  if (dismissed) return null;

  const resend = async () => {
    setState('sending');
    try {
      await resendVerification();
      setState('sent');
    } catch {
      setState('error');
    }
  };

  return (
    <div
      className="mb-6 flex items-center gap-3 rounded-lg border border-warn/30 bg-warn-soft px-4 py-3 text-sm"
      role="status"
      data-testid="verify-email-banner"
    >
      <span className="flex-1 text-warn">
        {state === 'sent' ? t('verifyEmail.banner.sent') : t('verifyEmail.banner.message', { email })}
      </span>
      {state !== 'sent' && (
        <Button
          variant="outline"
          size="xs"
          onClick={resend}
          disabled={state === 'sending'}
          data-testid="verify-email-resend"
        >
          {state === 'sending' ? '…' : t('verifyEmail.banner.resend')}
        </Button>
      )}
      {state === 'error' && (
        <span className="text-destructive" data-testid="verify-email-error">
          {t('verifyEmail.banner.error')}
        </span>
      )}
      <button
        type="button"
        onClick={() => setDismissed(true)}
        aria-label={t('verifyEmail.banner.dismiss')}
        className="text-warn/70 hover:text-warn"
        data-testid="verify-email-dismiss"
      >
        <X className="size-4" />
      </button>
    </div>
  );
}
