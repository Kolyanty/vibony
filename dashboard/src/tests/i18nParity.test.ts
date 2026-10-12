import { describe, expect, it } from 'vitest';
import en from '../i18n/en.json';
import ru from '../i18n/ru.json';

// i18n parity guard.
//
// The raw key COUNT of en.json vs ru.json legitimately differs and is NOT a bug:
// i18next v4 (Intl.PluralRules) picks plural suffixes per the language's CLDR
// categories — English uses `one`/`other`, Russian uses `one`/`few`/`many`(/`other`).
// So a pluralised string is 2 keys in en but 3-4 in ru.
//
// What MUST stay in sync is the set of *semantic* keys — the base with the plural
// suffix stripped. This guard catches a genuinely untranslated string (a base
// present in one catalog but missing in the other) while ignoring the legitimate
// plural-suffix difference.

const PLURAL_SUFFIXES = ['_zero', '_one', '_two', '_few', '_many', '_other'];

function baseKey(key: string): string {
  for (const s of PLURAL_SUFFIXES) {
    if (key.endsWith(s)) return key.slice(0, -s.length);
  }
  return key;
}

const enKeys = Object.keys(en);
const ruKeys = Object.keys(ru);
const enSet = new Set(enKeys);
const ruSet = new Set(ruKeys);

function semanticBases(keys: string[]): string[] {
  return [...new Set(keys.map(baseKey))].sort();
}

/** Bases that are pluralised in this catalog (at least one key carries a suffix). */
function pluralBases(keys: string[]): Set<string> {
  return new Set(keys.filter((k) => baseKey(k) !== k).map(baseKey));
}

describe('i18n catalogs (en/ru)', () => {
  it('share an identical set of semantic keys (plural suffixes ignored)', () => {
    // toEqual on the sorted arrays gives a readable diff of any missing key.
    expect(semanticBases(ruKeys)).toEqual(semanticBases(enKeys));
  });

  it('have no empty values', () => {
    for (const [k, v] of Object.entries(en)) expect(v, `en["${k}"]`).not.toBe('');
    for (const [k, v] of Object.entries(ru)) expect(v, `ru["${k}"]`).not.toBe('');
  });

  it('carry the English CLDR plural forms (one + other) for every plural key', () => {
    for (const base of pluralBases(enKeys)) {
      expect(enSet.has(`${base}_one`), `${base}_one`).toBe(true);
      expect(enSet.has(`${base}_other`), `${base}_other`).toBe(true);
    }
  });

  it('carry the Russian CLDR plural forms (one + few + many) for every plural key', () => {
    for (const base of pluralBases(ruKeys)) {
      expect(ruSet.has(`${base}_one`), `${base}_one`).toBe(true);
      expect(ruSet.has(`${base}_few`), `${base}_few`).toBe(true);
      expect(ruSet.has(`${base}_many`), `${base}_many`).toBe(true);
    }
  });
});
