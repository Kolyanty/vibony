import { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Pencil, Plus, Search, Star, Trash2 } from 'lucide-react';
import { AuthError, getMe, logout, type MembershipInfo } from '../api/auth';
import {
  listTasks,
  deleteTask,
  setTaskFavorite,
  TaskApiError,
  type TaskListItem,
  type TaskVisibility,
} from '../api/rest';
import { cn } from '@/lib/utils';
import { LanguageSwitcher } from '../components/LanguageSwitcher';
import { WorkspaceSwitcher } from '../components/WorkspaceSwitcher';
import { CreateWorkspaceDialog } from '../components/CreateWorkspaceDialog';
import { useActiveWorkspace } from '../state/activeWorkspace';
import { AppHeader } from '@/components/AppHeader';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';

const VISIBILITY_BADGE: Record<TaskVisibility, string> = {
  private: 'bg-warn-soft text-warn',
  workspace: 'bg-primary-soft text-primary',
  public: 'bg-success-soft text-success',
};

type DeleteState = { kind: 'closed' } | { kind: 'confirm'; task: TaskListItem } | { kind: 'deleting'; task: TaskListItem };

export function TasksPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [tasks, setTasks] = useState<TaskListItem[]>([]);
  const [memberships, setMemberships] = useState<MembershipInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [del, setDel] = useState<DeleteState>({ kind: 'closed' });
  const [wsModalOpen, setWsModalOpen] = useState(false);
  // Task ids with an in-flight favorite toggle — guards against a racing re-click.
  const [favBusy, setFavBusy] = useState<Set<string>>(() => new Set());
  const [search, setSearch] = useState('');
  // The skeleton shows only on the first load; search refetches replace the list in
  // place, so typing doesn't flash the whole page.
  const didInitialLoad = useRef(false);

  const { activeId, active, setActiveId } = useActiveWorkspace(memberships);

  // Memberships load once (they drive the workspace switcher, not the search).
  useEffect(() => {
    const controller = new AbortController();
    getMe({ signal: controller.signal })
      .then((me) => setMemberships(me.memberships))
      .catch((err: unknown) => {
        if (err instanceof DOMException && err.name === 'AbortError') return;
        if (err instanceof AuthError && err.reason === 'unauthorized') {
          navigate('/login', { replace: true });
        }
      });
    return () => controller.abort();
  }, [navigate]);

  // Tasks load on mount and whenever the search term changes (debounced when typing).
  // The description-keyword match happens server-side via `?q=`.
  useEffect(() => {
    const controller = new AbortController();
    const run = () => {
      if (!didInitialLoad.current) setLoading(true);
      listTasks({ signal: controller.signal, q: search })
        .then((tk) => {
          setTasks(tk);
          setError(null);
        })
        .catch((err: unknown) => {
          if (err instanceof DOMException && err.name === 'AbortError') return;
          // Mid-session cookie expiry → redirect (RequireAuth covers the mount case).
          if (err instanceof TaskApiError && err.status === 401) {
            navigate('/login', { replace: true });
            return;
          }
          setError(err instanceof Error ? err.message : String(err));
        })
        .finally(() => {
          // Only mark the initial load done on a completed (non-aborted) run, so a
          // StrictMode dev double-mount doesn't skip the skeleton on the real first load.
          if (!controller.signal.aborted) {
            didInitialLoad.current = true;
            setLoading(false);
          }
        });
    };
    const handle = window.setTimeout(run, search ? 250 : 0);
    return () => {
      window.clearTimeout(handle);
      controller.abort();
    };
  }, [search, navigate]);

  const handleLogout = useCallback(async () => {
    await logout();
    navigate('/login', { replace: true });
  }, [navigate]);

  const handleWorkspaceCreated = (m: MembershipInfo) => {
    setMemberships((prev) => [...prev, m]);
    setActiveId(m.workspace_id);
  };

  // Templates are always shown; workspace tasks are scoped to the active workspace.
  const visibleTasks = activeId
    ? tasks.filter((tk) => tk.is_template || tk.company_id === activeId)
    : tasks;

  // Favorites float into their own section at the top of the catalog.
  const favoriteTasks = visibleTasks.filter((tk) => tk.is_favorite);
  const otherTasks = visibleTasks.filter((tk) => !tk.is_favorite);

  // Optimistically flip the star, then persist; revert + surface the error on failure.
  // An in-flight guard drops a re-click on the same task until its request settles, so
  // a rapid double-click can't race a PUT against a DELETE and desync the star.
  const toggleFavorite = async (tk: TaskListItem) => {
    if (favBusy.has(tk.id)) return;
    const next = !tk.is_favorite;
    setFavBusy((prev) => new Set(prev).add(tk.id));
    setTasks((prev) => prev.map((t) => (t.id === tk.id ? { ...t, is_favorite: next } : t)));
    try {
      await setTaskFavorite(tk.id, next);
    } catch (err) {
      setTasks((prev) => prev.map((t) => (t.id === tk.id ? { ...t, is_favorite: !next } : t)));
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setFavBusy((prev) => {
        const nextBusy = new Set(prev);
        nextBusy.delete(tk.id);
        return nextBusy;
      });
    }
  };

  const confirmDelete = async () => {
    if (del.kind !== 'confirm') return;
    const task = del.task;
    setDel({ kind: 'deleting', task });
    try {
      await deleteTask(task.id);
      setTasks((prev) => prev.filter((tk) => tk.id !== task.id));
      setDel({ kind: 'closed' });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setDel({ kind: 'closed' });
    }
  };

  const renderRow = (tk: TaskListItem) => (
    <div
      key={tk.id}
      data-testid={`task-row-${tk.id}`}
      className="flex items-center gap-4 rounded-xl border border-border bg-card px-5 py-4 shadow-sm"
    >
      <button
        type="button"
        onClick={() => toggleFavorite(tk)}
        aria-label={t(tk.is_favorite ? 'tasks.favorite.remove' : 'tasks.favorite.add')}
        aria-pressed={tk.is_favorite}
        data-testid={`task-favorite-${tk.id}`}
        className="shrink-0 rounded-md p-1 text-muted-foreground transition-colors hover:text-warn"
      >
        <Star className={cn('size-4', tk.is_favorite && 'fill-warn text-warn')} />
      </button>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="truncate font-semibold">{tk.title}</span>
          {tk.is_template && (
            <span className="rounded-full bg-muted px-2 py-0.5 text-xs font-semibold text-muted-foreground">
              {t('tasks.templateTag')}
            </span>
          )}
        </div>
        <span className="font-mono text-xs text-muted-foreground">{tk.slug}</span>
      </div>
      <span
        className={`rounded-full px-2.5 py-0.5 text-xs font-semibold ${VISIBILITY_BADGE[tk.visibility]}`}
        data-testid={`task-visibility-${tk.id}`}
      >
        {t(`tasks.visibility.${tk.visibility}`)}
      </span>
      {tk.can_edit ? (
        <div className="flex shrink-0 items-center gap-1.5">
          <Button
            variant="outline"
            size="xs"
            onClick={() => navigate(`/tasks/${tk.id}/edit`)}
            data-testid={`task-edit-${tk.id}`}
          >
            <Pencil className="size-3" />
            {t('tasks.edit')}
          </Button>
          <Button
            variant="ghost"
            size="xs"
            onClick={() => setDel({ kind: 'confirm', task: tk })}
            data-testid={`task-delete-${tk.id}`}
          >
            <Trash2 className="size-3 text-destructive" />
          </Button>
        </div>
      ) : (
        <span className="shrink-0 text-xs text-muted-foreground">{t('tasks.readOnly')}</span>
      )}
    </div>
  );

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
        <Button asChild variant="ghost" size="sm">
          <Link to="/">{t('tasks.sessionsNav')}</Link>
        </Button>
        <Button variant="outline" size="sm" onClick={handleLogout} data-testid="logout-button">
          {t('auth.logout.button')}
        </Button>
      </AppHeader>

      <main className="mx-auto max-w-[960px] px-6 py-10">
        <div className="mb-7 flex items-center justify-between">
          <h1 className="text-2xl font-bold tracking-tight">{t('tasks.title')}</h1>
          <Button onClick={() => navigate('/tasks/new')} data-testid="new-task-button">
            <Plus className="size-4" />
            {t('tasks.new')}
          </Button>
        </div>

        <div className="relative mb-4">
          <Search
            className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground"
            aria-hidden="true"
          />
          <Input
            type="search"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t('tasks.searchPlaceholder')}
            aria-label={t('tasks.searchPlaceholder')}
            className="pl-9"
            data-testid="tasks-search"
          />
        </div>

        {error && (
          <p className="mb-4 text-sm text-destructive" role="alert" data-testid="tasks-error">
            {error}
          </p>
        )}

        {loading ? (
          <div className="flex flex-col gap-2.5" data-testid="tasks-loading" aria-hidden="true">
            {Array.from({ length: 3 }).map((_, i) => (
              <div key={i} className="flex items-center gap-4 rounded-xl border border-border bg-card px-5 py-4">
                <Skeleton className="h-4 w-40" />
                <Skeleton className="h-4 flex-1" />
                <Skeleton className="h-5 w-16 rounded-full" />
              </div>
            ))}
          </div>
        ) : visibleTasks.length === 0 ? (
          <div
            className="rounded-xl border border-dashed border-border bg-card/50 px-6 py-16 text-center text-muted-foreground"
            data-testid="tasks-empty"
          >
            {t(search ? 'tasks.searchEmpty' : 'tasks.empty')}
          </div>
        ) : (
          <div className="flex flex-col gap-6" data-testid="tasks-table">
            {favoriteTasks.length > 0 && (
              <section data-testid="tasks-favorites-section">
                <h2 className="mb-2.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                  {t('tasks.group.favorites')}
                </h2>
                <div className="flex flex-col gap-2.5">{favoriteTasks.map(renderRow)}</div>
              </section>
            )}
            {otherTasks.length > 0 && (
              <section>
                {favoriteTasks.length > 0 && (
                  <h2 className="mb-2.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                    {t('tasks.group.other')}
                  </h2>
                )}
                <div className="flex flex-col gap-2.5">{otherTasks.map(renderRow)}</div>
              </section>
            )}
          </div>
        )}
      </main>

      <Dialog open={del.kind !== 'closed'} onOpenChange={(open) => !open && setDel({ kind: 'closed' })}>
        <DialogContent data-testid="delete-modal" className="sm:max-w-[420px]">
          <DialogHeader>
            <DialogTitle>{t('tasks.delete.title')}</DialogTitle>
            <DialogDescription>
              {del.kind !== 'closed' ? t('tasks.delete.body', { title: del.task.title }) : ''}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDel({ kind: 'closed' })} data-testid="delete-cancel">
              {t('tasks.delete.cancel')}
            </Button>
            <Button
              variant="destructive"
              onClick={confirmDelete}
              disabled={del.kind === 'deleting'}
              data-testid="delete-confirm"
            >
              {del.kind === 'deleting' ? '…' : t('tasks.delete.confirm')}
            </Button>
          </DialogFooter>
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
