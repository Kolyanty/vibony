import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Check, Copy, Plus } from 'lucide-react';
import { AuthError, getMe, logout, type MembershipInfo, type User } from '../api/auth';
import {
  listSessions,
  createSession,
  listTasks,
  type SessionListItem,
  type SessionCreatedResponse,
  type TaskListItem,
} from '../api/rest';
import type { SessionMode } from '../api/types';
import { LanguageSwitcher } from '../components/LanguageSwitcher';
import { WorkspaceSwitcher } from '../components/WorkspaceSwitcher';
import { CreateWorkspaceDialog } from '../components/CreateWorkspaceDialog';
import { VerifyEmailBanner } from '../components/VerifyEmailBanner';
import { useActiveWorkspace } from '../state/activeWorkspace';
import { apiErrorMessage } from '@/lib/apiError';
import { AppHeader } from '@/components/AppHeader';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { TaskCombobox } from '../components/TaskCombobox';

type ModalState =
  | { kind: 'closed' }
  | { kind: 'open' }
  | { kind: 'submitting' }
  | { kind: 'created'; result: SessionCreatedResponse };

// Default async time limit shown in the form (minutes) + bounds mirroring the
// backend's MIN/MAX_TIME_LIMIT_SECONDS (300–14400 → 5–240 min).
const DEFAULT_TIME_LIMIT_MINUTES = 60;
const MIN_TIME_LIMIT_MINUTES = 5;
const MAX_TIME_LIMIT_MINUTES = 240;

// Canonicalize the async time-limit text field: digits only, no leading zeros
// (so "05" / "000000005" can't masquerade as input), empty stays empty. Range
// validation happens on submit with an i18n message; this only fixes the
// display + parsing. We keep the field as a string because binding `value` to a
// number drops back into the DOM as a string — React then skips re-syncing the
// input whenever the parsed number is unchanged, leaving leading zeros stuck.
function normalizeTimeLimit(raw: string): string {
  return raw.replace(/\D/g, '').replace(/^0+(?=\d)/, '');
}

const STATUS_BADGE: Record<string, string> = {
  pending: 'bg-warn-soft text-warn',
  active: 'bg-success-soft text-success',
  completed: 'bg-muted text-muted-foreground',
};

const MODE_BADGE: Record<SessionMode, string> = {
  live: 'bg-muted text-muted-foreground',
  async: 'bg-primary-soft text-primary',
};

/**
 * Browser entry point for the candidate. Prefer the backend-issued
 * `candidate_url`; fall back to building it from the token so the link is
 * always shown even if the field is null.
 */
function candidateUrlFor(result: SessionCreatedResponse): string {
  return result.candidate_url ?? `${window.location.origin}/candidate?token=${result.candidate_token}`;
}

