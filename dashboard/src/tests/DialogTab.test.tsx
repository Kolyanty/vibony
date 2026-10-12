import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { DialogTab } from '../components/DialogTab';
import type { EventEnvelope } from '../api/types';

function setScroll(el: HTMLElement, sh: number, st: number, ch: number) {
  Object.defineProperty(el, 'scrollHeight', { configurable: true, value: sh });
  Object.defineProperty(el, 'scrollTop', { configurable: true, writable: true, value: st });
  Object.defineProperty(el, 'clientHeight', { configurable: true, value: ch });
}

// jsdom doesn't implement scrollTo
beforeEach(() => {
  Object.defineProperty(HTMLElement.prototype, 'scrollTop', {
    writable: true,
    value: 0,
  });
});

// ---- Fixtures ----------------------------------------------------------------

let seq = 0;

function nextSeq() {
  return ++seq;
}

beforeEach(() => {
  seq = 0;
});

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

function makeToolCall(toolName: string, callId: string): EventEnvelope {
  return {
    id: `tc-${nextSeq()}`,
    seq: nextSeq(),
    session_code: 'AAAA-BBBB',
    timestamp: '2026-05-15T10:00:00Z',
    type: 'tool_call',
    payload: { tool_call_id: callId, tool_name: toolName, input: { path: 'train.py' } },
  };
}

function makeToolResult(callId: string, output: string, isError = false): EventEnvelope {
  return {
    id: `tr-${nextSeq()}`,
    seq: nextSeq(),
    session_code: 'AAAA-BBBB',
    timestamp: '2026-05-15T10:00:00Z',
    type: 'tool_result',
    payload: { tool_call_id: callId, output, is_error: isError },
  };
}

function makeFileChanged(): EventEnvelope {
  return {
    id: `fc-${nextSeq()}`,
    seq: nextSeq(),
    session_code: 'AAAA-BBBB',
    timestamp: '2026-05-15T10:00:00Z',
    type: 'file_changed',
    payload: { file_path: 'train.py', diff: '--- a\n+++ b', source: 'agent' },
  };
}

function makeSandboxReset(reason?: 'expired'): EventEnvelope {
  return {
    id: `sr-${nextSeq()}`,
    seq: nextSeq(),
    session_code: 'AAAA-BBBB',
    timestamp: '2026-05-15T10:00:00Z',
    type: 'sandbox_reset',
    payload: reason ? { reason } : {},
  };
}

// ---- Tests -------------------------------------------------------------------

