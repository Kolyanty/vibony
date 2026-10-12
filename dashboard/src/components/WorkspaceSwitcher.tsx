import { useTranslation } from 'react-i18next';
import { Check, ChevronsUpDown, Plus } from 'lucide-react';
import type { MembershipInfo } from '../api/auth';
import { trialDaysLeft } from '../state/activeWorkspace';
import { buttonVariants } from '@/components/ui/button-variants';
import { cn } from '@/lib/utils';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';

/*
 * Workspace switcher — dropdown in AppHeader. Shows the active workspace
 * name + a trial badge, lists every membership for switching, and a "New
 * workspace" entry. The active workspace plan/trial drives the badge + the
 * (placeholder) upgrade CTA (no billing yet).
 */
export function WorkspaceSwitcher({
  memberships,
  active,
  onSelect,
  onCreate,
}: {
  memberships: MembershipInfo[];
  active: MembershipInfo | null;
  onSelect: (id: string) => void;
  onCreate: () => void;
}) {
  const { t } = useTranslation();
  if (!active) return null;

  const days = active.plan === 'trial' ? trialDaysLeft(active.trial_ends_at) : null;

  return (
    <div className="flex items-center gap-2" data-testid="workspace-switcher">
      <DropdownMenu>
        {/* Radix Trigger renders its own <button>; style it directly (rather than
            asChild + <Button>) so its ref attaches cleanly under React 18. */}
        <DropdownMenuTrigger
          className={cn(buttonVariants({ variant: 'outline', size: 'sm' }))}
          data-testid="workspace-switcher-trigger"
        >
          <span className="max-w-[140px] truncate font-medium">{active.company_name}</span>
          <ChevronsUpDown className="size-3.5 opacity-60" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="min-w-[220px]">
          <DropdownMenuLabel>{t('workspace.switcher.label')}</DropdownMenuLabel>
          <DropdownMenuSeparator />
          {memberships.map((m) => (
            <DropdownMenuItem
              key={m.workspace_id}
              onSelect={() => onSelect(m.workspace_id)}
              data-testid={`workspace-option-${m.workspace_id}`}
              className="flex items-center justify-between gap-2"
            >
              <span className="truncate">{m.company_name}</span>
              {m.workspace_id === active.workspace_id && <Check className="size-3.5 shrink-0" />}
            </DropdownMenuItem>
          ))}
          <DropdownMenuSeparator />
          <DropdownMenuItem onSelect={onCreate} data-testid="workspace-create-option">
            <Plus className="size-3.5" />
            {t('workspace.create.button')}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      {active.plan === 'trial' && (
        <span
          className="hidden items-center gap-1.5 rounded-full bg-primary-soft px-2.5 py-0.5 text-xs font-semibold text-primary sm:inline-flex"
          data-testid="workspace-plan-badge"
        >
          {days != null
            ? t('workspace.plan.trialDays', { count: days })
            : t('workspace.plan.trial')}
        </span>
      )}
    </div>
  );
}
