import * as React from 'react';

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import { buttonVariants } from '@/components/ui/button-variants';

export interface ConfirmDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: React.ReactNode;
  confirmLabel: string;
  cancelLabel: string;
  /** Fired when the user confirms; the dialog then closes (Radix default). */
  onConfirm: () => void;
  /** Style the confirm action as a destructive (red) button. */
  destructive?: boolean;
  /** data-testid on the AlertDialogContent (confirm/cancel get `-confirm`/`-cancel`). */
  testId?: string;
}

/**
 * Shadcn `AlertDialog`-based confirm modal replacing the native `window.confirm`
 * (F1): on-brand, non-blocking, focus-managed, role="alertdialog". The confirm
 * action closes the dialog by default (Radix); the caller's `onConfirm` runs the
 * actual side effect.
 */
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  cancelLabel,
  onConfirm,
  destructive = false,
  testId,
}: ConfirmDialogProps) {
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent
        data-testid={testId}
        className="sm:max-w-[440px]"
      >
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          {description !== undefined && (
            <AlertDialogDescription>{description}</AlertDialogDescription>
          )}
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel data-testid={testId ? `${testId}-cancel` : undefined}>
            {cancelLabel}
          </AlertDialogCancel>
          <AlertDialogAction
            onClick={onConfirm}
            data-testid={testId ? `${testId}-confirm` : undefined}
            className={destructive ? buttonVariants({ variant: 'destructive' }) : undefined}
          >
            {confirmLabel}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