describe('DialogTab', () => {
  it('empty events list → shows "Dialog is empty"', () => {
    render(<DialogTab events={[]} />);
    expect(screen.getByText('Dialog is empty')).toBeInTheDocument();
  });

  it('user_message → renders with label "candidate"', () => {
    render(<DialogTab events={[makeUserMsg('hi, read train.py')]} />);
    expect(screen.getAllByText('candidate').length).toBeGreaterThan(0);
    expect(screen.getByText('hi, read train.py')).toBeInTheDocument();
  });

  it('assistant_message → renders with label "AI"', () => {
    render(<DialogTab events={[makeAssistantMsg('ok, reading the file')]} />);
    expect(screen.getAllByText('AI').length).toBeGreaterThan(0);
    expect(screen.getByText('ok, reading the file')).toBeInTheDocument();
  });

  it('tool_call + tool_result → render as a single block with the tool name', () => {
    const callId = 'call-001';
    const events: EventEnvelope[] = [
      makeToolCall('Read', callId),
      makeToolResult(callId, 'line1\nline2\nline3\n'),
    ];
    render(<DialogTab events={events} />);

    expect(screen.getAllByText('tool').length).toBeGreaterThan(0);
    expect(screen.getByText('Read')).toBeInTheDocument();
    // Read tool_result renders as `✓ N lines`, not as raw output.
    expect(
      screen.getByText((_, el) => el?.textContent === '✓ 3 lines'),
    ).toBeInTheDocument();
  });

  it('tool_call without result → shows "waiting for result…"', () => {
    const events: EventEnvelope[] = [makeToolCall('Read', 'call-002')];
    render(<DialogTab events={events} />);
    expect(screen.getByText(/waiting for result/)).toBeInTheDocument();
  });

  it('file_changed and other non-display types are not rendered in the dialog', () => {
    const events: EventEnvelope[] = [makeFileChanged(), makeUserMsg('hi')];
    render(<DialogTab events={events} />);
    // only one message; file_changed is not shown
    expect(screen.queryByText('agent')).not.toBeInTheDocument();
    expect(screen.getByText('hi')).toBeInTheDocument();
  });

  it('several events of different types render in the correct order', () => {
    const callId = 'call-003';
    const events: EventEnvelope[] = [
      makeUserMsg('first'),
      makeToolCall('Edit', callId),
      makeToolResult(callId, 'ok'),
      makeAssistantMsg('done'),
    ];
    render(<DialogTab events={events} />);

    const items = screen.getAllByRole('paragraph').map((el) => el.textContent);
    // Order: candidate text, tool input block, result, AI text
    // Check role labels in DOM order — filter to <span> nodes to avoid aria-label hits.
    const roles = screen
      .getAllByText(/^(candidate|AI|tool)$/)
      .filter((el) => el.tagName === 'SPAN');
    expect(roles[0]?.textContent).toBe('candidate');
    expect(roles[1]?.textContent).toBe('tool');
    expect(roles[2]?.textContent).toBe('AI');

    // Explicit texts
    expect(screen.getByText('first')).toBeInTheDocument();
    expect(screen.getByText('done')).toBeInTheDocument();
    void items; // satisfy unused-var check
  });

  it('sandbox_reset → system divider in the feed, between messages', () => {
    const events: EventEnvelope[] = [
      makeUserMsg('before reset'),
      makeSandboxReset(),
      makeAssistantMsg('after reset'),
    ];
    render(<DialogTab events={events} />);
    const divider = screen.getByTestId('dialog-system-reset');
    expect(divider).toHaveTextContent('Sandbox reset to baseline');
    expect(screen.getByText('before reset')).toBeInTheDocument();
    expect(screen.getByText('after reset')).toBeInTheDocument();
  });

  it('sandbox_reset reason=expired → expiry label', () => {
    render(<DialogTab events={[makeSandboxReset('expired')]} />);
    expect(screen.getByTestId('dialog-system-reset')).toHaveTextContent(
      'Sandbox expired — reset to baseline',
    );
  });

  it('tool_result with is_error=true → shows the "error" label', () => {
    const callId = 'call-004';
    render(
      <DialogTab
        events={[makeToolCall('Bash', callId), makeToolResult(callId, 'permission denied', true)]}
      />,
    );
    expect(screen.getByText('error')).toBeInTheDocument();
    expect(screen.getByText('permission denied')).toBeInTheDocument();
  });

  // Empty/whitespace-only bubbles are filtered out. Guards against
  // legacy events in the DB (the backend fix only stops new ones).
  it('empty assistant_message → AI bubble is not rendered', () => {
    render(<DialogTab events={[makeUserMsg('hi'), makeAssistantMsg('')]} />);
    expect(screen.getByText('hi')).toBeInTheDocument();
    const aiLabels = screen.queryAllByText('AI').filter((el) => el.tagName === 'SPAN');
    expect(aiLabels.length).toBe(0);
  });

  it('whitespace-only assistant_message → AI bubble is not rendered', () => {
    render(<DialogTab events={[makeUserMsg('hi'), makeAssistantMsg('  \n\t ')]} />);
    const aiLabels = screen.queryAllByText('AI').filter((el) => el.tagName === 'SPAN');
    expect(aiLabels.length).toBe(0);
  });

  it('an empty bubble between non-empty ones is filtered, the rest remain', () => {
    render(
      <DialogTab
        events={[makeUserMsg('first'), makeAssistantMsg(''), makeAssistantMsg('real answer')]}
      />,
    );
    expect(screen.getByText('first')).toBeInTheDocument();
    expect(screen.getByText('real answer')).toBeInTheDocument();
    const aiLabels = screen.queryAllByText('AI').filter((el) => el.tagName === 'SPAN');
    expect(aiLabels.length).toBe(1);
  });

  // The backend agent_status signal forces the typing indicator on for the
  // whole turn — the last-event heuristic alone hides it after an intermediate
  // assistant message (the gap where a tool-heavy turn looked idle).
  it('forceTyping shows the typing indicator even after an assistant message', () => {
    const events = [makeUserMsg('go'), makeAssistantMsg('on it')];
    const { rerender } = render(<DialogTab events={events} sessionActive />);
    expect(screen.queryByTestId('typing-indicator')).not.toBeInTheDocument();

    rerender(<DialogTab events={events} sessionActive forceTyping />);
    expect(screen.getByTestId('typing-indicator')).toBeInTheDocument();
  });
});

