import { fireEvent, render, screen, within } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { DialogTab } from '../components/DialogTab';
import { groupConsecutiveTools } from '../components/dialogTabUtils';
import type { EventEnvelope } from '../api/types';

let seq = 0;
function nextSeq() {
  return ++seq;
}
beforeEach(() => {
  seq = 0;
});

function makeToolCall(toolName: string, callId: string): EventEnvelope {
  return {
    id: `tc-${callId}`,
    seq: nextSeq(),
    session_code: 'AAAA-BBBB',
    timestamp: '2026-05-15T10:00:00Z',
    type: 'tool_call',
    payload: { tool_call_id: callId, tool_name: toolName, input: { path: `f${callId}.py` } },
  };
}

function makeToolResult(callId: string, output: string): EventEnvelope {
  return {
    id: `tr-${callId}`,
    seq: nextSeq(),
    session_code: 'AAAA-BBBB',
    timestamp: '2026-05-15T10:00:00Z',
    type: 'tool_result',
    payload: { tool_call_id: callId, output, is_error: false },
  };
}

function makeUserMsg(text: string): EventEnvelope {
  return {
    id: `u-${nextSeq()}`,
    seq: nextSeq(),
    session_code: 'AAAA-BBBB',
    timestamp: '2026-05-15T10:00:00Z',
    type: 'user_message',
    payload: { text },
  };
}

function makeAssistantMsg(text: string): EventEnvelope {
  return {
    id: `a-${nextSeq()}`,
    seq: nextSeq(),
    session_code: 'AAAA-BBBB',
    timestamp: '2026-05-15T10:00:00Z',
    type: 'assistant_message',
    payload: { text },
  };
}

// ---- Pure function: groupConsecutiveTools -----------------------------------

describe('groupConsecutiveTools', () => {
  it('3 Read in a row → one group of size 3', () => {
    const items = [
      makeToolCall('Read', '1'),
      makeToolCall('Read', '2'),
      makeToolCall('Read', '3'),
    ];
    const out = groupConsecutiveTools(items as never);
    expect(out).toHaveLength(1);
    expect(out[0]?.kind).toBe('group');
    if (out[0]?.kind === 'group') {
      expect(out[0].calls).toHaveLength(3);
    }
  });

  it('Read + Edit + Read → three separate items, no group', () => {
    const items = [
      makeToolCall('Read', '1'),
      makeToolCall('Edit', '2'),
      makeToolCall('Read', '3'),
    ];
    const out = groupConsecutiveTools(items as never);
    expect(out).toHaveLength(3);
    expect(out.every((it) => it.kind === 'tool')).toBe(true);
  });

  it('2 Read in a row → group of size 2', () => {
    const items = [makeToolCall('Read', '1'), makeToolCall('Read', '2')];
    const out = groupConsecutiveTools(items as never);
    expect(out).toHaveLength(1);
    expect(out[0]?.kind).toBe('group');
  });

  it('a single Read → not a group, a plain tool item', () => {
    const out = groupConsecutiveTools([makeToolCall('Read', '1')] as never);
    expect(out).toHaveLength(1);
    expect(out[0]?.kind).toBe('tool');
  });

  it('Read + Glob (different tools) → two separate items, no group', () => {
    const items = [makeToolCall('Read', '1'), makeToolCall('Glob', '2')];
    const out = groupConsecutiveTools(items as never);
    expect(out).toHaveLength(2);
    expect(out.every((it) => it.kind === 'tool')).toBe(true);
  });

  it('Read + Read + Glob + Glob → two groups (Read×2 + Glob×2)', () => {
    const items = [
      makeToolCall('Read', '1'),
      makeToolCall('Read', '2'),
      makeToolCall('Glob', '3'),
      makeToolCall('Glob', '4'),
    ];
    const out = groupConsecutiveTools(items as never);
    expect(out).toHaveLength(2);
    expect(out[0]?.kind).toBe('group');
    expect(out[1]?.kind).toBe('group');
  });

  it('Bash×3 → not grouped (Bash not in GROUPABLE)', () => {
    const items = [
      makeToolCall('Bash', '1'),
      makeToolCall('Bash', '2'),
      makeToolCall('Bash', '3'),
    ];
    const out = groupConsecutiveTools(items as never);
    expect(out).toHaveLength(3);
    expect(out.every((it) => it.kind === 'tool')).toBe(true);
  });

  it('Edit×3 → not grouped', () => {
    const items = [
      makeToolCall('Edit', '1'),
      makeToolCall('Edit', '2'),
      makeToolCall('Edit', '3'),
    ];
    const out = groupConsecutiveTools(items as never);
    expect(out).toHaveLength(3);
  });

  it('a user_message between Reads breaks the group', () => {
    const items = [
      makeToolCall('Read', '1'),
      makeToolCall('Read', '2'),
      makeUserMsg('pause'),
      makeToolCall('Read', '3'),
      makeToolCall('Read', '4'),
    ];
    const out = groupConsecutiveTools(items as never);
    expect(out).toHaveLength(3);
    expect(out[0]?.kind).toBe('group');
    expect(out[1]?.kind).toBe('message');
    expect(out[2]?.kind).toBe('group');
  });

  it('an assistant_message between Reads breaks the group', () => {
    const items = [
      makeToolCall('Read', '1'),
      makeAssistantMsg('thinking'),
      makeToolCall('Read', '2'),
    ];
    const out = groupConsecutiveTools(items as never);
    expect(out).toHaveLength(3);
    expect(out.every((it) => it.kind !== 'group')).toBe(true);
  });

  it('empty input → empty output', () => {
    expect(groupConsecutiveTools([])).toEqual([]);
  });
});

