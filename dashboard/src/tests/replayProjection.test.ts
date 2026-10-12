import { describe, expect, it } from 'vitest';
import type { EventEnvelope } from '../api/types';
import { replayModifiedPaths, replayTreePaths } from '../components/replayProjection';

function fc(
  seq: number,
  file_path: string,
  payload: Record<string, unknown> = {},
): EventEnvelope {
  return {
    id: `e${seq}`,
    seq,
    session_code: 'X',
    timestamp: '2026-01-01T00:00:00Z',
    type: 'file_changed',
    payload: { file_path, ...payload },
  } as EventEnvelope;
}

function sandboxReset(seq: number): EventEnvelope {
  return {
    id: `e${seq}`,
    seq,
    session_code: 'X',
    timestamp: '2026-01-01T00:00:00Z',
    type: 'sandbox_reset',
    payload: {},
  } as EventEnvelope;
}

describe('replayTreePaths', () => {
  it('includes created files', () => {
    expect(replayTreePaths([fc(1, 'a.py', { content: 'x', op: 'created' })])).toEqual(['a.py']);
  });

  it('drops a file deleted via op:deleted', () => {
    expect(
      replayTreePaths([
        fc(1, 'a.py', { content: 'x', op: 'created' }),
        fc(2, 'a.py', { content: null, op: 'deleted' }),
      ]),
    ).toEqual([]);
  });

  it('drops a file deleted via legacy content:null (no op)', () => {
    expect(
      replayTreePaths([
        fc(1, 'a.py', { content: 'x' }),
        fc(2, 'a.py', { content: null }),
      ]),
    ).toEqual([]);
  });

  it('ignores directory events', () => {
    expect(replayTreePaths([fc(1, 'dir', { is_dir: true, content: null })])).toEqual([]);
  });

  it('restores baseline and drops candidate files on sandbox_reset', () => {
    const paths = replayTreePaths([
      fc(1, 'baseline.py', { content: 'b', source: 'baseline' }),
      fc(2, 'candidate.py', { content: 'c', op: 'created' }),
      sandboxReset(3),
    ]);
    expect(paths).toEqual(['baseline.py']);
  });

  it('keeps files created after a reset', () => {
    const paths = replayTreePaths([
      fc(1, 'baseline.py', { content: 'b', source: 'baseline' }),
      sandboxReset(2),
      fc(3, 'after.py', { content: 'a', op: 'created' }),
    ]);
    expect(paths.sort()).toEqual(['after.py', 'baseline.py']);
  });
});

describe('replayModifiedPaths', () => {
  it('marks non-baseline edits', () => {
    expect([...replayModifiedPaths([fc(1, 'a.py', { content: 'x', op: 'modified' })])]).toEqual([
      'a.py',
    ]);
  });

  it('excludes baseline-sourced changes', () => {
    expect([...replayModifiedPaths([fc(1, 'a.py', { content: 'x', source: 'baseline' })])]).toEqual(
      [],
    );
  });

  it('unmarks a deleted file', () => {
    expect([
      ...replayModifiedPaths([
        fc(1, 'a.py', { content: 'x', op: 'modified' }),
        fc(2, 'a.py', { content: null, op: 'deleted' }),
      ]),
    ]).toEqual([]);
  });

  it('clears on sandbox_reset', () => {
    expect([
      ...replayModifiedPaths([
        fc(1, 'a.py', { content: 'x', op: 'modified' }),
        sandboxReset(2),
      ]),
    ]).toEqual([]);
  });
});