// ---- Auto-scroll behaviour --------------------------------------------------

describe('DialogTab auto-scroll', () => {
  it('default: a new event → scrollTop moves to scrollHeight', () => {
    const { rerender } = render(<DialogTab events={[makeUserMsg('a')]} />);
    const scrollEl = screen.getByTestId('dialog-scroll');
    setScroll(scrollEl, 1000, 0, 500);
    rerender(<DialogTab events={[makeUserMsg('a'), makeUserMsg('b')]} />);
    expect(scrollEl.scrollTop).toBe(1000);
    // The button does not appear — we're at the bottom.
    expect(screen.queryByTestId('dialog-jump-button')).toBeNull();
  });

  it('user scrolled up → a new event does not scroll + a button appears', () => {
    const seed = makeUserMsg('a');
    const { rerender } = render(<DialogTab events={[seed]} />);
    const scrollEl = screen.getByTestId('dialog-scroll');
    // 1000 - 0 - 500 = 500 > 60 → not at the bottom.
    setScroll(scrollEl, 1000, 0, 500);
    fireEvent.scroll(scrollEl);
    // Pinned scrollTop so the effect doesn't overwrite it.
    rerender(<DialogTab events={[seed, makeAssistantMsg('b')]} />);
    expect(scrollEl.scrollTop).toBe(0);
    const btn = screen.getByTestId('dialog-jump-button');
    expect(btn).toHaveTextContent('1');
  });

  it('counter accumulates N AI events while not at the bottom', () => {
    const seed = makeUserMsg('a');
    const { rerender } = render(<DialogTab events={[seed]} />);
    const scrollEl = screen.getByTestId('dialog-scroll');
    setScroll(scrollEl, 1000, 0, 500);
    fireEvent.scroll(scrollEl);
    rerender(<DialogTab events={[seed, makeAssistantMsg('b')]} />);
    rerender(<DialogTab events={[seed, makeAssistantMsg('b'), makeAssistantMsg('c')]} />);
    rerender(
      <DialogTab events={[seed, makeAssistantMsg('b'), makeAssistantMsg('c'), makeAssistantMsg('d')]} />,
    );
    expect(screen.getByTestId('dialog-jump-button')).toHaveTextContent('3');
  });

  it('clicking the button → scrollTop = scrollHeight, button disappears', () => {
    const seed = makeUserMsg('a');
    const { rerender } = render(<DialogTab events={[seed]} />);
    const scrollEl = screen.getByTestId('dialog-scroll');
    setScroll(scrollEl, 1000, 0, 500);
    fireEvent.scroll(scrollEl);
    rerender(<DialogTab events={[seed, makeAssistantMsg('b')]} />);
    const btn = screen.getByTestId('dialog-jump-button');
    fireEvent.click(btn);
    expect(scrollEl.scrollTop).toBe(1000);
    expect(screen.queryByTestId('dialog-jump-button')).toBeNull();
  });

  it('tool_result arrives (displayItems does not grow) → still scrolls to the bottom', () => {
    const callId = 'edit-1';
    const { rerender } = render(<DialogTab events={[makeToolCall('Edit', callId)]} />);
    const scrollEl = screen.getByTestId('dialog-scroll');
    setScroll(scrollEl, 2000, 0, 500);
    // tool_result is folded into the tool_call block — displayItems.length doesn't change.
    rerender(
      <DialogTab
        events={[makeToolCall('Edit', callId), makeToolResult(callId, 'ok')]}
      />,
    );
    expect(scrollEl.scrollTop).toBe(2000);
    // pendingCount must not grow — there's no new logical item.
    expect(screen.queryByTestId('dialog-jump-button')).toBeNull();
  });

  it('scrolling back to the bottom manually → counter resets', () => {
    const seed = makeUserMsg('a');
    const { rerender } = render(<DialogTab events={[seed]} />);
    const scrollEl = screen.getByTestId('dialog-scroll');
    setScroll(scrollEl, 1000, 0, 500);
    fireEvent.scroll(scrollEl);
    rerender(<DialogTab events={[seed, makeAssistantMsg('b')]} />);
    expect(screen.getByTestId('dialog-jump-button')).toBeInTheDocument();
    // User scrolled back to the bottom manually: 1000 - 500 - 500 = 0 < 60 → at the bottom.
    setScroll(scrollEl, 1000, 500, 500);
    fireEvent.scroll(scrollEl);
    expect(screen.queryByTestId('dialog-jump-button')).toBeNull();
  });
});