// ---- Integration: render group inside DialogTab -----------------------------

describe('ToolGroup (integration via DialogTab)', () => {
  it('3 Read in a row → collapsed by default, one header with count in the DOM', () => {
    const events: EventEnvelope[] = [
      makeToolCall('Read', '1'),
      makeToolResult('1', 'a\n'),
      makeToolCall('Read', '2'),
      makeToolResult('2', 'b\n'),
      makeToolCall('Read', '3'),
      makeToolResult('3', 'c\n'),
    ];
    render(<DialogTab events={events} />);

    const group = screen.getByTestId('tool-group-read');
    expect(group).toHaveAttribute('data-expanded', 'false');
    const header = within(group).getByRole('button');
    expect(header).toHaveAttribute('aria-expanded', 'false');
    expect(within(group).getByText('Read')).toBeInTheDocument();
    expect(within(group).getByText('× 3')).toBeInTheDocument();
    // The inner ToolCallBlocks are not rendered — no "tool" label at all.
    expect(screen.queryByText('tool')).toBeNull();
  });

  it('clicking the header expands the group — 3 ToolCallBlocks inside', () => {
    const events: EventEnvelope[] = [
      makeToolCall('Read', '1'),
      makeToolResult('1', 'a\n'),
      makeToolCall('Read', '2'),
      makeToolResult('2', 'b\n'),
      makeToolCall('Read', '3'),
      makeToolResult('3', 'c\n'),
    ];
    render(<DialogTab events={events} />);

    const group = screen.getByTestId('tool-group-read');
    const header = within(group).getByRole('button');
    fireEvent.click(header);

    expect(group).toHaveAttribute('data-expanded', 'true');
    expect(header).toHaveAttribute('aria-expanded', 'true');
    // 3 expanded blocks — 3 "tool" labels.
    expect(screen.getAllByText('tool')).toHaveLength(3);
  });

  it('2 Read in a row → group expanded by default', () => {
    const events: EventEnvelope[] = [
      makeToolCall('Read', '1'),
      makeToolResult('1', 'a\n'),
      makeToolCall('Read', '2'),
      makeToolResult('2', 'b\n'),
    ];
    render(<DialogTab events={events} />);

    const group = screen.getByTestId('tool-group-read');
    expect(group).toHaveAttribute('data-expanded', 'true');
    expect(screen.getAllByText('tool')).toHaveLength(2);
  });

  it('a single Read renders without a group wrapper', () => {
    const events: EventEnvelope[] = [
      makeToolCall('Read', '1'),
      makeToolResult('1', 'a\n'),
    ];
    render(<DialogTab events={events} />);
    expect(screen.queryByTestId('tool-group-read')).toBeNull();
    expect(screen.getByText('tool')).toBeInTheDocument();
  });

  it('Glob×3 → tool-group-glob group with count × 3', () => {
    const events: EventEnvelope[] = [
      makeToolCall('Glob', '1'),
      makeToolResult('1', 'a.py\n'),
      makeToolCall('Glob', '2'),
      makeToolResult('2', 'b.py\n'),
      makeToolCall('Glob', '3'),
      makeToolResult('3', 'c.py\n'),
    ];
    render(<DialogTab events={events} />);

    const group = screen.getByTestId('tool-group-glob');
    expect(within(group).getByText('Glob')).toBeInTheDocument();
    expect(within(group).getByText('× 3')).toBeInTheDocument();
  });

  it('the group has one header for the whole group (not one per item)', () => {
    const events: EventEnvelope[] = [
      makeToolCall('Read', '1'),
      makeToolResult('1', 'a\n'),
      makeToolCall('Read', '2'),
      makeToolResult('2', 'b\n'),
      makeToolCall('Read', '3'),
      makeToolResult('3', 'c\n'),
    ];
    render(<DialogTab events={events} />);
    expect(screen.getAllByTestId('tool-group-read')).toHaveLength(1);
  });
});
