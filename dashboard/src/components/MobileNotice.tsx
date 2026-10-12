import { useTranslation } from 'react-i18next';
import { MonitorSmartphone } from 'lucide-react';
import { Logo } from '@/components/Logo';

/*
 * Full-screen "open on a laptop" notice shown only below the `md` breakpoint.
 * The IDE layouts (interviewer SessionView, CandidatePage) assume a wide screen;
 * until a real mobile layout lands (later milestone), small screens get this
 * overlay instead of a cramped, unusable grid. Rendered as a sibling overlay so
 * the host page needs no restructuring.
 */
export function MobileNotice() {
  const { t } = useTranslation();
  return (
    <div className="fixed inset-0 z-[60] flex flex-col items-center justify-center gap-5 bg-background px-8 text-center md:hidden">
      <span className="flex size-14 items-center justify-center rounded-2xl bg-primary-soft text-primary">
        <MonitorSmartphone className="size-7" />
      </span>
      <div>
        <h1 className="text-lg font-bold tracking-tight">{t('mobile.notice.title')}</h1>
        <p className="mx-auto mt-2 max-w-[320px] text-sm leading-relaxed text-muted-foreground">
          {t('mobile.notice.body')}
        </p>
      </div>
      <span className="mt-2 flex items-center gap-2 text-sm font-extrabold tracking-tight text-foreground">
        <Logo variant="tile" className="size-6" />
        Vibony
      </span>
    </div>
  );
}
