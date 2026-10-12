import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { createWorkspace } from '../api/rest';
import { apiErrorMessage } from '@/lib/apiError';
import type { MembershipInfo } from '../api/auth';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';

/*
 * Create-workspace modal. Shared by the header switcher ("New workspace")
 * and the zero-membership onboarding prompt. On success the parent gets the new
 * membership so it can append it + switch to it.
 */
export function CreateWorkspaceDialog({
  open,
  onOpenChange,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: (membership: MembershipInfo) => void;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Clear a stale error each time the dialog is (re)opened.
  useEffect(() => {
    if (open) setError(null);
  }, [open]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    setSubmitting(true);
    setError(null);
    try {
      const membership = await createWorkspace(name.trim());
      setName('');
      onCreated(membership);
      onOpenChange(false);
    } catch (err) {
      setError(apiErrorMessage(err, t));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(o) => !submitting && onOpenChange(o)}>
      <DialogContent data-testid="create-workspace-modal" className="sm:max-w-[420px]">
        <DialogHeader>
          <DialogTitle>{t('workspace.create.title')}</DialogTitle>
          <DialogDescription>{t('workspace.create.body')}</DialogDescription>
        </DialogHeader>
        <form className="flex flex-col gap-4" onSubmit={handleSubmit} data-testid="create-workspace-form">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="workspace-name">{t('workspace.create.nameLabel')}</Label>
            <Input
              id="workspace-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoFocus
              required
              maxLength={200}
              data-testid="workspace-name-input"
            />
          </div>
          {error && (
            <p className="text-sm text-destructive" role="alert" data-testid="create-workspace-error">
              {error}
            </p>
          )}
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={submitting}
              data-testid="create-workspace-cancel"
            >
              {t('workspace.create.cancel')}
            </Button>
            <Button type="submit" disabled={submitting || !name.trim()} data-testid="create-workspace-submit">
              {submitting ? '…' : t('workspace.create.submit')}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
