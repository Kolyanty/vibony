import { describe, expect, it } from 'vitest';
import type { EventEnvelope } from '../api/types';
import { buildTimeline, resolveActiveSeq } from '../components/timelineUtils';

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

describe('buildTimeline', () => {
  it('returns [] for an empty log', () => {
    expect(buildTimeline([])).toEqual([]);
  });

  it('groups events into phases opened by user_message; pre-message events form a setup phase', () => {
    const phases = buildTimeline([
      ev(1, 'session_start', { task_id: 'task-01' }),
      ev(2, 'user_message', { text: 'do the thing' }),
      ev(3, 'assistant_message', { text: 'on it' }),
    ]);
    expect(phases).toHaveLength(2);
    // setup phase
    expect(phases[0]!.headerSeq).toBeNull();
    expect(phases[0]!.label).toBeNull();
    expect(phases[0]!.entries.map((e) => e.kind)).toEqual(['session_start']);
    // user-message phase
    expect(phases[1]!.headerSeq).toBe(2);
    expect(phases[1]!.label).toBe('do the thing');
    expect(phases[1]!.entries.map((e) => e.kind)).toEqual(['assistant_message']);
  });

  it('keeps a user-message phase with no entries (header alone)', () => {
    const phases = buildTimeline([ev(1, 'user_message', { text: 'hi' })]);
    expect(phases).toHaveLength(1);
    expect(phases[0]!.headerSeq).toBe(1);
    expect(phases[0]!.entries).toEqual([]);
  });

  it('drops an empty setup phase (no leading events)', () => {
    const phases = buildTimeline([ev(1, 'user_message', { text: 'hi' }), ev(2, 'assistant_message', { text: 'yo' })]);
    expect(phases).toHaveLength(1);
    expect(phases[0]!.headerSeq).toBe(1);
  });

  it('folds command_run + command_finished into one entry carrying the exit code', () => {
    const phases = buildTimeline([
      ev(1, 'user_message', { text: 'run it' }),
      ev(2, 'command_run', { command_id: 'c1', command: 'python train.py', cwd: '.' }),
      ev(3, 'command_output', { command_id: 'c1', stream: 'stdout', data: 'noise' }),
      ev(4, 'command_finished', { command_id: 'c1', exit_code: 1, duration_ms: 10 }),
    ]);
    const entries = phases[0]!.entries;
    expect(entries).toHaveLength(1);
    expect(entries[0]).toMatchObject({ seq: 2, kind: 'command', text: 'python train.py', exitCode: 1 });
  });

  it('folds consecutive Read/Glob/Grep tool_calls into a counted tool_group', () => {
    const phases = buildTimeline([
      ev(1, 'user_message', { text: 'look' }),
      ev(2, 'tool_call', { tool_call_id: 't1', tool_name: 'Read', input: {} }),
      ev(3, 'tool_call', { tool_call_id: 't2', tool_name: 'Grep', input: {} }),
      ev(4, 'tool_call', { tool_call_id: 't3', tool_name: 'Edit', input: {} }),
    ]);
    const entries = phases[0]!.entries;
    expect(entries.map((e) => e.kind)).toEqual(['tool_group', 'tool']);
    expect(entries[0]).toMatchObject({ seq: 2, kind: 'tool_group', count: 2 });
    expect(entries[1]).toMatchObject({ seq: 4, kind: 'tool', text: 'Edit' });
  });

  it('marks file_changed as saved vs deleted and skips baseline + directory events', () => {
    const phases = buildTimeline([
      ev(1, 'user_message', { text: 'edit' }),
      ev(2, 'file_changed', { file_path: 'base.py', content: 'b', source: 'baseline' }),
      ev(3, 'file_changed', { file_path: 'dir', is_dir: true, content: null }),
      ev(4, 'file_changed', { file_path: 'train.py', content: 'x', op: 'modified' }),
      ev(5, 'file_changed', { file_path: 'old.py', content: null, op: 'deleted' }),
    ]);
    const entries = phases[0]!.entries;
    expect(entries.map((e) => [e.kind, e.text])).toEqual([
      ['file_saved', 'train.py'],
      ['file_deleted', 'old.py'],
    ]);
  });

  it('skips tool_result and command_output (folded/no navigation value)', () => {
    const phases = buildTimeline([
      ev(1, 'user_message', { text: 'go' }),
      ev(2, 'tool_call', { tool_call_id: 't1', tool_name: 'Edit', input: {} }),
      ev(3, 'tool_result', { tool_call_id: 't1', output: 'ok', is_error: false }),
    ]);
    expect(phases[0]!.entries.map((e) => e.kind)).toEqual(['tool']);
  });
});

describe('resolveActiveSeq', () => {
  const phases = buildTimeline([
    ev(1, 'session_start', { task_id: 'task-01' }),
    ev(2, 'user_message', { text: 'go' }),
    ev(4, 'file_changed', { file_path: 'a.py', content: 'x', op: 'modified' }),
    ev(6, 'command_run', { command_id: 'c1', command: 'ls', cwd: '.' }),
    ev(7, 'command_finished', { command_id: 'c1', exit_code: 0, duration_ms: 1 }),
  ]);

  it('returns null for a null target', () => {
    expect(resolveActiveSeq(phases, null)).toBeNull();
  });

  it('returns the greatest header/entry seq ≤ target', () => {
    expect(resolveActiveSeq(phases, 1)).toBe(1); // setup session_start
    expect(resolveActiveSeq(phases, 3)).toBe(2); // user_message header
    expect(resolveActiveSeq(phases, 5)).toBe(4); // file_changed entry
  });

  it('maps a folded command_finished seq to its command_run entry', () => {
    // seq 7 (command_finished) has no row; resolves to the command_run row at seq 6.
    expect(resolveActiveSeq(phases, 7)).toBe(6);
  });

  it('returns null when target precedes every point', () => {
    expect(resolveActiveSeq(phases, 0)).toBeNull();
  });
});
