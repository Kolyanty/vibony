import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ToolCallBlock } from '../components/ToolCallBlock';
import type { EventEnvelope, PermissionDecision } from '../api/types';

const TS = '2026-05-15T10:00:00Z';

function makeBashCall(
  command: string,
  decision?: PermissionDecision,
): Extract<EventEnvelope, { type: 'tool_call' }> {
  return {
    id: 'tc-bash',
    seq: 1,
    session_code: 'AAAA-BBBB',
    timestamp: TS,
    type: 'tool_call',
    payload: {
      tool_call_id: 'cid-bash',
      tool_name: 'Bash',
      input: { command },
      ...(decision !== undefined ? { permission_decision: decision } : {}),
    },
  };
}

describe('ToolCallBlock — permission badge', () => {
  it('user_allow renders "approved" badge', () => {
    render(<ToolCallBlock call={makeBashCall('pytest', 'user_allow')} />);
    const badge = screen.getByRole('status');
    expect(badge).toHaveTextContent('approved');
    expect(badge).toHaveAttribute('data-decision', 'user_allow');
  });

  it('user_deny renders "denied by user" badge', () => {
    render(<ToolCallBlock call={makeBashCall('rm notes.md', 'user_deny')} />);
    const badge = screen.getByRole('status');
    expect(badge).toHaveTextContent('denied by user');
    expect(badge).toHaveAttribute('data-decision', 'user_deny');
  });

  it('policy_deny renders "blocked by policy" badge', () => {
    render(<ToolCallBlock call={makeBashCall('sudo ls', 'policy_deny')} />);
    const badge = screen.getByRole('status');
    expect(badge).toHaveTextContent('blocked by policy');
    expect(badge).toHaveAttribute('data-decision', 'policy_deny');
  });

  it('auto_allow renders no badge', () => {
    render(<ToolCallBlock call={makeBashCall('pytest', 'auto_allow')} />);
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('missing permission_decision renders no badge', () => {
    render(<ToolCallBlock call={makeBashCall('pytest')} />);
    expect(screen.queryByRole('status')).toBeNull();
  });
});
