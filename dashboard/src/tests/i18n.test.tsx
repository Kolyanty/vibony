import { act } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import i18next from '../i18n';

describe('i18n — language switch', () => {
  it('default language is en', () => {
    // setup.ts afterEach resets to 'en' between tests
    expect(i18next.language).toBe('en');
    expect(i18next.t('connection.idle')).toBe('not connected');
    expect(i18next.t('tabs.dialog')).toBe('Dialog');
  });

  it('changeLanguage("ru") flips translations', async () => {
    await act(async () => {
      await i18next.changeLanguage('ru');
    });
    expect(i18next.t('connection.idle')).toBe('не подключено');
    expect(i18next.t('tabs.dialog')).toBe('Диалог');
  });

  it('reset to en works', async () => {
    await act(async () => {
      await i18next.changeLanguage('en');
    });
    expect(i18next.t('connection.idle')).toBe('not connected');
  });
});

describe('i18n — plurals', () => {
  it('en plural forms (one/other)', () => {
    const en = i18next.getFixedT('en');
    expect(en('files.changeCount', { count: 1 })).toBe('1 change');
    expect(en('files.changeCount', { count: 2 })).toBe('2 changes');
    expect(en('files.changeCount', { count: 5 })).toBe('5 changes');
  });

  it('ru plural forms (one/few/many)', () => {
    const ru = i18next.getFixedT('ru');
    expect(ru('files.changeCount', { count: 1 })).toBe('1 изменение');
    expect(ru('files.changeCount', { count: 2 })).toBe('2 изменения');
    expect(ru('files.changeCount', { count: 3 })).toBe('3 изменения');
    expect(ru('files.changeCount', { count: 5 })).toBe('5 изменений');
    expect(ru('files.changeCount', { count: 11 })).toBe('11 изменений');
    expect(ru('files.changeCount', { count: 21 })).toBe('21 изменение');
  });

  it('en dialog.jumpNew with count', () => {
    const en = i18next.getFixedT('en');
    expect(en('dialog.jumpNew', { count: 1 })).toBe('↓ 1 new event');
    expect(en('dialog.jumpNew', { count: 7 })).toBe('↓ 7 new events');
  });

  it('ru dialog.jumpNew with count', () => {
    const ru = i18next.getFixedT('ru');
    expect(ru('dialog.jumpNew', { count: 1 })).toBe('↓ новое событие: 1');
    expect(ru('dialog.jumpNew', { count: 3 })).toBe('↓ новых события: 3');
    expect(ru('dialog.jumpNew', { count: 12 })).toBe('↓ новых событий: 12');
  });

  it('D-11: ru workspace.plan.trialDays resolves every plural form (no untranslated key)', () => {
    const ru = i18next.getFixedT('ru');
    expect(ru('workspace.plan.trialDays', { count: 1 })).toBe('Триал · 1 день');
    expect(ru('workspace.plan.trialDays', { count: 3 })).toBe('Триал · 3 дня');
    expect(ru('workspace.plan.trialDays', { count: 5 })).toBe('Триал · 5 дней');
    expect(ru('workspace.plan.trialDays', { count: 14 })).toBe('Триал · 14 дней');
    expect(ru('workspace.plan.trialDays', { count: 21 })).toBe('Триал · 21 день');
    // none of the forms leak the raw key
    for (const count of [1, 3, 5, 14, 21]) {
      expect(ru('workspace.plan.trialDays', { count })).not.toContain('trialDays');
    }
  });
});

describe('i18n — interpolation', () => {
  it('error.endStatus inserts {{status}}', () => {
    const en = i18next.getFixedT('en');
    expect(en('error.endStatus', { status: 503 })).toBe('Backend returned status 503');
  });

  it('session.end.error inserts {{message}}', () => {
    const en = i18next.getFixedT('en');
    expect(en('session.end.error', { message: 'bad token' })).toBe(
      'Could not end session: bad token',
    );
  });
});
