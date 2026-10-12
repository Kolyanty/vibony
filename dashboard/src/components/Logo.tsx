/*
 * Vibony brand mark — "vibe-wave": a tilde waveform (vibe-coding) flowing into a
 * prompt caret (you drive the AI). Name-agnostic: tied to the vibe-coding
 * category, not to the "Vibony" wordmark, so a rename only touches the wordmark.
 *
 * Variants:
 *   tile — dark rounded tile, white wave + teal caret (favicon, dark contexts)
 *   open — no tile, indigo wave + teal caret (on light backgrounds)
 *   mono — single `currentColor` (stamps, muted/disabled contexts)
 */
type LogoVariant = 'tile' | 'open' | 'mono';

const WAVE = 'M5 16.5 Q8 10.6 11 16.5 Q14 22.4 17 16.5';
const CARET = 'M19.4 12 L23.8 16.5 L19.4 21';

export function Logo({
  variant = 'tile',
  className,
  title,
}: {
  variant?: LogoVariant;
  className?: string;
  /** Accessible name. Omit when the mark sits next to a text wordmark (then it
   *  is decorative — `aria-hidden` — so screen readers don't read the name twice). */
  title?: string;
}) {
  const waveColor = variant === 'tile' ? '#ffffff' : variant === 'open' ? '#6366f1' : 'currentColor';
  const caretColor = variant === 'mono' ? 'currentColor' : variant === 'tile' ? '#2dd4bf' : '#14b8a6';
  const stroke = { strokeWidth: 2.7, strokeLinecap: 'round', strokeLinejoin: 'round', fill: 'none' } as const;
  const a11y = title ? { role: 'img', 'aria-label': title } : { 'aria-hidden': true };

  return (
    <svg viewBox="0 0 32 32" className={className} {...a11y}>
      {variant === 'tile' && <rect x="2" y="2" width="28" height="28" rx="8.5" fill="#1c1b29" />}
      <path d={WAVE} stroke={waveColor} {...stroke} />
      <path d={CARET} stroke={caretColor} {...stroke} />
    </svg>
  );
}
