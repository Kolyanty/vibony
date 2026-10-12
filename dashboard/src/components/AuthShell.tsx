import { Link } from 'react-router-dom';
import { Logo } from '@/components/Logo';
import { Card } from '@/components/ui/card';

/*
 * Shared chrome for the auth surfaces (login / signup / accept-invite):
 * warm background, centered card, brand mark on top. Tailwind + shadcn on the
 * design tokens. Keeps the auth pages free of layout boilerplate.
 */
export function AuthShell({
  title,
  description,
  children,
  footer,
}: {
  title: string;
  description?: React.ReactNode;
  children: React.ReactNode;
  footer?: React.ReactNode;
}) {
  return (
    <main
      aria-labelledby="auth-title"
      className="flex min-h-screen flex-col items-center justify-center bg-background px-6 py-12"
      style={{
        backgroundImage:
          'radial-gradient(620px 420px at 50% -10%, rgba(99,102,241,0.10), transparent 62%)',
      }}
    >
      <Link to="/" className="mb-7 flex items-center gap-2.5 font-extrabold text-[19px] tracking-tight text-foreground hover:no-underline">
        <Logo variant="tile" className="size-8" />
        Vibony
      </Link>
      <Card className="w-full max-w-[400px] gap-0 p-8">
        <h1 id="auth-title" className="text-xl font-bold tracking-tight">{title}</h1>
        {description && (
          <p className="mt-2 text-sm leading-relaxed text-muted-foreground">{description}</p>
        )}
        <div className="mt-6">{children}</div>
      </Card>
      {footer && (
        <p className="mt-5 text-sm font-medium text-muted-foreground">{footer}</p>
      )}
    </main>
  );
}
