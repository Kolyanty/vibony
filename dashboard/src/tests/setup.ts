import '@testing-library/jest-dom/vitest';
import { afterEach, vi } from 'vitest';
import i18next from '../i18n';

// jsdom doesn't implement scrollIntoView (the Timeline auto-scrolls the active row
// into view as the cursor moves). Stub it so those effects are a no-op under tests.
if (!HTMLElement.prototype.scrollIntoView) {
  HTMLElement.prototype.scrollIntoView = vi.fn();
}

// jsdom lacks ResizeObserver + the Pointer Capture API. Radix's Popover (used by
// the task-picker combobox) positions its content via floating-ui, which needs
// ResizeObserver, and its pointer handling calls has/set/releasePointerCapture.
// Stub them so the popover content actually mounts under tests.
if (!('ResizeObserver' in globalThis)) {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}
if (!HTMLElement.prototype.hasPointerCapture) {
  HTMLElement.prototype.hasPointerCapture = vi.fn(() => false);
  HTMLElement.prototype.setPointerCapture = vi.fn();
  HTMLElement.prototype.releasePointerCapture = vi.fn();
}

// All tests run under the en catalog by default. If a test switches the language
// (e.g. ru-specific plural forms), this hook resets back to en for the next test.
afterEach(() => {
  if (i18next.language !== 'en') {
    void i18next.changeLanguage('en');
  }
});
