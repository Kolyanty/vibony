import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { ArrowRight, ExternalLink, MessageSquareCode, Eye, Sparkles } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Logo } from '@/components/Logo';

/*
 * Welcome page for a self-hosted Vibony instance, using the shared design tokens. Copy lives in the i18n `landing.*` namespace (en/ru).
 */

const REPOSITORY_URL = 'https://github.com/Kolyanty/vibony';
const DOCUMENTATION_URL = `${REPOSITORY_URL}#self-host-quick-start`;

function BrandMark() {
  return (
    <span className="flex items-center gap-2.5 font-extrabold text-[19px] tracking-tight">
      <Logo variant="tile" className="size-8" />
      Vibony
    </span>
  );
}

// Mock editor content for the hero preview. Real Python, 4-space indent.
// Only keywords and strings are colored (as a default IDE theme would) — plain
// identifiers like `torch`/`range` stay default-colored.
const KW = ({ children }: { children: React.ReactNode }) => (
  <span className="text-primary">{children}</span>
);
const STR = ({ children }: { children: React.ReactNode }) => (
  <span className="text-success">{children}</span>
);

const EDITOR_LINES: { n: number; code: React.ReactNode }[] = [
  { n: 12, code: <>best_loss = float(<STR>"inf"</STR>)</> },
  {
    n: 13,
    code: (
      <>
        <KW>for</KW> epoch <KW>in</KW> range(epochs):
      </>
    ),
  },
  { n: 14, code: <>{'    '}loss = train_one_epoch(model, loader)</> },
  {
    n: 15,
    code: (
      <>
        {'    '}
        <KW>if</KW> loss &lt; best_loss:
      </>
    ),
  },
  { n: 16, code: <>{'        '}best_loss = loss</> },
  {
    n: 17,
    code: (
      <>
        {'        '}torch.save(model.state_dict(), <STR>"best.pt"</STR>)
      </>
    ),
  },
];

