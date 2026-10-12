import { Link } from 'react-router-dom';
import { Logo } from '@/components/Logo';

/*
 * Top app bar for the authed employer screens (sessions list, team).
 * Brand mark on the left, page actions passed as children on the right.
 */
export function AppHeader({ children }: { children?: React.ReactNode }) {
  return (
    <header className="border-b border-border bg-card">
      <div className="mx-auto flex max-w-[960px] items-center justify-between px-6 py-3">
        <Link
          to="/"
          className="flex items-center gap-2.5 text-[17px] font-extrabold tracking-tight text-foreground hover:no-underline"
        >
          <Logo variant="tile" className="size-7" />
          Vibony
        </Link>
        <div className="flex items-center gap-2.5">{children}</div>
      </div>
    </header>
  );
}
