import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LanguageSwitcher } from '../components/LanguageSwitcher';
import i18next, { STORAGE_KEY } from '../i18n';

describe('LanguageSwitcher', () => {
  beforeEach(() => {
    try {
      localStorage.removeItem(STORAGE_KEY);
    } catch {
      // ignore
    }
  });

  afterEach(async () => {
    await act(async () => {
      await i18next.changeLanguage('en');
    });
    vi.restoreAllMocks();
  });

  it('renders globe trigger with current language code', () => {
    render(<LanguageSwitcher />);
    const trigger = screen.getByTestId('language-switcher-trigger');
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    expect(trigger).toHaveAttribute('data-current', 'en');
    expect(trigger).toHaveTextContent('EN');
    // menu is closed initially
    expect(screen.queryByTestId('language-switcher-menu')).toBeNull();
  });

  it('click trigger → menu opens with all languages', () => {
    render(<LanguageSwitcher />);
    fireEvent.click(screen.getByTestId('language-switcher-trigger'));
    expect(screen.getByTestId('language-switcher-menu')).toBeInTheDocument();
    expect(screen.getByTestId('language-en')).toHaveTextContent('English');
    expect(screen.getByTestId('language-ru')).toHaveTextContent('Русский');
  });

  it('active language has aria-checked="true" and data-active', () => {
    render(<LanguageSwitcher />);
    fireEvent.click(screen.getByTestId('language-switcher-trigger'));
    const en = screen.getByTestId('language-en');
    const ru = screen.getByTestId('language-ru');
    expect(en).toHaveAttribute('aria-checked', 'true');
    expect(en).toHaveAttribute('data-active', 'true');
    expect(ru).toHaveAttribute('aria-checked', 'false');
    expect(ru).toHaveAttribute('data-active', 'false');
  });

  it('click RU → switches i18next.language and closes menu', async () => {
    render(<LanguageSwitcher />);
    fireEvent.click(screen.getByTestId('language-switcher-trigger'));
    await act(async () => {
      fireEvent.click(screen.getByTestId('language-ru'));
      await Promise.resolve();
    });
    expect(i18next.language).toBe('ru');
    expect(screen.queryByTestId('language-switcher-menu')).toBeNull();
    // trigger now shows RU
    expect(screen.getByTestId('language-switcher-trigger')).toHaveAttribute('data-current', 'ru');
    expect(screen.getByTestId('language-switcher-trigger')).toHaveTextContent('RU');
  });

  it('click on language → writes to localStorage', async () => {
    const setSpy = vi.spyOn(Storage.prototype, 'setItem');
    render(<LanguageSwitcher />);
    fireEvent.click(screen.getByTestId('language-switcher-trigger'));
    await act(async () => {
      fireEvent.click(screen.getByTestId('language-ru'));
      await Promise.resolve();
    });
    expect(setSpy).toHaveBeenCalledWith(STORAGE_KEY, 'ru');
  });

  it('localStorage error does not crash the switcher', async () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('quota');
    });
    render(<LanguageSwitcher />);
    fireEvent.click(screen.getByTestId('language-switcher-trigger'));
    await act(async () => {
      fireEvent.click(screen.getByTestId('language-en'));
      await Promise.resolve();
    });
    expect(i18next.language).toBe('en');
  });

  it('Escape closes the menu', () => {
    render(<LanguageSwitcher />);
    fireEvent.click(screen.getByTestId('language-switcher-trigger'));
    expect(screen.getByTestId('language-switcher-menu')).toBeInTheDocument();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByTestId('language-switcher-menu')).toBeNull();
  });

  it('click outside closes the menu', () => {
    render(
      <>
        <LanguageSwitcher />
        <button data-testid="outside">outside</button>
      </>,
    );
    fireEvent.click(screen.getByTestId('language-switcher-trigger'));
    expect(screen.getByTestId('language-switcher-menu')).toBeInTheDocument();
    fireEvent.mouseDown(screen.getByTestId('outside'));
    expect(screen.queryByTestId('language-switcher-menu')).toBeNull();
  });
});
