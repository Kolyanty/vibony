import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { RefreshCw } from 'lucide-react';
import { AuthError, getMe } from '../api/auth';
import { SessionsListPage } from '../pages/SessionsListPage';
import { LandingPage } from '../pages/LandingPage';
import { Logo } from '@/components/Logo';
import { Button } from '@/components/ui/button';

type AuthState = 'loading' | 'authenticated' | 'anonymous' | 'offline';

/**
 * Root `/` gate: authenticated users get the dashboard (SessionsListPage),
 * everyone else sees the instance welcome page. Unlike RequireAuth, an
 * unauthenticated visitor is NOT bounced to /login — `/` is a public page.
 *
 * A 401/403 is "not logged in" → public landing. A network/backend failure is
 * NOT treated as anonymous (a logged-in user refreshing while the backend is
 * down would otherwise silently see the public page); it shows a retryable
 * offline screen instead.
 */
export function HomeGate() {
  const { t } = useTranslation();
  const [state, setState] = useState<AuthState>('loading');
  // One in-flight probe at a time; a retry (or unmount) aborts the previous so
  // a stale callback never lands on an unmounted component.
  const controllerRef = useRef<AbortController | null>(null);

  const check = useCallback(() => {
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setState('loading');
    getMe({ signal: controller.signal })
      .then(() => setState('authenticated'))
      .catch((err: unknown) => {
        if (err instanceof DOMException && err.name === 'AbortError') return;
        if (err instanceof AuthError && err.reason === 'unauthorized') {
          setState('anonymous');
        } else {
          setState('offline');
        }
      });
  }, []);

  useEffect(() => {
    check();
    return () => controllerRef.current?.abort();
  }, [check]);

  if (state === 'loading') return null;
  if (state === 'anonymous') return <LandingPage />;
  if (state === 'offline') {
    return (
      <main className="flex min-h-screen flex-col items-center justify-center gap-5 bg-background px-6 text-center">
        <Logo variant="tile" className="size-10 opacity-90" />
        <div>
          <h1 className="text-lg font-bold tracking-tight">{t('home.offline.title')}</h1>
          <p className="mt-1.5 max-w-[360px] text-sm text-muted-foreground">
            {t('home.offline.body')}
          </p>
        </div>
        <Button onClick={() => check()} variant="outline">
          <RefreshCw className="size-4" />
          {t('home.offline.retry')}
        </Button>
      </main>
    );
  }
  return <SessionsListPage />;
}
