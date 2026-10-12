import { useCallback, useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import yaml from 'js-yaml';
import { Plus, Trash2 } from 'lucide-react';
import { getMe, logout, type MembershipInfo } from '../api/auth';
import {
  createTask,
  getTask,
  updateTask,
  TaskApiError,
  type EditableTaskVisibility,
  type TaskCreatePayload,
  type TaskStarterFile,
} from '../api/rest';
import { LanguageSwitcher } from '../components/LanguageSwitcher';
import { Markdown } from '../components/Markdown';
import { AppHeader } from '@/components/AppHeader';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { NATIVE_SELECT_CLASS } from '@/lib/formClasses';

// Mirror of the backend ALLOWED_RUNTIME_IMAGES whitelist (tasks/images.py).
const RUNTIME_IMAGES = ['vibony/sandbox-py:3.12', 'vibony/sandbox-node:20'];

const TEXTAREA_CLASS =
  'min-h-24 w-full rounded-md border border-input bg-transparent px-3 py-2 text-sm font-mono shadow-sm ' +
  'focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring';

function dumpYaml(obj: Record<string, unknown>): string {
  if (!obj || Object.keys(obj).length === 0) return '';
  return yaml.dump(obj, { indent: 2, lineWidth: 100, noRefs: true });
}

/** Parse a YAML mapping from the editor; '' → {}. Throws on bad YAML / non-mapping. */
function parseYamlMapping(text: string, label: string): Record<string, unknown> {
  const trimmed = text.trim();
  if (!trimmed) return {};
  let parsed: unknown;
  try {
    parsed = yaml.load(trimmed);
  } catch (err) {
    throw new Error(`${label}: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (parsed === null || parsed === undefined) return {};
  if (typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error(`${label}: must be a YAML mapping (key: value)`);
  }
  return parsed as Record<string, unknown>;
}

export function TaskEditPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { id } = useParams<{ id?: string }>();
  const isEdit = Boolean(id);

  const [loading, setLoading] = useState(isEdit);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [readOnly, setReadOnly] = useState(false);
  const [memberships, setMemberships] = useState<MembershipInfo[]>([]);

  // form fields
  const [slug, setSlug] = useState('');
  const [title, setTitle] = useState('');
  const [descriptionMd, setDescriptionMd] = useState('');
  const [descPreview, setDescPreview] = useState(false);
  const [runtimeImage, setRuntimeImage] = useState(RUNTIME_IMAGES[0]!);
  const [visibility, setVisibility] = useState<EditableTaskVisibility>('workspace');
  const [workspaceId, setWorkspaceId] = useState('');
  const [starterFiles, setStarterFiles] = useState<TaskStarterFile[]>([]);
  const [weaknessYaml, setWeaknessYaml] = useState('');
  const [specYaml, setSpecYaml] = useState('');

  useEffect(() => {
    const controller = new AbortController();
    getMe({ signal: controller.signal })
      .then((me) => {
        setMemberships(me.memberships);
        if (me.memberships.length === 1) setWorkspaceId(me.memberships[0]!.workspace_id);
      })
      .catch(() => {/* RequireAuth handles 401 */});

    if (isEdit && id) {
      getTask(id, { signal: controller.signal })
        .then((task) => {
          setSlug(task.slug);
          setTitle(task.title);
          setDescriptionMd(task.description_md);
          setRuntimeImage(task.runtime_image);
          setVisibility(task.visibility === 'private' ? 'private' : 'workspace');
          setStarterFiles(task.starter_files);
          setWeaknessYaml(dumpYaml(task.weakness));
          setSpecYaml(dumpYaml(task.spec));
          setReadOnly(!task.can_edit);
        })
        .catch((err: unknown) => {
          if (err instanceof DOMException && err.name === 'AbortError') return;
          if (err instanceof TaskApiError && err.status === 401) {
            navigate('/login', { replace: true });
            return;
          }
          setError(err instanceof Error ? err.message : String(err));
        })
        .finally(() => {
          if (!controller.signal.aborted) setLoading(false);
        });
    }
    return () => controller.abort();
  }, [id, isEdit, navigate]);

  const handleLogout = useCallback(async () => {
    await logout();
    navigate('/login', { replace: true });
  }, [navigate]);

  const addFile = () => setStarterFiles((prev) => [...prev, { path: '', content: '' }]);
  const removeFile = (i: number) => setStarterFiles((prev) => prev.filter((_, idx) => idx !== i));
  const updateFile = (i: number, patch: Partial<TaskStarterFile>) =>
    setStarterFiles((prev) => prev.map((f, idx) => (idx === i ? { ...f, ...patch } : f)));

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    let weakness: Record<string, unknown>;
    let spec: Record<string, unknown>;
    try {
      weakness = parseYamlMapping(weaknessYaml, t('taskEdit.weaknessLabel'));
      spec = parseYamlMapping(specYaml, t('taskEdit.specLabel'));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      return;
    }

    const files = starterFiles
      .map((f) => ({ path: f.path.trim(), content: f.content }))
      .filter((f) => f.path);

    setSaving(true);
    try {
      if (isEdit && id) {
        await updateTask(id, {
          title,
          description_md: descriptionMd,
          starter_files: files,
          runtime_image: runtimeImage,
          weakness,
          spec,
          visibility,
        });
      } else {
        const payload: TaskCreatePayload = {
          slug,
          title,
          description_md: descriptionMd,
          starter_files: files,
          runtime_image: runtimeImage,
          weakness,
          spec,
          visibility,
          workspace_id: workspaceId || null,
        };
        await createTask(payload);
      }
      navigate('/tasks');
    } catch (err) {
      if (err instanceof TaskApiError && err.reason === 'slug_taken') {
        setError(t('taskEdit.slugTaken', { slug }));
      } else {
        setError(err instanceof Error ? err.message : String(err));
      }
      setSaving(false);
    }
  };

  return (
    <div className="min-h-screen bg-background">
      <AppHeader>
        <LanguageSwitcher />
        <Button asChild variant="ghost" size="sm">
          <Link to="/tasks">{t('taskEdit.backToTasks')}</Link>
        </Button>
        <Button variant="outline" size="sm" onClick={handleLogout} data-testid="logout-button">
          {t('auth.logout.button')}
        </Button>
      </AppHeader>

      <main className="mx-auto max-w-[760px] px-6 py-10">
        <h1 className="mb-7 text-2xl font-bold tracking-tight">
          {isEdit ? t('taskEdit.editTitle') : t('taskEdit.newTitle')}
        </h1>

        {loading ? (
          <p className="text-muted-foreground" data-testid="task-edit-loading">
            {t('taskEdit.loading')}
          </p>
        ) : (
          <form className="flex flex-col gap-6" onSubmit={handleSubmit} data-testid="task-edit-form">
            {readOnly && (
              <p className="rounded-md bg-warn-soft px-3 py-2 text-sm text-warn" data-testid="task-readonly-notice">
                {t('taskEdit.readOnlyNotice')}
              </p>
            )}

            {/* Basics */}
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="slug">{t('taskEdit.slugLabel')}</Label>
                <Input
                  id="slug"
                  value={slug}
                  onChange={(e) => setSlug(e.target.value)}
                  disabled={isEdit}
                  required
                  placeholder="my-task"
                  data-testid="slug-input"
                />
                {isEdit && <p className="text-xs text-muted-foreground">{t('taskEdit.slugImmutable')}</p>}
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="title">{t('taskEdit.titleLabel')}</Label>
                <Input
                  id="title"
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  required
                  data-testid="title-input"
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="runtime-image">{t('taskEdit.imageLabel')}</Label>
                <select
                  id="runtime-image"
                  className={NATIVE_SELECT_CLASS}
                  value={runtimeImage}
                  onChange={(e) => setRuntimeImage(e.target.value)}
                  data-testid="image-select"
                >
                  {RUNTIME_IMAGES.map((img) => (
                    <option key={img} value={img}>
                      {img}
                    </option>
                  ))}
                </select>
              </div>
              <div className="flex flex-col gap-1.5">
                <Label id="vis-label">{t('taskEdit.visibilityLabel')}</Label>
                <div className="flex gap-4 pt-1.5" role="radiogroup" aria-labelledby="vis-label">
                  {(['workspace', 'private'] as const).map((v) => (
                    <label key={v} className="flex cursor-pointer items-center gap-1.5 text-sm">
                      <input
                        type="radio"
                        name="visibility"
                        value={v}
                        checked={visibility === v}
                        onChange={() => setVisibility(v)}
                        className="accent-primary"
                        data-testid={`visibility-${v}`}
                      />
                      {t(`tasks.visibility.${v}`)}
                    </label>
                  ))}
                </div>
              </div>
            </div>

            {!isEdit && memberships.length > 1 && (
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="workspace">{t('taskEdit.workspaceLabel')}</Label>
                <select
                  id="workspace"
                  className={NATIVE_SELECT_CLASS}
                  value={workspaceId}
                  onChange={(e) => setWorkspaceId(e.target.value)}
                  required
                  data-testid="workspace-select"
                >
                  <option value="">—</option>
                  {memberships.map((m) => (
                    <option key={m.workspace_id} value={m.workspace_id}>
                      {m.company_name}
                    </option>
                  ))}
                </select>
              </div>
            )}

            {/* Description (markdown) */}
            <div className="flex flex-col gap-1.5">
              <div className="flex items-center justify-between">
                <Label htmlFor="description">{t('taskEdit.descriptionLabel')}</Label>
                <button
                  type="button"
                  className="text-xs text-primary hover:underline"
                  onClick={() => setDescPreview((p) => !p)}
                  data-testid="desc-preview-toggle"
                >
                  {descPreview ? t('taskEdit.descEdit') : t('taskEdit.descPreview')}
                </button>
              </div>
              {descPreview ? (
                <div className="min-h-24 rounded-md border border-input px-3 py-2 text-sm" data-testid="desc-preview">
                  <Markdown source={descriptionMd || `_${t('taskEdit.descEmpty')}_`} />
                </div>
              ) : (
                <textarea
                  id="description"
                  className={`${TEXTAREA_CLASS} min-h-32`}
                  value={descriptionMd}
                  onChange={(e) => setDescriptionMd(e.target.value)}
                  data-testid="description-input"
                />
              )}
              <p className="text-xs text-muted-foreground">{t('taskEdit.descriptionHint')}</p>
            </div>

            {/* Starter files */}
            <div className="flex flex-col gap-2">
              <Label>{t('taskEdit.starterFilesLabel')}</Label>
              {starterFiles.map((f, i) => (
                <div key={i} className="flex flex-col gap-1.5 rounded-md border border-border p-3" data-testid={`starter-file-${i}`}>
                  <div className="flex items-center gap-2">
                    <Input
                      value={f.path}
                      onChange={(e) => updateFile(i, { path: e.target.value })}
                      placeholder="train.py"
                      className="font-mono"
                      data-testid={`starter-path-${i}`}
                    />
                    <Button
                      type="button"
                      variant="ghost"
                      size="xs"
                      onClick={() => removeFile(i)}
                      data-testid={`starter-remove-${i}`}
                    >
                      <Trash2 className="size-3 text-destructive" />
                    </Button>
                  </div>
                  <textarea
                    className={TEXTAREA_CLASS}
                    value={f.content}
                    onChange={(e) => updateFile(i, { content: e.target.value })}
                    data-testid={`starter-content-${i}`}
                  />
                </div>
              ))}
              <Button type="button" variant="outline" size="sm" onClick={addFile} data-testid="add-file-button">
                <Plus className="size-3" />
                {t('taskEdit.addFile')}
              </Button>
            </div>

            {/* Weakness (YAML) */}
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="weakness">{t('taskEdit.weaknessLabel')}</Label>
              <textarea
                id="weakness"
                className={`${TEXTAREA_CLASS} min-h-40`}
                value={weaknessYaml}
                onChange={(e) => setWeaknessYaml(e.target.value)}
                spellCheck={false}
                data-testid="weakness-input"
              />
              <p className="text-xs text-muted-foreground">{t('taskEdit.weaknessHint')}</p>
            </div>

            {/* Spec (YAML) */}
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="spec">{t('taskEdit.specLabel')}</Label>
              <textarea
                id="spec"
                className={`${TEXTAREA_CLASS} min-h-40`}
                value={specYaml}
                onChange={(e) => setSpecYaml(e.target.value)}
                spellCheck={false}
                data-testid="spec-input"
              />
              <p className="text-xs text-muted-foreground">{t('taskEdit.specHint')}</p>
            </div>

            {error && (
              <p className="text-sm text-destructive" role="alert" data-testid="task-edit-error">
                {error}
              </p>
            )}

            <div className="flex justify-end gap-2">
              <Button asChild type="button" variant="outline">
                <Link to="/tasks">{t('taskEdit.cancel')}</Link>
              </Button>
              <Button type="submit" disabled={saving || readOnly} data-testid="save-button">
                {saving ? '…' : t('taskEdit.save')}
              </Button>
            </div>
          </form>
        )}
      </main>
    </div>
  );
}
