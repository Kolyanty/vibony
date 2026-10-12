import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { MessageRow } from '../components/MessageRow';
import type { EventEnvelope } from '../api/types';

function makeAssistant(text: string): Extract<EventEnvelope, { type: 'assistant_message' }> {
  return {
    id: 'a-1',
    seq: 1,
    session_code: 'AAAA-BBBB',
    timestamp: '2026-05-15T10:00:00Z',
    type: 'assistant_message',
    payload: { text },
  };
}

function makeUser(text: string): Extract<EventEnvelope, { type: 'user_message' }> {
  return {
    id: 'u-1',
    seq: 1,
    session_code: 'AAAA-BBBB',
    timestamp: '2026-05-15T10:00:00Z',
    type: 'user_message',
    payload: { text },
  };
}

describe('MessageRow markdown rendering', () => {
  it('assistant_message: заголовок второго уровня рендерится как <h2>', () => {
    render(<MessageRow event={makeAssistant('## Что я сделал\n\nвот так')} />);
    const heading = screen.getByRole('heading', { level: 2 });
    expect(heading).toHaveTextContent('Что я сделал');
  });

  it('assistant_message: список рендерится как <ul><li>', () => {
    render(<MessageRow event={makeAssistant('- один\n- два\n- три')} />);
    const list = screen.getByRole('list');
    const items = within(list).getAllByRole('listitem');
    expect(items).toHaveLength(3);
    expect(items[0]).toHaveTextContent('один');
  });

  it('assistant_message: inline-код рендерится как <code> без <pre>', () => {
    render(<MessageRow event={makeAssistant('используй `pytest -k checkpoint` для отладки')} />);
    const code = screen.getByText('pytest -k checkpoint');
    expect(code.tagName).toBe('CODE');
    // must not be inside pre/div fenced
    expect(code.closest('pre')).toBeNull();
  });

  it('assistant_message: fenced код с языком — есть моноширинный блок с текстом', () => {
    const src = 'смотри:\n\n```python\nprint("hi")\n```\n';
    render(<MessageRow event={makeAssistant(src)} />);
    // SyntaxHighlighter renders tokenized text; collect all code nodes
    const codes = document.querySelectorAll('code');
    const joined = Array.from(codes)
      .map((c) => c.textContent ?? '')
      .join(' ');
    expect(joined).toContain('print');
    expect(joined).toContain('"hi"');
  });

  it('assistant_message: ссылка получает target=_blank + rel=noopener noreferrer', () => {
    render(<MessageRow event={makeAssistant('[docs](https://example.com/x)')} />);
    const link = screen.getByRole('link', { name: 'docs' });
    expect(link).toHaveAttribute('href', 'https://example.com/x');
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', expect.stringContaining('noopener'));
  });

  it('assistant_message: raw HTML не рендерится как HTML (XSS-защита)', () => {
    render(<MessageRow event={makeAssistant('<script>alert(1)</script>текст после')} />);
    // react-markdown by default does not render raw HTML → <script> comes as plain text
    expect(document.querySelector('script')).toBeNull();
    expect(screen.getByText(/текст после/)).toBeInTheDocument();
  });

  it('user_message: остаётся plain text (markdown не парсится)', () => {
    render(<MessageRow event={makeUser('## не заголовок\n- не список')} />);
    expect(screen.queryByRole('heading')).toBeNull();
    expect(screen.queryByRole('list')).toBeNull();
    // Text is shown as-is
    expect(screen.getByText(/## не заголовок/)).toBeInTheDocument();
  });

  it('user_message: renders label "candidate", assistant — "AI"', () => {
    const { rerender } = render(<MessageRow event={makeUser('hi')} />);
    expect(screen.getAllByText('candidate').length).toBeGreaterThan(0);
    rerender(<MessageRow event={makeAssistant('hello')} />);
    expect(screen.getAllByText('AI').length).toBeGreaterThan(0);
  });

  it('user_message: shows candidate avatar (data-role="candidate")', () => {
    render(<MessageRow event={makeUser('hi')} />);
    const avatar = screen.getByRole('img', { name: 'candidate' });
    expect(avatar).toHaveAttribute('data-role', 'candidate');
  });

  it('assistant_message: показывает avatar AI (data-role="ai")', () => {
    render(<MessageRow event={makeAssistant('hi')} />);
    const avatar = screen.getByRole('img', { name: 'AI' });
    expect(avatar).toHaveAttribute('data-role', 'ai');
  });
});
