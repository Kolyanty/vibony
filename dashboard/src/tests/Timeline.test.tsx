import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { EventEnvelope } from '../api/types';
import { Timeline } from '../components/Timeline';
import { buildTimeline } from '../components/timelineUtils';

function ev(seq: number, type: string, payload: Record<string, unknown> = {}): EventEnvelope {
  return {
    id: `e${seq}`,
    seq,
    session_code: 'X',
    timestamp: '2026-01-01T00:00:00Z',
    type,
    payload,
  } as EventEnvelope;
}

const PHASES = buildTimeline([
  ev(1, 'session_start', { task_id: 'task-01' }),
  ev(2, 'user_message', { text: 'fix the loop' }),
  ev(3, 'file_changed', { file_path: 'train.py', content: 'x', op: 'modified' }),
  ev(4, 'command_run', { command_id: 'c1', command: 'python train.py', cwd: '.' }),
  ev(5, 'command_finished', { command_id: 'c1', exit_code: 0, duration_ms: 1 }),
]);

const btnFor = (text: string) => screen.getByText(text).closest('button')!;

describe('Timeline', () => {
  it('renders the empty state when there are no phases', () => {
    render(<Timeline phases={[]} currentSeq={null} highlightSeq={null} onSelect={() => {}} />);
    expect(screen.getByTestId('timeline-empty')).toBeInTheDocument();
  });

  it('renders phases (setup + user message) and entries', () => {
    render(<Timeline phases={PHASES} currentSeq={null} highlightSeq={null} onSelect={() => {}} />);
    expect(screen.getByText('Setup')).toBeInTheDocument();
    expect(screen.getByText('fix the loop')).toBeInTheDocument();
    expect(screen.getByText('train.py')).toBeInTheDocument();
    // command entry shows its exit-code badge
    expect(btnFor('python train.py')).toHaveTextContent('exit 0');
  });

  it('the setup phase header is disabled (no headerSeq to jump to)', () => {
    render(<Timeline phases={PHASES} currentSeq={null} highlightSeq={null} onSelect={() => {}} />);
    expect(screen.getByRole('button', { name: 'Setup' })).toBeDisabled();
  });

  it('clicking an entry calls onSelect with its seq', () => {
    const onSelect = vi.fn();
    render(<Timeline phases={PHASES} currentSeq={null} highlightSeq={null} onSelect={onSelect} />);
    fireEvent.click(screen.getByText('train.py'));
    expect(onSelect).toHaveBeenCalledWith(3);
  });

  it('clicking a user-message phase header jumps to its seq', () => {
    const onSelect = vi.fn();
    render(<Timeline phases={PHASES} currentSeq={null} highlightSeq={null} onSelect={onSelect} />);
    fireEvent.click(screen.getByRole('button', { name: 'fix the loop' }));
    expect(onSelect).toHaveBeenCalledWith(2);
  });

  it('marks the active row from currentSeq (greatest point ≤ cursor)', () => {
    render(<Timeline phases={PHASES} currentSeq={5} highlightSeq={null} onSelect={() => {}} />);
    // cursor's event seq 5 (command_finished) resolves to the command_run row at seq 4.
    expect(btnFor('python train.py')).toHaveAttribute('data-active', 'true');
    expect(btnFor('train.py')).toHaveAttribute('data-active', 'false');
  });

  it('flashes the deep-link target row from highlightSeq', () => {
    render(<Timeline phases={PHASES} currentSeq={null} highlightSeq={3} onSelect={() => {}} />);
    expect(btnFor('train.py')).toHaveAttribute('data-flash', 'true');
  });

  it('scrolls to the flash target (not the cursor) when highlightSeq is set', () => {
    const targets: (string | null)[] = [];
    const spy = vi
      .spyOn(HTMLElement.prototype, 'scrollIntoView')
      .mockImplementation(function (this: HTMLElement) {
        targets.push(this.getAttribute('data-seq'));
      });
    // cursor at seq 2 (active), deep-link flash at seq 3 → must scroll to 3.
    render(<Timeline phases={PHASES} currentSeq={2} highlightSeq={3} onSelect={() => {}} />);
    expect(targets.at(-1)).toBe('3');
    spy.mockRestore();
  });

  it('scrolls to the active row when there is no flash', () => {
    const targets: (string | null)[] = [];
    const spy = vi
      .spyOn(HTMLElement.prototype, 'scrollIntoView')
      .mockImplementation(function (this: HTMLElement) {
        targets.push(this.getAttribute('data-seq'));
      });
    render(<Timeline phases={PHASES} currentSeq={4} highlightSeq={null} onSelect={() => {}} />);
    expect(targets.at(-1)).toBe('4');
    spy.mockRestore();
  });
});
