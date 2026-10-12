import { useEffect, useState } from 'react';
import { Navigate } from 'react-router-dom';
import { getMe, AuthError } from '../api/auth';

type AuthState = 'loading' | 'authenticated' | 'unauthenticated';

export function RequireAuth({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<AuthState>('loading');

  useEffect(() => {
    const controller = new AbortController();
    getMe({ signal: controller.signal })
      .then(() => setState('authenticated'))
      .catch((err: unknown) => {
        if (err instanceof DOMException && err.name === 'AbortError') return;
        if (err instanceof AuthError && err.reason === 'unauthorized') {
          setState('unauthenticated');
        } else {
          // Network error or unexpected error — also redirect to login.
          setState('unauthenticated');
        }
      });
    return () => controller.abort();
  }, []);

  if (state === 'loading') return null;
  if (state === 'unauthenticated') return <Navigate to="/login" replace />;
  return <>{children}</>;
}
