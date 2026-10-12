/*
 * Tailwind class string for native <select> elements styled to match the
 * shadcn <Input> look. Used where a Radix Select would complicate the
 * jsdom fireEvent-based tests (short, static dropdowns). Kept in one place so
 * it doesn't drift from the Input primitive across the screens that use it.
 */
export const NATIVE_SELECT_CLASS =
  'h-9 w-full min-w-0 rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-xs outline-none transition-[color,box-shadow] disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50 focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50';