// ---- entry animation + typing indicator -----------------------------

describe('DialogTab — entry animation', () => {
  it('on the first mount, events from initialSeqs do not get the .enter class', () => {
    const events = [makeUserMsg('a'), makeAssistantMsg('b')];
    const { container } = render(<DialogTab events={events} />);
    expect(container.querySelectorAll('[data-new="true"]').length).toBe(0);
  });

  it('a new event after mount gets data-new=true', () => {
    const a = makeUserMsg('a');
    const { rerender, container } = render(<DialogTab events={[a]} />);
    expect(container.querySelectorAll('[data-new="true"]').length).toBe(0);
    const b = makeAssistantMsg('b');
    rerender(<DialogTab events={[a, b]} />);
    const news = container.querySelectorAll('[data-new="true"]');
    expect(news.length).toBe(1);
    expect(news[0]?.textContent).toContain('b');
  });

  it('a repeated rerender of the same events does not mark old ones as new', () => {
    const a = makeUserMsg('a');
    const { rerender, container } = render(<DialogTab events={[a]} />);
    const b = makeAssistantMsg('b');
    rerender(<DialogTab events={[a, b]} />);
    rerender(<DialogTab events={[a, b]} />);
    // only b stays marked as new
    expect(container.querySelectorAll('[data-new="true"]').length).toBe(1);
  });

  it('a new group of Reads (entirely after mount) → data-new=true', () => {
    const seed = makeUserMsg('hi');
    const { rerender, container } = render(<DialogTab events={[seed]} />);
    const r1 = makeToolCall('Read', 'r1');
    const rr1 = makeToolResult('r1', 'x\n');
    const r2 = makeToolCall('Read', 'r2');
    const rr2 = makeToolResult('r2', 'y\n');
    const r3 = makeToolCall('Read', 'r3');
    const rr3 = makeToolResult('r3', 'z\n');
    rerender(<DialogTab events={[seed, r1, rr1, r2, rr2, r3, rr3]} />);
    // seed is old (not marked), the group is new (marked)
    const news = container.querySelectorAll('[data-new="true"]');
    expect(news.length).toBe(1);
    expect(news[0]?.getAttribute('data-testid') ?? news[0]?.querySelector('[data-testid]')?.getAttribute('data-testid')).toContain('tool-group-read');
  });
});

