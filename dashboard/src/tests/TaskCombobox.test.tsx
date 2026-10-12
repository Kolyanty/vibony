import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { TaskCombobox } from '../components/TaskCombobox';
import type { TaskListItem } from '../api/rest';

function task(over: Partial<TaskListItem>): TaskListItem {
  return {
    id: over.slug ?? 'id',
    company_id: 'c1',
    slug: 'task-01-training-loop',
    title: 'Training loop',
    runtime_image: 'vibony/sandbox-py:3.12',
    visibility: 'public',
    is_template: false,
    can_edit: false,
    is_favorite: false,
    created_at: '2026-05-01T00:00:00Z',
    updated_at: '2026-05-01T00:00:00Z',
    ...over,
  };
}

const TASKS: TaskListItem[] = [
  task({ id: 't-own', slug: 'own-task', title: 'Our IDOR task', is_template: false }),
  task({ id: 't-tmpl2', slug: 'task-01-training-loop', title: 'Training loop', is_template: true }),
  task({ id: 't-tmpl2-seat', slug: 'task-02-seat-reservation', title: 'Seat Reservation', is_template: true }),
];

function open() {
  fireEvent.click(screen.getByTestId('task-combobox-trigger'));
  return waitFor(() => expect(screen.getByTestId('task-combobox-list')).toBeInTheDocument());
}

describe('TaskCombobox', () => {
  it('shows the selected task title + slug on the trigger', () => {
    render(<TaskCombobox tasks={TASKS} value="task-02-seat-reservation" onChange={() => {}} />);
    const trigger = screen.getByTestId('task-combobox-trigger');
    expect(trigger).toHaveTextContent('Seat Reservation');
    expect(trigger).toHaveTextContent('task-02-seat-reservation');
  });

  it('groups by origin and shows the slug on each option — no per-row Template tag', async () => {
    render(<TaskCombobox tasks={TASKS} value="" onChange={() => {}} />);
    await open();
    // Group headers present.
    expect(screen.getByText('Workspace tasks')).toBeInTheDocument();
    expect(screen.getByText('Vibony tasks')).toBeInTheDocument();
    // The slug rides along with each option.
    expect(screen.getByTestId('task-option-task-01-training-loop')).toHaveTextContent('task-01-training-loop');
    // The old "· Template" inline tag is gone; origin is the group header now.
    expect(screen.queryByText(/·\s*Template/)).not.toBeInTheDocument();
  });

  it('filters by title and by slug', async () => {
    render(<TaskCombobox tasks={TASKS} value="" onChange={() => {}} />);
    await open();
    const input = screen.getByTestId('task-combobox-input');

    // By slug fragment (the "number").
    fireEvent.change(input, { target: { value: '02' } });
    expect(screen.getByTestId('task-option-task-02-seat-reservation')).toBeInTheDocument();
    expect(screen.queryByTestId('task-option-task-01-training-loop')).not.toBeInTheDocument();
    expect(screen.queryByTestId('task-option-own-task')).not.toBeInTheDocument();

    // By title fragment.
    fireEvent.change(input, { target: { value: 'idor' } });
    expect(screen.getByTestId('task-option-own-task')).toBeInTheDocument();
    expect(screen.queryByTestId('task-option-task-02-seat-reservation')).not.toBeInTheDocument();

    // No match → empty state.
    fireEvent.change(input, { target: { value: 'zzz-nothing' } });
    expect(screen.getByTestId('task-combobox-empty')).toBeInTheDocument();
  });

  it('matches a multi-word query token-by-token across title and slug', async () => {
    render(<TaskCombobox tasks={TASKS} value="" onChange={() => {}} />);
    await open();
    const input = screen.getByTestId('task-combobox-input');
    // 'training' is only in the title, '01' only in the slug — the whole phrase is a
    // substring of neither, but every token appears in "title + slug".
    fireEvent.change(input, { target: { value: 'training 01' } });
    expect(screen.getByTestId('task-option-task-01-training-loop')).toBeInTheDocument();
    expect(screen.queryByTestId('task-option-own-task')).not.toBeInTheDocument();
    // A token that matches nothing excludes the row (AND semantics).
    fireEvent.change(input, { target: { value: 'training nomatch' } });
    expect(screen.getByTestId('task-combobox-empty')).toBeInTheDocument();
  });

  it('commits a selection on click', async () => {
    const onChange = vi.fn();
    render(<TaskCombobox tasks={TASKS} value="" onChange={onChange} />);
    await open();
    fireEvent.click(screen.getByTestId('task-option-own-task'));
    expect(onChange).toHaveBeenCalledWith('own-task');
  });

  it('floats a favorite into a top "Favorites" group, out of its origin group', async () => {
    const tasks = [
      task({ id: 't-a', slug: 'own-task', title: 'Our IDOR task', is_template: false }),
      task({ id: 't-b', slug: 'task-02-seat-reservation', title: 'Seat Reservation', is_template: true, is_favorite: true }),
    ];
    render(<TaskCombobox tasks={tasks} value="" onChange={() => {}} />);
    await open();
    expect(screen.getByText('Favorites')).toBeInTheDocument();
    // The favorited template shows under Favorites, not under "Vibony tasks".
    const fav = screen.getByTestId('task-option-task-02-seat-reservation');
    expect(fav.closest('[role="group"]')).toHaveAttribute('aria-label', 'Favorites');
    // Its origin group ("Vibony tasks") no longer lists it → that group is absent.
    expect(screen.queryByText('Vibony tasks')).not.toBeInTheDocument();
    // Workspace group still shows the non-favorite.
    expect(screen.getByTestId('task-option-own-task')).toBeInTheDocument();
  });

  it('selects the highlighted option with Enter after arrow navigation', async () => {
    const onChange = vi.fn();
    render(<TaskCombobox tasks={TASKS} value="" onChange={onChange} />);
    await open();
    const input = screen.getByTestId('task-combobox-input');
    // Flat order is workspace-first: [own-task, task-01, task-02]. Down once → task-01.
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onChange).toHaveBeenCalledWith('task-01-training-loop');
  });
});
