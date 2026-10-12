import { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Check, Copy, UserPlus } from 'lucide-react';
import { AuthError, getMe, type MembershipInfo } from '../api/auth';
import {
  listMembers,
  listCompanyInvites,
  listAudit,
  createInvite,
  revokeInvite,
  updateMemberRole,
  removeMember,
  deleteWorkspace,
  type MemberListItem,
  type InviteItem,
  type AuditLogEntry,
} from '../api/rest';
import { WorkspaceSwitcher } from '../components/WorkspaceSwitcher';
import { CreateWorkspaceDialog } from '../components/CreateWorkspaceDialog';
import { ConfirmDialog } from '../components/ConfirmDialog';
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
import { NATIVE_SELECT_CLASS } from '@/lib/formClasses';

type ModalState =
  | { kind: 'closed' }
  | { kind: 'open' }
  | { kind: 'submitting' }
  | { kind: 'created'; invite: InviteItem };

type PageState =
  | { kind: 'loading' }
  | { kind: 'no_access' }
  | {
      kind: 'loaded';
      membership: MembershipInfo;
      members: MemberListItem[];
      invites: InviteItem[];
      audit: AuditLogEntry[];
    }
  | { kind: 'error'; message: string };

const INVITE_STATUS_BADGE: Record<string, string> = {
  pending: 'bg-warn-soft text-warn',
  accepted: 'bg-success-soft text-success',
  revoked: 'bg-muted text-muted-foreground',
  expired: 'bg-muted text-muted-foreground',
};