describe('DialogTab — TypingIndicator', () => {
  it('last message is a user_message → indicator visible', () => {
    render(<DialogTab events={[makeUserMsg('help me')]} />);
    expect(screen.getByTestId('typing-indicator')).toBeInTheDocument();
  });

  it('last message is an assistant_message → indicator not visible', () => {
    render(
      <DialogTab events={[makeUserMsg('a'), makeAssistantMsg('done')]} />,
    );
    expect(screen.queryByTestId('typing-indicator')).toBeNull();
  });

  it('user_message + tool_call/tool_result without assistant_message → indicator visible', () => {
    const cid = 'c-1';
    render(
      <DialogTab
        events={[
          makeUserMsg('read the file'),
          makeToolCall('Read', cid),
          makeToolResult(cid, 'data\n'),
        ]}
      />,
    );
    expect(screen.getByTestId('typing-indicator')).toBeInTheDocument();
  });

  it('sessionActive=false → indicator not visible even with a trailing user_message', () => {
    render(<DialogTab events={[makeUserMsg('a')]} sessionActive={false} />);
    expect(screen.queryByTestId('typing-indicator')).toBeNull();
  });

  it('empty events list → indicator not visible', () => {
    render(<DialogTab events={[]} />);
    expect(screen.queryByTestId('typing-indicator')).toBeNull();
    expect(screen.getByText('Dialog is empty')).toBeInTheDocument();
  });

  it('indicator appears without overlapping the "Dialog is empty" placeholder', () => {
    render(<DialogTab events={[makeUserMsg('hi')]} />);
    expect(screen.queryByText('Dialog is empty')).toBeNull();
    expect(screen.getByTestId('typing-indicator')).toBeInTheDocument();
  });
});

// ---- EmptyState instead of "Dialog is empty" while pending ---------------------

describe('DialogTab — EmptyState', () => {
  it('events=[] + sessionStatus=pending + sessionCode → renders EmptyState', () => {
    render(<DialogTab events={[]} sessionStatus="pending" sessionCode="AAAA-BBBB" />);
    expect(screen.getByTestId('dialog-empty-state')).toBeInTheDocument();
    expect(screen.queryByText('Dialog is empty')).toBeNull();
  });

  it('events=[] + sessionStatus=active → fallback to "Dialog is empty"', () => {
    render(<DialogTab events={[]} sessionStatus="active" sessionCode="AAAA-BBBB" />);
    expect(screen.getByText('Dialog is empty')).toBeInTheDocument();
    expect(screen.queryByTestId('dialog-empty-state')).toBeNull();
  });

  it('events=[] + sessionStatus=completed → fallback to "Dialog is empty"', () => {
    render(
      <DialogTab events={[]} sessionStatus="completed" sessionCode="AAAA-BBBB" />,
    );
    expect(screen.getByText('Dialog is empty')).toBeInTheDocument();
    expect(screen.queryByTestId('dialog-empty-state')).toBeNull();
  });

  it('events=[] + sessionStatus=pending without sessionCode → fallback', () => {
    render(<DialogTab events={[]} sessionStatus="pending" />);
    expect(screen.getByText('Dialog is empty')).toBeInTheDocument();
    expect(screen.queryByTestId('dialog-empty-state')).toBeNull();
  });

  it('the first event appears → EmptyState hides', () => {
    const { rerender } = render(
      <DialogTab events={[]} sessionStatus="pending" sessionCode="AAAA-BBBB" />,
    );
    expect(screen.getByTestId('dialog-empty-state')).toBeInTheDocument();
    rerender(
      <DialogTab
        events={[makeUserMsg('hi')]}
        sessionStatus="active"
        sessionCode="AAAA-BBBB"
      />,
    );
    expect(screen.queryByTestId('dialog-empty-state')).toBeNull();
    expect(screen.getByText('hi')).toBeInTheDocument();
  });
});

// ---- force-scroll on user message send ---------------------------

