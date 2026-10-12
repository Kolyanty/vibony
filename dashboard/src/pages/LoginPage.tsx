import { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { getAuthConfig, login, AuthError, type AuthConfig } from '../api/auth';
import { AuthShell } from '@/components/AuthShell';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';

export function LoginPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [config, setConfig] = useState<AuthConfig | null>(null);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    getAuthConfig({ signal: controller.signal })
      .then(setConfig)
      .catch(() => {});
    return () => controller.abort();
  }, []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setPending(true);
    try {
      await login({ email, password });
      navigate('/', { replace: true });
    } catch (err) {
      // Map by reason; never fold a 422/429/500 into "could not reach server"
      // and never leave a non-AuthError as a silent no-op (F2 / D-3).
      if (err instanceof AuthError && err.reason === 'unauthorized') {
        setError(t('auth.login.errorWrongCredentials'));
      } else if (err instanceof AuthError && err.reason === 'rate_limited') {
        setError(t('auth.login.errorRateLimited'));
      } else if (err instanceof AuthError && err.reason === 'invalid') {
        setError(t('auth.login.errorValidation'));
      } else if (err instanceof AuthError && err.reason === 'network') {
        setError(t('auth.login.errorNetwork'));
      } else {
        setError(t('auth.login.errorGeneric'));
      }
    } finally {
      setPending(false);
    }
  };

  return (
    <AuthShell
      title={t('auth.login.title')}
      footer={<Link to="/signup">{t('auth.signup.title')}</Link>}
    >
      {config?.mode === 'oidc' ? (
        <Button asChild variant="outline" className="w-full" data-testid="login-oidc-button">
          <a href={config.oidc_login_url ?? '/auth/oidc/start'}>{t('auth.login.oidcButton')}</a>
        </Button>
      ) : (
        <form className="flex flex-col gap-4" onSubmit={handleSubmit} data-testid="login-form">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="email">{t('auth.login.email')}</Label>
            <Input
              id="email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              autoComplete="email"
              data-testid="login-email"
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="password">{t('auth.login.password')}</Label>
            <Input
              id="password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              autoComplete="current-password"
              data-testid="login-password"
            />
          </div>
          {error && (
            <p className="text-sm text-destructive" role="alert" data-testid="login-error">
              {error}
            </p>
          )}
          <Button type="submit" className="mt-1 w-full" disabled={pending} data-testid="login-submit">
            {pending ? '…' : t('auth.login.submit')}
          </Button>
        </form>
      )}
    </AuthShell>
  );
}
