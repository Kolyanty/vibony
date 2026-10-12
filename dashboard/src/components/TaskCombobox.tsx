import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Check, ChevronsUpDown, Search, Star } from 'lucide-react';
import type { TaskListItem } from '../api/rest';
import { cn } from '@/lib/utils';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';

interface TaskComboboxProps {
  id?: string;
  tasks: TaskListItem[];
  /** Selected task slug (matches the create-session `task_id`). */
  value: string;
  onChange: (slug: string) => void;
  disabled?: boolean;
}

// Tokenized match over the two fields a picker recognizes — the human title and the
// stable slug (which carries the task "number", e.g. task-01-training-loop). The query
// is split on whitespace and every token must appear somewhere in "title + slug", so
// "ab test" finds "A/B Test" / "…-ab-test" even though the whole phrase is a substring
// of neither. Description search is a server-side concern (catalog `?q=`), not here.
function matches(task: TaskListItem, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (q === '') return true;
  const haystack = `${task.title} ${task.slug}`.toLowerCase();
  return q.split(/\s+/).every((token) => haystack.includes(token));
}

/**
 * Searchable task picker for the create-session dialog. Replaces the native
 * <select>: type-to-filter over title+slug, tasks grouped by origin (workspace
 * vs Vibony templates), the slug shown as a mono trailing tag so tasks are
 * distinguishable and the corpus number is visible. No "Template" tag — origin
 * is conveyed by the group header, not repeated per row.
 */
export function TaskCombobox({ id, tasks, value, onChange, disabled }: TaskComboboxProps) {
  const { t } = useTranslation();
  const baseId = useId();
  const listboxId = `${baseId}-listbox`;
  const optionId = (tk: TaskListItem) => `${baseId}-opt-${tk.id}`;
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  const selected = useMemo(() => tasks.find((tk) => tk.slug === value) ?? null, [tasks, value]);

  const filtered = useMemo(() => tasks.filter((tk) => matches(tk, query)), [tasks, query]);
  // Favorites float to the top; the rest group by origin (workspace before Vibony,
  // mirroring the list endpoint's is_template ASC). A favorite shows only in the
  // Favorites group — each task lands in exactly one group.
  const groups = useMemo(() => {
    const favorites = filtered.filter((tk) => tk.is_favorite);
    const workspace = filtered.filter((tk) => !tk.is_favorite && !tk.is_template);
    const templates = filtered.filter((tk) => !tk.is_favorite && tk.is_template);
    return [
      { key: 'favorites', label: t('tasks.group.favorites'), items: favorites },
      { key: 'workspace', label: t('tasks.group.workspace'), items: workspace },
      { key: 'templates', label: t('tasks.group.templates'), items: templates },
    ].filter((g) => g.items.length > 0);
  }, [filtered, t]);

  // Flat order matching visual (group) order — the index space for keyboard nav.
  const flat = useMemo(() => groups.flatMap((g) => g.items), [groups]);

  // Keep the active highlight in range whenever the result set changes.
  useEffect(() => {
    setActiveIndex((i) => (flat.length === 0 ? 0 : Math.min(i, flat.length - 1)));
  }, [flat.length]);

  const activeTask = flat[activeIndex] ?? null;

  const commit = (slug: string) => {
    onChange(slug);
    setOpen(false);
    setQuery('');
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActiveIndex((i) => (flat.length === 0 ? 0 : (i + 1) % flat.length));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActiveIndex((i) => (flat.length === 0 ? 0 : (i - 1 + flat.length) % flat.length));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const target = flat[activeIndex];
      if (target) commit(target.slug);
    }
  };

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) setQuery('');
      }}
    >
      <PopoverTrigger
        id={id}
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        disabled={disabled}
        data-testid="task-combobox-trigger"
        className={cn(
          'flex h-9 w-full min-w-0 items-center justify-between gap-2 rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-xs outline-none transition-[color,box-shadow] disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50 focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50',
        )}
      >
        {selected ? (
          <span className="flex min-w-0 items-center gap-2">
            <span className="truncate">{selected.title}</span>
            <span className="shrink-0 font-mono text-xs text-muted-foreground">{selected.slug}</span>
          </span>
        ) : (
          <span className="text-muted-foreground">{t('sessions.create.taskPlaceholder')}</span>
        )}
        <ChevronsUpDown className="size-4 shrink-0 opacity-50" aria-hidden="true" />
      </PopoverTrigger>
      <PopoverContent
        align="start"
        className="w-(--radix-popover-trigger-width) p-0"
        onOpenAutoFocus={(e) => {
          e.preventDefault();
          inputRef.current?.focus();
        }}
      >
        <div className="flex items-center gap-2 border-b border-border px-3">
          <Search className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
          <input
            ref={inputRef}
            type="text"
            role="combobox"
            aria-expanded
            aria-controls={listboxId}
            aria-activedescendant={activeTask ? optionId(activeTask) : undefined}
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setActiveIndex(0);
            }}
            onKeyDown={handleKeyDown}
            placeholder={t('sessions.create.taskSearchPlaceholder')}
            aria-label={t('sessions.create.taskSearchPlaceholder')}
            data-testid="task-combobox-input"
            className="h-9 w-full bg-transparent py-1 text-sm outline-none placeholder:text-muted-foreground"
          />
        </div>
        <div id={listboxId} role="listbox" className="max-h-64 overflow-y-auto p-1" data-testid="task-combobox-list">
          {flat.length === 0 ? (
            <p className="px-2 py-4 text-center text-sm text-muted-foreground" data-testid="task-combobox-empty">
              {tasks.length === 0 ? t('sessions.create.noTasks') : t('sessions.create.noTaskMatch')}
            </p>
          ) : (
            groups.map((group) => (
              <div key={group.key} role="group" aria-label={group.label}>
                <p className="px-2 py-1.5 text-xs font-medium text-muted-foreground">{group.label}</p>
                {group.items.map((tk) => {
                  const isActive = activeTask === tk;
                  const isSelected = tk.slug === value;
                  return (
                    <button
                      key={tk.id}
                      id={optionId(tk)}
                      type="button"
                      role="option"
                      aria-selected={isSelected}
                      data-testid={`task-option-${tk.slug}`}
                      onClick={() => commit(tk.slug)}
                      onMouseMove={() => {
                        const idx = flat.indexOf(tk);
                        if (idx >= 0) setActiveIndex(idx);
                      }}
                      className={cn(
                        'flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-sm outline-none',
                        isActive && 'bg-accent text-accent-foreground',
                      )}
                    >
                      <Check className={cn('size-4 shrink-0', isSelected ? 'opacity-100' : 'opacity-0')} aria-hidden="true" />
                      <span className="min-w-0 flex-1 truncate">{tk.title}</span>
                      <span className="shrink-0 font-mono text-xs text-muted-foreground">{tk.slug}</span>
                      {tk.is_favorite && (
                        <Star className="size-3.5 shrink-0 fill-warn text-warn" aria-hidden="true" />
                      )}
                    </button>
                  );
                })}
              </div>
            ))
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}