describe('DialogTab — send-force-scroll', () => {
  it('sending your own message while scrolled up → forced scroll to the bottom', () => {
    const seed = makeUserMsg('first');
    const { rerender } = render(<DialogTab events={[seed]} />);
    const scrollEl = screen.getByTestId('dialog-scroll');
    setScroll(scrollEl, 1000, 0, 500);
    fireEvent.scroll(scrollEl); // pin atBottom=false

    rerender(<DialogTab events={[seed, makeUserMsg('new question')]} />);

    expect(scrollEl.scrollTop).toBe(1000);
    expect(screen.queryByTestId('dialog-jump-button')).toBeNull();
  });

  it('an AI message while scrolled up → does not scroll, a button appears', () => {
    const seed = makeUserMsg('first');
    const { rerender } = render(<DialogTab events={[seed]} />);
    const scrollEl = screen.getByTestId('dialog-scroll');
    setScroll(scrollEl, 1000, 0, 500);
    fireEvent.scroll(scrollEl);

    rerender(<DialogTab events={[seed, makeAssistantMsg('AI response')]} />);

    expect(scrollEl.scrollTop).toBe(0);
    expect(screen.getByTestId('dialog-jump-button')).toBeInTheDocument();
  });

  it('a user_message from the initial snapshot (on mount) does not trigger force-scroll', () => {
    const msgs = [makeUserMsg('old1'), makeAssistantMsg('old2')];
    const { rerender } = render(<DialogTab events={msgs} />);
    const scrollEl = screen.getByTestId('dialog-scroll');
    setScroll(scrollEl, 1000, 0, 500);
    fireEvent.scroll(scrollEl);

    rerender(<DialogTab events={msgs} />);

    expect(scrollEl.scrollTop).toBe(0);
  });

  it('force-scroll resets the accumulated pending counter', () => {
    const seed = makeUserMsg('seed');
    const { rerender } = render(<DialogTab events={[seed]} />);
    const scrollEl = screen.getByTestId('dialog-scroll');
    setScroll(scrollEl, 1000, 0, 500);
    fireEvent.scroll(scrollEl);

    // Accumulate pending from AI messages
    rerender(<DialogTab events={[seed, makeAssistantMsg('r1')]} />);
    rerender(<DialogTab events={[seed, makeAssistantMsg('r1'), makeAssistantMsg('r2')]} />);
    expect(screen.getByTestId('dialog-jump-button')).toBeInTheDocument();

    // User sends — force-scroll clears the counter
    rerender(
      <DialogTab
        events={[seed, makeAssistantMsg('r1'), makeAssistantMsg('r2'), makeUserMsg('follow-up')]}
      />,
    );
    expect(scrollEl.scrollTop).toBe(1000);
    expect(screen.queryByTestId('dialog-jump-button')).toBeNull();
  });
});

// ---- collapse heavy output via isLatest ----------------------------------

describe('DialogTab — isLatest for heavy output', () => {
  it('Read as the last item → preview expanded, not last → collapsed', () => {
    const cidLast = 'read-last';
    const cidPrev = 'read-prev';
    const { container } = render(
      <DialogTab
        events={[
          makeToolCall('Read', cidPrev),
          makeToolResult(cidPrev, 'EARLIER\n'),
          makeAssistantMsg('thinking'),
          makeToolCall('Read', cidLast),
          makeToolResult(cidLast, 'LATEST\n'),
        ]}
      />,
    );
    expect(container.textContent).toContain('LATEST');
    expect(container.textContent).not.toContain('EARLIER');
  });

  it('Edit with a large diff as the last item → expanded directly (no toggle)', () => {
    const big =
      '--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,N @@\n' +
      Array.from({ length: 25 }, (_, i) => `+line ${i + 1}`).join('\n') + '\n';
    const cid = 'edit-last';
    const events: EventEnvelope[] = [
      {
        id: `tc-${nextSeq()}`,
        seq: nextSeq(),
        session_code: 'AAAA-BBBB',
        timestamp: '2026-05-15T10:00:00Z',
        type: 'tool_call',
        payload: { tool_call_id: cid, tool_name: 'Edit', input: { path: 'x.py' } },
      },
      {
        id: `tr-${nextSeq()}`,
        seq: nextSeq(),
        session_code: 'AAAA-BBBB',
        timestamp: '2026-05-15T10:00:00Z',
        type: 'tool_result',
        payload: { tool_call_id: cid, output: '', is_error: false, diff: big },
      },
    ];
    const { container } = render(<DialogTab events={events} />);
    expect(screen.queryByTestId('tool-result-diff')).toBeNull();
    expect(container.textContent).toContain('+line 1');
  });
});
