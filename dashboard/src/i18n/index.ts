import i18next from 'i18next';
import { initReactI18next } from 'react-i18next';
import en from './en.json';
import ru from './ru.json';

/**
 * Registered UI languages. To add a new language:
 *   1. Create `src/i18n/<code>.json` with the same keys as en.json.
 *   2. Import it and add it to `resources` below.
 *   3. Add a `{ code, nativeName }` entry to `LANGUAGES` — `LanguageSwitcher` picks it up.
 * No component code needs to change.
 */
export const LANGUAGES = [
  { code: 'en', nativeName: 'English' },
  { code: 'ru', nativeName: 'Русский' },
] as const;

export const SUPPORTED_LANGS = LANGUAGES.map((l) => l.code) as readonly Lang[];
export type Lang = (typeof LANGUAGES)[number]['code'];
export const DEFAULT_LANG: Lang = 'en';
export const STORAGE_KEY = 'vibony.lang';

function isSupported(lang: string): lang is Lang {
  return (SUPPORTED_LANGS as readonly string[]).includes(lang);
}

function readStoredLang(): Lang {
  if (typeof localStorage === 'undefined') return DEFAULT_LANG;
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw && isSupported(raw)) return raw;
  } catch {
    // ignore: privacy mode or stub localStorage
  }
  return DEFAULT_LANG;
}

if (!i18next.isInitialized) {
  void i18next.use(initReactI18next).init({
    resources: { en: { translation: en }, ru: { translation: ru } },
    lng: readStoredLang(),
    fallbackLng: DEFAULT_LANG,
    supportedLngs: SUPPORTED_LANGS as unknown as string[],
    // react-i18next renders the result as a React node and React escapes the
    // content itself — XSS protection stays; extra escaping by i18next only
    // breaks non-ASCII characters/symbols in placeholders.
    interpolation: { escapeValue: false },
    returnNull: false,
  });
}

export function setLanguage(lang: Lang): void {
  if (!isSupported(lang)) return;
  void i18next.changeLanguage(lang);
  if (typeof localStorage !== 'undefined') {
    try {
      localStorage.setItem(STORAGE_KEY, lang);
    } catch {
      // ignore
    }
  }
}

export default i18next;