export function SessionsListPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [sessions, setSessions] = useState<SessionListItem[]>([]);
  const [user, setUser] = useState<User | null>(null);
  const [memberships, setMemberships] = useState<MembershipInfo[]>([]);
  const [tasks, setTasks] = useState<TaskListItem[]>([]);
  const [modal, setModal] = useState<ModalState>({ kind: 'closed' });
  const [taskId, setTaskId] = useState('');
  const [mode, setMode] = useState<SessionMode>('live');
  const [timeLimitInput, setTimeLimitInput] = useState<string>(String(DEFAULT_TIME_LIMIT_MINUTES));
  const [createError, setCreateError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [wsModalOpen, setWsModalOpen] = useState(false);
  // Whether the candidate-URL copy button last fired (resets after 1.5 s).
  const [copied, setCopied] = useState(false);

  // Active workspace drives the switcher + scopes the session list.
  const { activeId, active, setActiveId } = useActiveWorkspace(memberships);

  // Create-session sends `workspace_id=activeId`, so the task picker must
  // only offer tasks usable in that workspace — its own + templates. Listing every
  // workspace's tasks let the employer pick a cross-workspace task → backend 400.
  const visibleTasks = useMemo(() => {
    const scoped = activeId ? tasks.filter((tk) => tk.is_template || tk.company_id === activeId) : tasks;
    // Create-session resolves task_id (slug) + workspace_id with workspace-over-template
    // precedence (backend find_task_by_slug, is_template ASC). Mirror it: when a workspace
    // task and a Vibony task share a slug, only the workspace one will ever run, so collapse
    // them to a single row — otherwise the picker offers an ambiguous duplicate (same slug,
    // both look selected). Preferring the non-template row matches what the backend resolves.
    const bySlug = new Map<string, TaskListItem>();
    for (const tk of scoped) {
      const existing = bySlug.get(tk.slug);
      if (!existing || (existing.is_template && !tk.is_template)) bySlug.set(tk.slug, tk);
    }
    return [...bySlug.values()];
  }, [tasks, activeId]);

  // Keep the selected task within the visible set: seed it once tasks load and
  // reset it whenever the active workspace changes out from under the selection.
  useEffect(() => {
    if (visibleTasks.length === 0) {
      if (taskId !== '') setTaskId('');
      return;
    }
    if (!visibleTasks.some((tk) => tk.slug === taskId)) {
      setTaskId(visibleTasks[0]!.slug);
    }
  }, [visibleTasks, taskId]);

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      listSessions({ signal: controller.signal }),
      getMe({ signal: controller.signal }),
      listTasks({ signal: controller.signal }),
    ])
      .then(([sess, me, tk]) => {
        setSessions(sess);
        setUser(me.user);
        setMemberships(me.memberships);
        setTasks(tk);
      })
      .catch((err: unknown) => {
        if (err instanceof DOMException && err.name === 'AbortError') return;
        if (err instanceof AuthError && err.reason === 'unauthorized') {
          navigate('/login', { replace: true });
        }
        // other errors (network, 500) — leave page empty; user can retry
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [navigate]);

  const handleLogout = useCallback(async () => {
    await logout();
    navigate('/login', { replace: true });
  }, [navigate]);

  const openModal = () => {
    setCreateError(null);
    setModal({ kind: 'open' });
  };

  const closeModal = () => setModal({ kind: 'closed' });

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    // Backstop the disabled-submit gate: under `noValidate` the `<select required>`
    // native check is inert, so a programmatic submit could otherwise post an empty
    // task_id. The button is already disabled when `!taskId`; this just makes it sound.
    if (!taskId) return;
    // Guard the async limit client-side. The form is `noValidate` (native bubbles
    // render in the browser locale, not the app's i18n) so all validation runs
    // here. Parse from the normalized form so an Enter-submit (no blur) can't slip
    // a cleared field (NaN) or leading zeros past the check.
    const normalizedLimit = normalizeTimeLimit(timeLimitInput);
    const timeLimitMinutes = normalizedLimit === '' ? NaN : Number(normalizedLimit);
    if (
      mode === 'async' &&
      (!Number.isFinite(timeLimitMinutes) ||
        timeLimitMinutes < MIN_TIME_LIMIT_MINUTES ||
        timeLimitMinutes > MAX_TIME_LIMIT_MINUTES)
    ) {
      setCreateError(
        t('sessions.create.timeLimitInvalid', {
          min: MIN_TIME_LIMIT_MINUTES,
          max: MAX_TIME_LIMIT_MINUTES,
        }),
      );
      return;
    }
    setCreateError(null);
    setModal({ kind: 'submitting' });
    try {
      const result = await createSession({
        task_id: taskId,
        workspace_id: activeId || null,
        mode,
        time_limit_seconds: mode === 'async' ? timeLimitMinutes * 60 : null,
      });
      setModal({ kind: 'created', result });
      setSessions((prev) => [
        {
          session_code: result.session_code,
          session_status: result.session_status as SessionListItem['session_status'],
          task_id: result.task_id,
          task_dir_name: result.task_dir_name,
          workspace_id: activeId || null,
          created_at: result.created_at,
          expires_at: result.expires_at,
          mode: result.mode,
          time_limit_seconds: result.time_limit_seconds,
        },
        ...prev,
      ]);
    } catch (err) {
      setCreateError(apiErrorMessage(err, t));
      setModal({ kind: 'open' });
    }
  };

  const handleCopyLink = async (link: string) => {
    try {
      await navigator.clipboard.writeText(link);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // ignore clipboard errors
    }
  };

  // Team link reflects the active workspace's role (you may be admin in one
  // workspace, interviewer in another).
  const isAdmin = active?.role === 'admin';
  const createdCandidateUrl = modal.kind === 'created' ? candidateUrlFor(modal.result) : null;
  // Scope the list to the active workspace; before memberships resolve, show all.
  const visibleSessions = activeId ? sessions.filter((s) => s.workspace_id === activeId) : sessions;

  const handleWorkspaceCreated = (m: MembershipInfo) => {
    setMemberships((prev) => [...prev, m]);
    setActiveId(m.workspace_id);
  };

  return (
    <div className="min-h-screen bg-background">
      <AppHeader>
        <WorkspaceSwitcher
          memberships={memberships}
          active={active}
          onSelect={setActiveId}
          onCreate={() => setWsModalOpen(true)}
        />
        <LanguageSwitcher />
        <Button asChild variant="ghost" size="sm" data-testid="tasks-link">
          <Link to="/tasks">{t('tasks.nav')}</Link>
        </Button>
        {isAdmin && (
          <Button asChild variant="ghost" size="sm" data-testid="team-link">
            <Link to="/team">{t('team.nav')}</Link>
          </Button>
        )}
        <Button variant="outline" size="sm" onClick={handleLogout} data-testid="logout-button">
          {t('auth.logout.button')}
        </Button>
      </AppHeader>

      <main className="mx-auto max-w-[960px] px-6 py-10">
        {user && !user.email_verified && <VerifyEmailBanner email={user.email} />}
        <div className="mb-7 flex items-center justify-between">
          <h1 className="text-2xl font-bold tracking-tight">{t('sessions.list.title')}</h1>
          <Button onClick={openModal} data-testid="create-session-button">
            <Plus className="size-4" />
            {t('sessions.list.createButton')}
          </Button>
        </div>

        {loading ? (
          <div className="flex flex-col gap-2.5" data-testid="sessions-loading" aria-hidden="true">
            {Array.from({ length: 3 }).map((_, i) => (
              <div key={i} className="flex items-center gap-4 rounded-xl border border-border bg-card px-5 py-4">
                <Skeleton className="h-4 w-24" />
                <Skeleton className="h-4 flex-1" />
                <Skeleton className="h-5 w-16 rounded-full" />
              </div>
            ))}
          </div>
        ) : memberships.length === 0 ? (
          <div
            className="flex flex-col items-center gap-4 rounded-xl border border-dashed border-border bg-card/50 px-6 py-16 text-center text-muted-foreground"
            data-testid="no-workspace"
          >
            <p>{t('workspace.none.body')}</p>
            <Button onClick={() => setWsModalOpen(true)} data-testid="no-workspace-create">
              <Plus className="size-4" />
              {t('workspace.create.button')}
            </Button>
          </div>
        ) : visibleSessions.length === 0 ? (
          <div
            className="rounded-xl border border-dashed border-border bg-card/50 px-6 py-16 text-center text-muted-foreground"
            data-testid="sessions-empty"
          >
            {t('sessions.list.empty')}
          </div>
        ) : (
          <div className="flex flex-col gap-2.5" data-testid="sessions-table">
            {visibleSessions.map((s) => (
              <Link
                key={s.session_code}
                to={`/session/${s.session_code}`}
                data-testid={`session-row-${s.session_code}`}
                className="group flex items-center gap-4 rounded-xl border border-border bg-card px-5 py-4 shadow-sm transition-colors hover:border-primary/40 hover:no-underline"
              >
                <span className="font-mono text-sm font-semibold tracking-wide text-primary group-hover:underline">
                  {s.session_code}
                </span>
                <span className="min-w-0 flex-1 truncate text-sm text-muted-foreground">
                  {s.task_dir_name}
                </span>
                <span
                  className={`rounded-full px-2.5 py-0.5 text-xs font-semibold ${MODE_BADGE[s.mode ?? 'live']}`}
                  data-testid={`session-mode-${s.session_code}`}
                >
                  {t(`sessions.mode.${s.mode ?? 'live'}`)}
                </span>
                <span
                  className={`rounded-full px-2.5 py-0.5 text-xs font-semibold ${
                    STATUS_BADGE[s.session_status] ?? 'bg-muted text-muted-foreground'
                  }`}
                >
                  {s.session_status}
                </span>
                <span className="hidden shrink-0 text-xs text-muted-foreground sm:block">
                  {new Date(s.created_at).toLocaleString()}
                </span>
              </Link>
            ))}
          </div>
        )}
      </main>

      <Dialog open={modal.kind !== 'closed'} onOpenChange={(open) => !open && closeModal()}>
        <DialogContent data-testid="create-modal" className="sm:max-w-[460px]">
          {modal.kind === 'created' ? (
            <>
              <DialogHeader>
                <DialogTitle>{t('sessions.create.successTitle')}</DialogTitle>
                <DialogDescription className="sr-only">{t('sessions.create.successTitle')}</DialogDescription>
              </DialogHeader>
              <div className="flex flex-col gap-4 rounded-lg border border-border bg-muted/50 p-4">
                <div>
                  <p className="mb-1 text-xs text-muted-foreground">{t('sessions.create.code')}</p>
                  <span
                    className="font-mono text-2xl font-bold tracking-wider"
                    data-testid="created-code"
                  >
                    {modal.result.session_code}
                  </span>
                </div>
                <div className="flex flex-col gap-2">
                  <p className="text-xs text-muted-foreground">{t('sessions.create.candidateUrlHint')}</p>
                  <div className="flex items-center gap-2 rounded-md border border-border bg-card px-3 py-2">
                    <code className="flex-1 break-all font-mono text-xs" data-testid="candidate-url">
                      {createdCandidateUrl}
                    </code>
                    <Button
                      type="button"
                      variant="outline"
                      size="xs"
                      onClick={() => createdCandidateUrl && handleCopyLink(createdCandidateUrl)}
                      data-testid="copy-url-button"
                    >
                      {copied ? <Check className="size-3" /> : <Copy className="size-3" />}
                      {copied ? t('session.copy.done') : t('session.copy.link')}
                    </Button>
                  </div>
                </div>
                <Button asChild className="w-full">
                  <Link
                    to={`/session/${modal.result.session_code}`}
                    state={{ candidateToken: modal.result.candidate_token }}
                    data-testid="open-session-link"
                  >
                    {t('sessions.create.openSession')}
                  </Link>
                </Button>
              </div>
              <DialogFooter>
                <Button type="button" variant="outline" onClick={closeModal} data-testid="modal-close">
                  {t('sessions.create.close')}
                </Button>
              </DialogFooter>
            </>
          ) : (
            <>
              <DialogHeader>
                <DialogTitle>{t('sessions.create.title')}</DialogTitle>
                <DialogDescription className="sr-only">{t('sessions.create.title')}</DialogDescription>
              </DialogHeader>
              <form
                className="flex flex-col gap-4"
                onSubmit={handleCreate}
                noValidate
                data-testid="create-form"
              >
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="task-id">{t('sessions.create.taskLabel')}</Label>
                  <TaskCombobox
                    id="task-id"
                    tasks={visibleTasks}
                    value={taskId}
                    onChange={setTaskId}
                    disabled={visibleTasks.length === 0}
                  />
                </div>
                <div className="flex flex-col gap-1.5">
                  <Label id="mode-label">{t('sessions.create.modeLabel')}</Label>
                  <div className="flex gap-4" role="radiogroup" aria-labelledby="mode-label">
                    {(['live', 'async'] as const).map((m) => (
                      <label key={m} className="flex cursor-pointer items-center gap-1.5 text-sm">
                        <input
                          type="radio"
                          name="mode"
                          value={m}
                          checked={mode === m}
                          onChange={() => setMode(m)}
                          className="accent-primary"
                          data-testid={`mode-radio-${m}`}
                        />
                        {t(`sessions.mode.${m}`)}
                      </label>
                    ))}
                  </div>
                </div>
                {mode === 'async' && (
                  <div className="flex flex-col gap-1.5">
                    <Label htmlFor="time-limit">{t('sessions.create.timeLimitLabel')}</Label>
                    <Input
                      id="time-limit"
                      type="number"
                      min={MIN_TIME_LIMIT_MINUTES}
                      max={MAX_TIME_LIMIT_MINUTES}
                      inputMode="numeric"
                      value={timeLimitInput}
                      onChange={(e) => setTimeLimitInput(e.target.value)}
                      onBlur={() => setTimeLimitInput(normalizeTimeLimit)}
                      required
                      data-testid="time-limit-input"
                    />
                    <p className="text-xs text-muted-foreground">{t('sessions.create.timeLimitHint')}</p>
                  </div>
                )}
                {memberships.length > 1 && active && (
                  <p className="text-xs text-muted-foreground" data-testid="create-target-workspace">
                    {t('sessions.create.targetWorkspace', { name: active.company_name })}
                  </p>
                )}
                {createError && (
                  <p className="text-sm text-destructive" role="alert" data-testid="create-error">
                    {createError}
                  </p>
                )}
                <DialogFooter>
                  <Button type="button" variant="outline" onClick={closeModal} data-testid="modal-cancel">
                    {t('sessions.create.cancel')}
                  </Button>
                  <Button
                    type="submit"
                    disabled={modal.kind === 'submitting' || !taskId}
                    data-testid="create-submit"
                  >
                    {modal.kind === 'submitting' ? '…' : t('sessions.create.submit')}
                  </Button>
                </DialogFooter>
              </form>
            </>
          )}
        </DialogContent>
      </Dialog>

      <CreateWorkspaceDialog
        open={wsModalOpen}
        onOpenChange={setWsModalOpen}
        onCreated={handleWorkspaceCreated}
      />
    </div>
  );
}
