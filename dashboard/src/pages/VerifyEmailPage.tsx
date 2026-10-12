import { useEffect, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { verifyEmail } from '../api/auth';
import { AuthShell } from '../components/AuthShell';
import { Button } from '@/components/ui/button';

/*
 * Public landing for the verification link (`/verify-email?token=…`). Mirrors the
 * `/invite/:token` pattern: the page consumes the token, calls the backend, and
 * shows the outcome.
 */
export function VerifyEmailPage() {
  const { t } = useTranslation();
  const [params] = useSearchParams();
  const token = params.get('token');
  const [state, setState] = useState<'verifying' | 'ok' | 'error'>('verifying');

  useEffect(() => {
    if (!token) {
      setState('error');
      return;
    }
    const controller = new AbortController();
    verifyEmail(token, { signal: controller.signal })
      .then(() => setState('ok'))
      .catch((err: unknown) => {
        if (err instanceof DOMException && err.name === 'AbortError') return;
        setState('error');
      });
    return () => controller.abort();
  }, [token]);

  return (
    <AuthShell title={t('verifyEmail.page.title')}>
      <div className="flex flex-col gap-4 text-center" data-testid="verify-email-page">
        {state === 'verifying' && <p className="text-muted-foreground">{t('verifyEmail.page.verifying')}</p>}
        {state === 'ok' && (
          <>
            <p className="text-success" data-testid="verify-email-ok">
              {t('verifyEmail.page.ok')}
            </p>
            <Button asChild className="w-full">
              <Link to="/">{t('verifyEmail.page.continue')}</Link>
            </Button>
          </>
        )}
        {state === 'error' && (
          <>
            <p className="text-destructive" data-testid="verify-email-error">
              {t('verifyEmail.page.error')}
            </p>
            <Button asChild variant="outline" className="w-full">
              <Link to="/">{t('verifyEmail.page.continue')}</Link>
            </Button>
          </>
        )}
      </div>
    </AuthShell>
  );
}