export function TeamPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [memberships, setMemberships] = useState<MembershipInfo[]>([]);
  const [state, setState] = useState<PageState>({ kind: 'loading' });
  const [modal, setModal] = useState<ModalState>({ kind: 'closed' });
  const [inviteRole, setInviteRole] = useState<'admin' | 'interviewer'>('interviewer');
  const [inviteEmail, setInviteEmail] = useState('');
  const [modalError, setModalError] = useState<string | null>(null);
  const [copiedToken, setCopiedToken] = useState<string | null>(null);
  const [revokeError, setRevokeError] = useState<string | null>(null);
  const [memberError, setMemberError] = useState<string | null>(null);
  // user_ids with a role PATCH in flight — their <select> is disabled so a second
  // change can't race the first (out-of-order responses would leave a stale role).
  const [updatingRoles, setUpdatingRoles] = useState<Set<string>>(() => new Set());
  const [currentUserId, setCurrentUserId] = useState<string | null>(null);
  const [wsModalOpen, setWsModalOpen] = useState(false);
  // Confirm dialogs (shadcn AlertDialog) replacing window.confirm.
  const [revokeTarget, setRevokeTarget] = useState<string | null>(null);
  const [removeTarget, setRemoveTarget] = useState<MemberListItem | null>(null);
  // Delete-workspace flow (type-the-name-to-confirm — the confirm gate is stronger
  // than a plain ConfirmDialog because this tears down the whole workspace).
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleteConfirmName, setDeleteConfirmName] = useState('');
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const { activeId, active, setActiveId } = useActiveWorkspace(memberships);

  // Load the membership list (drives the switcher).
  useEffect(() => {
    const controller = new AbortController();
    getMe({ signal: controller.signal })
      .then((me) => {
        setMemberships(me.memberships);
        setCurrentUserId(me.user.id);
      })
      .catch((err: unknown) => {
        if (err instanceof DOMException && err.name === 'AbortError') return;
        if (err instanceof AuthError && err.reason === 'unauthorized') {
          navigate('/login', { replace: true });
          return;
        }
        setState({ kind: 'error', message: apiErrorMessage(err, t) });
      });
    return () => controller.abort();
  }, [navigate, t]);

  // Load members + invites for the active workspace. TeamPage operates on the
  // workspace the switcher selects; non-admins of it see the no-access screen.
  useEffect(() => {
    const m = memberships.find((x) => x.workspace_id === activeId) ?? null;
    if (!m) return; // memberships not resolved yet
    if (m.role !== 'admin') {
      setState({ kind: 'no_access' });
      return;
    }
    const controller = new AbortController();
    setState({ kind: 'loading' });
    Promise.all([
      listMembers(m.workspace_id, { signal: controller.signal }),
      listCompanyInvites(m.workspace_id, { signal: controller.signal }),
      listAudit(m.workspace_id, { signal: controller.signal }),
    ])
      .then(([members, invites, audit]) =>
        setState({ kind: 'loaded', membership: m, members, invites, audit }),
      )
      .catch((err: unknown) => {
        if (err instanceof DOMException && err.name === 'AbortError') return;
        if (err instanceof AuthError && err.reason === 'unauthorized') {
          navigate('/login', { replace: true });
          return;
        }
        setState({ kind: 'error', message: apiErrorMessage(err, t) });
      });
    return () => controller.abort();
  }, [activeId, memberships, navigate, t]);

  const handleWorkspaceCreated = (m: MembershipInfo) => {
    setMemberships((prev) => [...prev, m]);
    setActiveId(m.workspace_id);
  };

  const handleRevoke = async (token: string) => {
    setRevokeError(null);
    try {
      await revokeInvite(token);
      setState((prev) => {
        if (prev.kind !== 'loaded') return prev;
        return {
          ...prev,
          invites: prev.invites.map((inv) =>
            inv.token === token ? { ...inv, status: 'revoked' as const } : inv,
          ),
        };
      });
    } catch (err) {
      setRevokeError(apiErrorMessage(err, t));
    }
  };

  const auditText = (e: AuditLogEntry): string => {
    const md = e.metadata as Record<string, unknown>;
    return t(`team.audit.action.${e.action}`, {
      actor: e.actor_email ?? t('team.audit.system'),
      // email resolved server-side at read time, not from metadata.
      email: e.target_email ?? '',
      role: String(md.role ?? ''),
      to: String(md.to ?? ''),
      name: String(md.name ?? ''),
      code: String(e.target_id ?? ''),
      task: String(md.task_id ?? ''),
      defaultValue: e.action,
    });
  };

  // last_admin (409) maps to team.error.lastAdmin via the shared map; every other
  // member-op failure gets a localized status message instead of a raw "HTTP 4xx".
  const memberErrorMessage = (err: unknown): string => apiErrorMessage(err, t);

  const handleRoleChange = async (member: MemberListItem, role: 'admin' | 'interviewer') => {
    if (state.kind !== 'loaded' || role === member.role) return;
    if (updatingRoles.has(member.user_id)) return; // a PATCH is already in flight
    const companyId = state.membership.workspace_id;
    setMemberError(null);
    setUpdatingRoles((prev) => new Set(prev).add(member.user_id));
    try {
      const updated = await updateMemberRole(companyId, member.user_id, role);
      setState((prev) =>
        prev.kind === 'loaded'
          ? { ...prev, members: prev.members.map((m) => (m.user_id === member.user_id ? updated : m)) }
          : prev,
      );
    } catch (err) {
      setMemberError(memberErrorMessage(err));
    } finally {
      setUpdatingRoles((prev) => {
        const next = new Set(prev);
        next.delete(member.user_id);
        return next;
      });
    }
  };

  const handleRemoveMember = async (member: MemberListItem) => {
    if (state.kind !== 'loaded') return;
    const companyId = state.membership.workspace_id;
    setMemberError(null);
    try {
      await removeMember(companyId, member.user_id);
      setState((prev) =>
        prev.kind === 'loaded'
          ? { ...prev, members: prev.members.filter((m) => m.user_id !== member.user_id) }
          : prev,
      );
    } catch (err) {
      setMemberError(memberErrorMessage(err));
    }
  };

  const handleDeleteWorkspace = async () => {
    if (state.kind !== 'loaded') return;
    const deletedId = state.membership.workspace_id;
    setDeleteError(null);
    setDeleting(true);
    try {
      await deleteWorkspace(deletedId);
      const remaining = memberships.filter((m) => m.workspace_id !== deletedId);
      setDeleteOpen(false);
      if (remaining.length === 0) {
        // The zero-workspace onboarding screen lives on the home page.
        navigate('/', { replace: true });
        return;
      }
      // Switch to another workspace; the load effect refetches for it.
      setActiveId(remaining[0]!.workspace_id);
      setMemberships(remaining);
    } catch (err) {
      setDeleteError(apiErrorMessage(err, t));
    } finally {
      setDeleting(false);
    }
  };

  const openDeleteModal = () => {
    setDeleteError(null);
    setDeleteConfirmName('');
    setDeleteOpen(true);
  };

  const openModal = () => {
    setModalError(null);
    setInviteEmail('');
    setInviteRole('interviewer');
    setModal({ kind: 'open' });
  };

  const handleCreateInvite = async (e: React.FormEvent) => {
    e.preventDefault();
    if (state.kind !== 'loaded') return;
    setModalError(null);
    setModal({ kind: 'submitting' });
    try {
      const invite = await createInvite(state.membership.workspace_id, {
        role: inviteRole,
        email: inviteEmail.trim() || undefined,
      });
      setModal({ kind: 'created', invite });
      setState((prev) =>
        prev.kind === 'loaded' ? { ...prev, invites: [invite, ...prev.invites] } : prev,
      );
    } catch (err) {
      setModalError(apiErrorMessage(err, t));
      setModal({ kind: 'open' });
    }
  };

  const handleCopyLink = async (url: string, token: string) => {
    try {
      const fullUrl = url.startsWith('http') ? url : `${window.location.origin}${url}`;
      await navigator.clipboard.writeText(fullUrl);
      setCopiedToken(token);
      window.setTimeout(() => setCopiedToken(null), 1500);
    } catch {
      // ignore
    }
  };

  const backButton = (
    <Button asChild variant="ghost" size="sm" data-testid="back-to-sessions">
      <Link to="/">{t('team.nav.back')}</Link>
    </Button>
  );

  const shell = (children: React.ReactNode) => (
    <div className="min-h-screen bg-background">
      <AppHeader>
        <WorkspaceSwitcher
          memberships={memberships}
          active={active}
          onSelect={setActiveId}
          onCreate={() => setWsModalOpen(true)}
        />
        {backButton}
      </AppHeader>
      <main className="mx-auto max-w-[960px] px-6 py-10">{children}</main>
      <CreateWorkspaceDialog
        open={wsModalOpen}
        onOpenChange={setWsModalOpen}
        onCreated={handleWorkspaceCreated}
      />
    </div>
  );

  if (state.kind === 'loading') {
    return shell(
      <div aria-hidden="true">
        <Skeleton className="mb-8 h-7 w-64" />
        <Skeleton className="mb-3 h-3 w-24" />
        <div className="overflow-hidden rounded-xl border border-border bg-card">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="flex items-center gap-4 border-b border-border px-4 py-3 last:border-0">
              <Skeleton className="h-4 flex-1" />
              <Skeleton className="h-4 w-16" />
              <Skeleton className="h-4 w-20" />
            </div>
          ))}
        </div>
      </div>,
    );
  }

  if (state.kind === 'error') {
    return shell(<p className="text-destructive" role="alert">{state.message}</p>);
  }

  if (state.kind === 'no_access') {
    return shell(
      <div>
        <h1 className="text-2xl font-bold tracking-tight">{t('team.noAccess.title')}</h1>
        <p className="mt-3 text-muted-foreground" data-testid="no-access-message">
          {t('team.noAccess.message')}
        </p>
      </div>,
    );
  }

  const { membership, members, invites, audit } = state;

  return shell(
    <>
      <h1 className="mb-8 text-2xl font-bold tracking-tight">
        {t('team.title')} <span className="text-muted-foreground">— {membership.company_name}</span>
      </h1>

      <section className="mb-10">
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-muted-foreground">
          {t('team.members.title')}
        </h2>
        {memberError && (
          <p className="mb-3 text-sm text-destructive" role="alert" data-testid="member-error">
            {memberError}
          </p>
        )}
        {members.length === 0 ? (
          <p className="text-muted-foreground">{t('team.members.empty')}</p>
        ) : (
          <div className="overflow-hidden rounded-xl border border-border bg-card">
            <table className="w-full text-sm" data-testid="members-table">
              <thead>
                <tr className="border-b border-border text-left text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                  <th className="px-4 py-2.5 font-semibold">{t('team.members.colEmail')}</th>
                  <th className="px-4 py-2.5 font-semibold">{t('team.members.colRole')}</th>
                  <th className="px-4 py-2.5 font-semibold">{t('team.members.colJoined')}</th>
                  <th className="px-4 py-2.5 font-semibold">{t('team.members.colActions')}</th>
                </tr>
              </thead>
              <tbody>
                {members.map((m) => {
                  const isSelf = m.user_id === currentUserId;
                  return (
                    <tr
                      key={m.user_id}
                      data-testid={`member-row-${m.email}`}
                      className="border-b border-border last:border-0"
                    >
                      <td className="px-4 py-2.5">
                        {m.email}
                        {isSelf && <span className="ml-1.5 text-xs text-muted-foreground">{t('team.members.you')}</span>}
                      </td>
                      <td className="px-4 py-2.5 text-muted-foreground">
                        {isSelf ? (
                          m.role
                        ) : (
                          <select
                            className={NATIVE_SELECT_CLASS}
                            value={m.role}
                            disabled={updatingRoles.has(m.user_id)}
                            onChange={(e) => handleRoleChange(m, e.target.value as 'admin' | 'interviewer')}
                            data-testid={`member-role-${m.email}`}
                          >
                            <option value="interviewer">interviewer</option>
                            <option value="admin">admin</option>
                          </select>
                        )}
                      </td>
                      <td className="px-4 py-2.5 text-muted-foreground">
                        {new Date(m.joined_at).toLocaleDateString()}
                      </td>
                      <td className="px-4 py-2.5">
                        {!isSelf && (
                          <Button
                            type="button"
                            variant="ghost"
                            size="xs"
                            className="text-destructive hover:bg-destructive-soft hover:text-destructive"
                            onClick={() => setRemoveTarget(m)}
                            data-testid={`member-remove-${m.email}`}
                          >
                            {t('team.members.remove')}
                          </Button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section>
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
            {t('team.invites.title')}
          </h2>
          <Button size="sm" onClick={openModal} data-testid="invite-button">
            <UserPlus className="size-4" />
            {t('team.invite.button')}
          </Button>
        </div>
        {revokeError && (
          <p className="mb-3 text-sm text-destructive" role="alert" data-testid="revoke-error">
            {revokeError}
          </p>
        )}
        {invites.length === 0 ? (
          <p className="text-muted-foreground">{t('team.invites.empty')}</p>
        ) : (
          <div className="overflow-hidden rounded-xl border border-border bg-card">
            <table className="w-full text-sm" data-testid="invites-table">
              <thead>
                <tr className="border-b border-border text-left text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                  <th className="px-4 py-2.5 font-semibold">{t('team.invites.colEmail')}</th>
                  <th className="px-4 py-2.5 font-semibold">{t('team.invites.colRole')}</th>
                  <th className="px-4 py-2.5 font-semibold">{t('team.invites.colStatus')}</th>
                  <th className="px-4 py-2.5 font-semibold">{t('team.invites.colExpires')}</th>
                  <th className="px-4 py-2.5 font-semibold">{t('team.invites.colActions')}</th>
                </tr>
              </thead>
              <tbody>
                {invites.map((inv) => (
                  <tr
                    key={inv.id}
                    data-testid={`invite-row-${inv.id}`}
                    className="border-b border-border last:border-0"
                  >
                    <td className="px-4 py-2.5">{inv.email ?? '—'}</td>
                    <td className="px-4 py-2.5 text-muted-foreground">{inv.role}</td>
                    <td className="px-4 py-2.5">
                      <span
                        className={`rounded-full px-2.5 py-0.5 text-xs font-semibold ${
                          INVITE_STATUS_BADGE[inv.status] ?? 'bg-muted text-muted-foreground'
                        }`}
                      >
                        {t(`team.status.${inv.status}`)}
                      </span>
                    </td>
                    <td className="px-4 py-2.5 text-muted-foreground">
                      {new Date(inv.expires_at).toLocaleDateString()}
                    </td>
                    <td className="px-4 py-2.5">
                      <div className="flex items-center gap-2">
                        {inv.status === 'pending' && (
                          <Button
                            type="button"
                            variant="ghost"
                            size="xs"
                            className="text-destructive hover:bg-destructive-soft hover:text-destructive"
                            onClick={() => setRevokeTarget(inv.token)}
                            data-testid={`revoke-invite-${inv.id}`}
                          >
                            {t('team.revoke.button')}
                          </Button>
                        )}
                        {inv.accept_url && inv.status === 'pending' && (
                          <Button
                            type="button"
                            variant="outline"
                            size="xs"
                            onClick={() => handleCopyLink(inv.accept_url!, inv.token)}
                            data-testid={`copy-link-${inv.id}`}
                          >
                            {copiedToken === inv.token ? (
                              <Check className="size-3" />
                            ) : (
                              <Copy className="size-3" />
                            )}
                            {copiedToken === inv.token ? t('session.copy.done') : t('team.invite.copyLink')}
                          </Button>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="mt-10">
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-muted-foreground">
          {t('team.audit.title')}
        </h2>
        {audit.length === 0 ? (
          <p className="text-muted-foreground">{t('team.audit.empty')}</p>
        ) : (
          <ul className="flex flex-col gap-1.5" data-testid="audit-list">
            {audit.map((e) => (
              <li
                key={e.id}
                data-testid={`audit-row-${e.id}`}
                className="flex items-baseline justify-between gap-4 rounded-lg border border-border bg-card px-4 py-2.5 text-sm"
              >
                <span className="min-w-0 flex-1 truncate">{auditText(e)}</span>
                <span className="shrink-0 text-xs text-muted-foreground">
                  {new Date(e.created_at).toLocaleString()}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="mt-12 rounded-xl border border-destructive/40 bg-destructive-soft/30 p-5" data-testid="danger-zone">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-destructive">
          {t('team.danger.title')}
        </h2>
        <div className="mt-3 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <p className="text-sm text-muted-foreground">{t('team.danger.deleteBody')}</p>
          <Button
            type="button"
            variant="destructive"
            className="shrink-0"
            onClick={openDeleteModal}
            data-testid="delete-workspace-button"
          >
            {t('team.danger.deleteButton')}
          </Button>
        </div>
      </section>

      <Dialog open={deleteOpen} onOpenChange={(open) => { if (!deleting) setDeleteOpen(open); }}>
        <DialogContent data-testid="delete-workspace-modal" className="sm:max-w-[440px]">
          <DialogHeader>
            <DialogTitle>{t('team.danger.confirmTitle')}</DialogTitle>
            <DialogDescription>
              {t('team.danger.confirmBody', { name: membership.company_name })}
            </DialogDescription>
          </DialogHeader>
          <form
            className="flex flex-col gap-4"
            onSubmit={(e) => { e.preventDefault(); handleDeleteWorkspace(); }}
            data-testid="delete-workspace-form"
          >
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="delete-workspace-input">
                {t('team.danger.confirmLabel', { name: membership.company_name })}
              </Label>
              <Input
                id="delete-workspace-input"
                value={deleteConfirmName}
                onChange={(e) => setDeleteConfirmName(e.target.value)}
                autoComplete="off"
                data-testid="delete-workspace-input"
              />
            </div>
            {deleteError && (
              <p className="text-sm text-destructive" role="alert" data-testid="delete-workspace-error">
                {deleteError}
              </p>
            )}
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => setDeleteOpen(false)}
                disabled={deleting}
                data-testid="delete-workspace-cancel"
              >
                {t('common.cancel')}
              </Button>
              <Button
                type="submit"
                variant="destructive"
                disabled={deleting || deleteConfirmName.trim() !== membership.company_name}
                data-testid="delete-workspace-confirm"
              >
                {deleting ? '…' : t('team.danger.deleteButton')}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog open={modal.kind !== 'closed'} onOpenChange={(open) => !open && setModal({ kind: 'closed' })}>
        <DialogContent data-testid="invite-modal" className="sm:max-w-[440px]">
          {modal.kind === 'created' ? (
            <>
              <DialogHeader>
                <DialogTitle>{t('team.invite.success')}</DialogTitle>
                <DialogDescription className="sr-only">{t('team.invite.acceptUrl')}</DialogDescription>
              </DialogHeader>
              <div className="flex flex-col gap-2 rounded-lg border border-border bg-muted/50 p-4">
                <p className="text-xs text-muted-foreground">{t('team.invite.acceptUrl')}</p>
                <code className="break-all font-mono text-xs" data-testid="invite-accept-url">
                  {modal.invite.accept_url ?? `${window.location.origin}/invite/${modal.invite.token}`}
                </code>
              </div>
              <DialogFooter>
                <Button type="button" variant="outline" onClick={() => setModal({ kind: 'closed' })} data-testid="modal-close">
                  {t('sessions.create.close')}
                </Button>
              </DialogFooter>
            </>
          ) : (
            <>
              <DialogHeader>
                <DialogTitle>{t('team.invite.title')}</DialogTitle>
                <DialogDescription className="sr-only">{t('team.invite.title')}</DialogDescription>
              </DialogHeader>
              <form className="flex flex-col gap-4" onSubmit={handleCreateInvite} data-testid="invite-form">
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="invite-email">{t('team.invite.email')}</Label>
                  <Input
                    id="invite-email"
                    type="email"
                    value={inviteEmail}
                    onChange={(e) => setInviteEmail(e.target.value)}
                    placeholder="colleague@example.com"
                    data-testid="invite-email-input"
                  />
                </div>
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="invite-role">{t('team.invite.role')}</Label>
                  <select
                    id="invite-role"
                    className={NATIVE_SELECT_CLASS}
                    value={inviteRole}
                    onChange={(e) => setInviteRole(e.target.value as 'admin' | 'interviewer')}
                    data-testid="invite-role-select"
                  >
                    <option value="interviewer">interviewer</option>
                    <option value="admin">admin</option>
                  </select>
                </div>
                {modalError && <p className="text-sm text-destructive" role="alert">{modalError}</p>}
                <DialogFooter>
                  <Button type="button" variant="outline" onClick={() => setModal({ kind: 'closed' })} data-testid="modal-cancel">
                    {t('team.invite.cancel')}
                  </Button>
                  <Button type="submit" disabled={modal.kind === 'submitting'} data-testid="invite-submit">
                    {modal.kind === 'submitting' ? '…' : t('team.invite.submit')}
                  </Button>
                </DialogFooter>
              </form>
            </>
          )}
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={revokeTarget !== null}
        onOpenChange={(open) => { if (!open) setRevokeTarget(null); }}
        title={t('team.revoke.confirmTitle')}
        description={t('team.revoke.confirm')}
        confirmLabel={t('team.revoke.button')}
        cancelLabel={t('common.cancel')}
        onConfirm={() => { if (revokeTarget !== null) handleRevoke(revokeTarget); }}
        destructive
        testId="revoke-invite-confirm"
      />
      <ConfirmDialog
        open={removeTarget !== null}
        onOpenChange={(open) => { if (!open) setRemoveTarget(null); }}
        title={t('team.members.removeConfirmTitle')}
        description={removeTarget ? t('team.members.removeConfirm', { email: removeTarget.email }) : undefined}
        confirmLabel={t('team.members.remove')}
        cancelLabel={t('common.cancel')}
        onConfirm={() => { if (removeTarget) handleRemoveMember(removeTarget); }}
        destructive
        testId="member-remove-confirm"
      />
    </>,
  );
}