function IdePreview() {
  const { t } = useTranslation();
  return (
    <div className="overflow-hidden rounded-[18px] border border-border bg-card shadow-[0_40px_80px_-44px_rgba(28,27,41,0.4),0_2px_8px_rgba(0,0,0,0.05)]">
      <div className="border-b border-border px-4 py-2 text-xs font-medium text-muted-foreground">
        {t('landing.preview.label')}
      </div>
      {/* title bar */}
      <div className="flex items-center gap-2 border-b border-border bg-muted/60 px-4 py-3">
        <span className="size-[11px] rounded-full bg-[#fb7185]" />
        <span className="size-[11px] rounded-full bg-[#fbbf24]" />
        <span className="size-[11px] rounded-full bg-[#34d399]" />
        <span className="ml-2.5 font-mono text-xs text-muted-foreground">task-01-training-loop</span>

      </div>
      <div className="grid min-h-[300px] grid-cols-1 md:grid-cols-[minmax(0,300px)_1fr]">
        {/* chat */}
        <div className="flex flex-col gap-3 border-b border-border bg-muted/40 md:border-r md:border-b-0 p-4">
          <div className="rounded-[13px] border border-primary/25 bg-primary-soft p-3 text-[13.5px] leading-snug">
            <span className="mb-1 block text-[11px] font-bold uppercase tracking-wide text-[color:var(--accent-foreground)]">
              {t('landing.preview.candidate')}
            </span>
            {t('landing.preview.msg1')}
          </div>
          <div className="rounded-[13px] border border-border bg-card p-3 text-[13.5px] leading-snug text-muted-foreground">
            <span className="mb-1 flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-wide text-teal">
              <Sparkles className="size-3" /> {t('landing.preview.ai')}
            </span>
            {t('landing.preview.aiReply')}
          </div>
          <div className="rounded-[13px] border border-primary/25 bg-primary-soft p-3 text-[13.5px] leading-snug">
            <span className="mb-1 block text-[11px] font-bold uppercase tracking-wide text-[color:var(--accent-foreground)]">
              {t('landing.preview.candidate')}
            </span>
            {t('landing.preview.msg2')}
          </div>
        </div>
        {/* editor */}
        <div className="overflow-x-auto bg-card p-4 font-mono text-[13px] leading-[1.85] text-[#2b241d]">
          {EDITOR_LINES.map(({ n, code }) => (
            <div key={n} className="flex whitespace-pre">
              <span className="w-8 shrink-0 select-none text-right text-muted-foreground/45">{n}</span>
              <span className="pl-4">{code}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

const STEPS = [
  { icon: MessageSquareCode, key: 'step1' },
  { icon: Sparkles, key: 'step2' },
  { icon: Eye, key: 'step3' },
] as const;

export function LandingPage() {
  const { t } = useTranslation();
  return (
    <div className="min-h-screen bg-background text-foreground">
      <div className="mx-auto max-w-[1080px] px-5 sm:px-7">
        {/* nav */}
        <nav className="flex items-center justify-between py-5">
          <BrandMark />
          <div className="hidden items-center gap-7 text-sm font-semibold text-muted-foreground md:flex">
            <a href="#how" className="hover:text-foreground">{t('landing.nav.how')}</a>
            <a href={REPOSITORY_URL} className="hover:text-foreground">GitHub</a>
            <a href={DOCUMENTATION_URL} className="hover:text-foreground">{t('landing.nav.docs')}</a>
          </div>
          <div className="flex items-center gap-2.5">
            <Button asChild variant="outline">
              <Link to="/login">{t('landing.nav.login')}</Link>
            </Button>
          </div>
        </nav>

        <main>
          {/* hero */}
          <section
            className="pt-16 pb-8 text-center"
            style={{
              backgroundImage:
                'radial-gradient(620px 360px at 78% -6%, rgba(99,102,241,0.12), transparent 62%)',
            }}
          >
            <span className="inline-flex items-center gap-2 rounded-full border border-primary/20 bg-primary-soft px-3.5 py-1.5 text-xs sm:text-[13px] font-bold text-[color:var(--accent-foreground)]">
              <Logo variant="tile" className="size-3.5" /> {t('landing.hero.badge')}
            </span>
            <h1 className="mx-auto mt-5 max-w-[800px] text-[clamp(34px,6vw,58px)] font-extrabold leading-[1.05] tracking-tight">
              {t('landing.hero.titleBefore')}
              <span className="text-primary">{t('landing.hero.titleAccent')}</span>
            </h1>
            <p className="mx-auto mt-6 max-w-[560px] text-lg font-medium leading-relaxed text-muted-foreground">
              {t('landing.hero.subtitle')}
            </p>
            <div className="mt-7 flex flex-wrap items-center justify-center gap-3">
              <Button asChild size="lg">
                <Link to="/signup">
                  {t('landing.hero.ctaPrimary')} <ArrowRight className="size-4" />
                </Link>
              </Button>
              <Button asChild variant="outline" size="lg">
                <a href={REPOSITORY_URL}>
                  <ExternalLink className="size-4" /> {t('landing.hero.ctaGithub')}
                </a>
              </Button>
            </div>
            <p className="mt-3 text-sm text-muted-foreground">{t('landing.hero.accountNote')}</p>
            <p className="mt-7 text-[13.5px] font-semibold text-muted-foreground">
              {t('landing.hero.trust')}
            </p>
          </section>

          {/* product preview */}
          <section className="py-12">
            <IdePreview />
          </section>

          {/* how it works */}
          <section id="how" className="py-12">
            <h2 className="text-center text-3xl font-bold tracking-tight">{t('landing.how.title')}</h2>
            <div className="mt-10 grid gap-5 md:grid-cols-3">
              {STEPS.map((step, i) => (
                <div key={step.key} className="rounded-[16px] border border-border bg-card p-6 shadow-sm">
                  <div className="flex size-11 items-center justify-center rounded-[12px] bg-primary-soft text-primary">
                    <step.icon className="size-5" />
                  </div>
                  <div className="mt-4 flex items-baseline gap-2">
                    <span className="font-mono text-sm text-muted-foreground">0{i + 1}</span>
                    <h3 className="text-lg font-bold">{t(`landing.how.${step.key}.title`)}</h3>
                  </div>
                  <p className="mt-2 text-[15px] leading-relaxed text-muted-foreground">
                    {t(`landing.how.${step.key}.body`)}
                  </p>
                </div>
              ))}
            </div>
          </section>

          <p className="mx-auto max-w-[680px] text-center text-sm leading-relaxed text-muted-foreground">
            {t('landing.assessment.note')}{' '}
            <a href={`${REPOSITORY_URL}#evaluation-status`} className="underline underline-offset-4 hover:text-foreground">
              {t('landing.assessment.link')}
            </a>
          </p>

          {/* CTA band */}
          <section className="my-12 rounded-[20px] border border-border bg-foreground px-8 py-12 text-center">
            <h2 className="mx-auto max-w-[520px] text-3xl font-extrabold leading-tight tracking-tight text-background">
              {t('landing.cta.title')}
            </h2>
            <p className="mx-auto mt-4 max-w-[580px] text-sm leading-relaxed text-background/80">
              {t('landing.cta.providerNote')}
            </p>
            <div className="mt-7 flex flex-wrap items-center justify-center gap-3">
              <Button asChild size="lg" className="bg-primary text-primary-foreground hover:bg-primary/90">
                <Link to="/signup">
                  {t('landing.cta.button')} <ArrowRight className="size-4" />
                </Link>
              </Button>
            </div>
          </section>

        </main>

        {/* footer */}
        <footer className="flex flex-wrap items-center justify-between gap-5 border-t border-border py-7 text-[13px] font-semibold text-muted-foreground">
          <BrandMark />
          <div className="flex flex-wrap items-center gap-5">
            <a href={DOCUMENTATION_URL} className="hover:text-foreground">{t('landing.nav.docs')}</a>
            <a href={REPOSITORY_URL} className="hover:text-foreground">GitHub</a>
            <a href={`${REPOSITORY_URL}/blob/main/LICENSE`} className="hover:text-foreground">Apache-2.0</a>
          </div>
        </footer>
      </div>
    </div>
  );
}
