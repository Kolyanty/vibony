import { useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { AuthError, getMe } from '../api/auth';
import { acceptInvite, ApiError, previewInvite, signupAndAcceptInvite, type InvitePreview } from '../api/rest';
import { apiErrorMessage } from '@/lib/apiError';
import { AuthShell } from '@/components/AuthShell';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';

type PageState =
  | { kind: 'loading' }
  | { kind: 'not_found' }
  | { kind: 'unavailable'; status: string }
  | { kind: 'logged_in'; preview: InvitePreview }
  | { kind: 'not_logged_in'; preview: InvitePreview }
  | { kind: 'joining' }
  | { kind: 'joined'; companyName: string }
  | { kind: 'error'; message: string };

export function AcceptInvitePage() {
  const { token } = useParams<{ token: string }>();
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [state, setState] = useState<PageState>({ kind: 'loading' });
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!token) {
      setState({ kind: 'not_found' });
      return;
    }
    const controller = new AbortController();
    void (async () => {
      try {
        const [preview, me] = await Promise.allSettled([
          previewInvite(token, { signal: controller.signal }),
          getMe({ signal: controller.signal }),
        ]);
        if (controller.signal.aborted) return;

        if (preview.status === 'rejected') {
          const err = preview.reason;
          if (err instanceof ApiError && err.status === 404) {
            setState({ kind: 'not_found' });
          } else {
            setState({ kind: 'error', message: apiErrorMessage(err, t) });
          }
          return;
        }

        const inv = preview.value;
        if (inv.status !== 'pending') {
          setState({ kind: 'unavailable', status: inv.status });
          return;
        }

        if (me.status === 'fulfilled') {
          setState({ kind: 'logged_in', preview: inv });
        } else {
          setState({ kind: 'not_logged_in', preview: inv });
          if (inv.invited_email) setEmail(inv.invited_email);
        }
      } catch (err) {
        if (err instanceof DOMException && err.name === 'AbortError') return;
        setState({ kind: 'error', message: apiErrorMessage(err, t) });
      }
    })();
    return () => controller.abort();
  }, [token, t]);

  const handleJoin = async () => {
    if (!token) return;
    setState({ kind: 'joining' });
    try {
      await acceptInvite(token);
      const preview = (state as { preview?: InvitePreview }).preview;
      setState({ kind: 'joined', companyName: preview?.company_name ?? '' });
    } catch (err) {
      setState({ kind: 'error', message: apiErrorMessage(err, t) });
    }
  };

  const handleSignupAndAccept = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!token) return;
    setFormError(null);
    setSubmitting(true);
    try {
      await signupAndAcceptInvite(token, { email, password });
      const preview = (state as { preview?: InvitePreview }).preview;
      setState({ kind: 'joined', companyName: preview?.company_name ?? '' });
    } catch (err) {
      if (err instanceof AuthError && err.reason === 'duplicate') {
        setFormError(t('auth.signup.errorDuplicate'));
      } else {
        setFormError(apiErrorMessage(err, t));
      }
    } finally {
      setSubmitting(false);
    }
  };

  const backLink = (
    <Link to="/">{t('team.nav.back')}</Link>
  );

  if (state.kind === 'loading' || state.kind === 'joining') {
    return (
      <AuthShell title={t('invite.loading')}>
        <div className="h-2 w-full animate-pulse rounded-full bg-muted" />
      </AuthShell>
    );
  }

  if (state.kind === 'not_found') {
    return (
      <AuthShell title={t('invite.notFound')} footer={backLink}>
        <p className="text-sm text-muted-foreground" data-testid="invite-not-found">
          {t('invite.notFound')}
        </p>
      </AuthShell>
    );
  }

  if (state.kind === 'unavailable') {
    return (
      <AuthShell title={t(`invite.status.${state.status}`)} footer={backLink}>
        <p className="text-sm text-muted-foreground" data-testid="invite-unavailable">
          {t(`invite.status.${state.status}`)}
        </p>
      </AuthShell>
    );
  }

  if (state.kind === 'error') {
    return (
      <AuthShell title={t('invite.title')} footer={backLink}>
        <p className="text-sm text-destructive" role="alert">{state.message}</p>
      </AuthShell>
    );
  }

  if (state.kind === 'joined') {
    return (
      <AuthShell title={t('invite.accepted.title', { company: state.companyName })}>
        <div data-testid="invite-joined">
          <Button
            type="button"
            className="w-full"
            onClick={() => navigate('/', { replace: true })}
            data-testid="go-to-sessions"
          >
            {t('invite.accepted.continue')}
          </Button>
        </div>
      </AuthShell>
    );
  }

  const preview = (state as { preview: InvitePreview }).preview;

  if (state.kind === 'logged_in') {
    return (
      <AuthShell
        title={t('invite.title')}
        description={t('invite.description', {
          inviter: preview.inviter_email,
          company: preview.company_name,
          role: preview.role,
        })}
      >
        <div data-testid="invite-logged-in">
          <Button type="button" className="w-full" onClick={handleJoin} data-testid="join-button">
            {t('invite.join.button', { company: preview.company_name })}
          </Button>
        </div>
      </AuthShell>
    );
  }

  // state.kind === 'not_logged_in'
  return (
    <AuthShell
      title={t('invite.signup.title')}
      description={t('invite.signup.hint', { company: preview.company_name })}
      footer={
        <>
          {t('invite.loginHint')}{' '}
          <Link to="/login">{t('auth.login.submit')}</Link>
        </>
      }
    >
      <form className="flex flex-col gap-4" onSubmit={handleSignupAndAccept} data-testid="invite-signup-form">
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
        {formError && (
          <p className="text-sm text-destructive" role="alert" data-testid="signup-error">
            {formError}
          </p>
        )}
        <Button type="submit" className="mt-1 w-full" disabled={submitting} data-testid="signup-submit">
          {submitting ? '…' : t('auth.signup.submit')}
        </Button>
      </form>
    </AuthShell>
  );
}
