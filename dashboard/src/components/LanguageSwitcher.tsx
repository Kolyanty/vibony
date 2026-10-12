import { useCallback, useEffect, useRef, useState } from 'react';
import { Check, Globe } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { LANGUAGES, setLanguage, type Lang } from '../i18n';
import styles from './LanguageSwitcher.module.css';

/**
 * Language-picker popover menu. Source of truth for available languages —
 * `LANGUAGES` in `src/i18n/index.ts`. To add a new language, add an entry
 * there and the component picks it up.
 */
export function LanguageSwitcher() {
  const { i18n, t } = useTranslation();
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const firstItemRef = useRef<HTMLButtonElement>(null);

  const current = (LANGUAGES.find((l) => l.code === i18n.language)
    ?? LANGUAGES[0]) as (typeof LANGUAGES)[number];

  // Close on outside click and Escape
  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      if (!rootRef.current) return;
      if (e.target instanceof Node && rootRef.current.contains(e.target)) return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onDocClick);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDocClick);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  // On open, move focus to the first item — keyboard navigation.
  useEffect(() => {
    if (open) firstItemRef.current?.focus();
  }, [open]);

  const handleSelect = useCallback((code: Lang) => {
    setLanguage(code);
    setOpen(false);
  }, []);

  return (
    <div className={styles.root} ref={rootRef} data-testid="language-switcher">
      <button
        type="button"
        className={styles.trigger}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={t('language.switcher.current', { name: current.nativeName })}
        title={t('language.switcher.open')}
        onClick={() => setOpen((v) => !v)}
        data-testid="language-switcher-trigger"
        data-current={current.code}
      >
        <Globe size={16} strokeWidth={2} aria-hidden="true" />
        <span className={styles.code} aria-hidden="true">
          {current.code.toUpperCase()}
        </span>
      </button>
      {open && (
        <div
          className={styles.menu}
          role="menu"
          aria-label={t('language.switcher.label')}
          data-testid="language-switcher-menu"
        >
          {LANGUAGES.map((lang, idx) => {
            const active = lang.code === current.code;
            return (
              <button
                key={lang.code}
                type="button"
                role="menuitemradio"
                aria-checked={active}
                className={styles.item}
                data-active={active}
                data-testid={`language-${lang.code}`}
                ref={idx === 0 ? firstItemRef : undefined}
                onClick={() => handleSelect(lang.code)}
              >
                <span className={styles.itemName}>{lang.nativeName}</span>
                {active ? (
                  <Check
                    className={styles.itemCheck}
                    size={14}
                    strokeWidth={2.5}
                    aria-hidden="true"
                  />
                ) : (
                  <span className={styles.itemCheckSpacer} aria-hidden="true" />
                )}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
