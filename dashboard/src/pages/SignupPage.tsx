import { useEffect, useState } from 'react';
import { Link, Navigate, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { getAuthConfig, signup, AuthError, type AuthConfig } from '../api/auth';
import { AuthShell } from '@/components/AuthShell';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';

export function SignupPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [config, setConfig] = useState<AuthConfig | null>(null);
  const [configLoaded, setConfigLoaded] = useState(false);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [companyName, setCompanyName] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    getAuthConfig({ signal: controller.signal })
      .then((c) => {
        setConfig(c);
        setConfigLoaded(true);
      })
      .catch(() => setConfigLoaded(true));
    return () => controller.abort();
  }, []);

  // OIDC mode — signup not available; redirect to login
  if (configLoaded && config?.mode === 'oidc') {
    return <Navigate to="/login" replace />;
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setPending(true);
    try {
      await signup({ email, password, company_name: companyName });
      navigate('/', { replace: true });
    } catch (err) {
      // Map by reason; a reserved-TLD email (422) or rate-limit (429) must not
      // read as "server unreachable", and a non-AuthError must not no-op (F2).
      if (err instanceof AuthError && err.reason === 'duplicate') {
        setError(t('auth.signup.errorDuplicate'));
      } else if (err instanceof AuthError && err.reason === 'rate_limited') {
        setError(t('auth.signup.errorRateLimited'));
      } else if (err instanceof AuthError && err.reason === 'invalid') {
        setError(t('auth.signup.errorValidation'));
      } else if (err instanceof AuthError && err.reason === 'network') {
        setError(t('auth.signup.errorNetwork'));
      } else {
        setError(t('auth.signup.errorGeneric'));
      }
    } finally {
      setPending(false);
    }
  };

  return (
    <AuthShell
      title={t('auth.signup.title')}
      footer={<Link to="/login">{t('auth.login.title')}</Link>}
    >
      <form className="flex flex-col gap-4" onSubmit={handleSubmit} data-testid="signup-form">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="company">{t('auth.signup.companyName')}</Label>
          <Input
            id="company"
            type="text"
            value={companyName}
            onChange={(e) => setCompanyName(e.target.value)}
            required
            data-testid="signup-company"
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="email">{t('auth.login.email')}</Label>
          <Input
            id="email"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
            autoComplete="email"
            data-testid="signup-email"
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
            minLength={8}
            autoComplete="new-password"
            data-testid="signup-password"
          />
        </div>
        {error && (
          <p className="text-sm text-destructive" role="alert" data-testid="signup-error">
            {error}
          </p>
        )}
        <Button type="submit" className="mt-1 w-full" disabled={pending} data-testid="signup-submit">
          {pending ? '…' : t('auth.signup.submit')}
        </Button>
      </form>
    </AuthShell>
  );
}
