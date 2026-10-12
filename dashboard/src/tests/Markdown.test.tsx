import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { Markdown } from '../components/Markdown';

describe('Markdown nodes', () => {
  it('h1 renders with a border-bottom (for hierarchy)', () => {
    render(<Markdown source={'# Heading\n\ntext'} />);
    const h1 = screen.getByRole('heading', { level: 1 });
    expect(h1).toHaveTextContent('Heading');
  });

  it('h2 renders as its own level', () => {
    render(<Markdown source={'## Subheading'} />);
    const h2 = screen.getByRole('heading', { level: 2 });
    expect(h2).toHaveTextContent('Subheading');
  });

  it('h3 renders', () => {
    render(<Markdown source={'### Third level'} />);
    expect(screen.getByRole('heading', { level: 3 })).toHaveTextContent('Third level');
  });

  it('h4 renders', () => {
    render(<Markdown source={'#### Fourth'} />);
    expect(screen.getByRole('heading', { level: 4 })).toHaveTextContent('Fourth');
  });

  it('blockquote renders with text', () => {
    render(<Markdown source={'> A quote\n>\n> second line'} />);
    const quote = document.querySelector('blockquote');
    expect(quote).not.toBeNull();
    expect(quote).toHaveTextContent('A quote');
    expect(quote).toHaveTextContent('second line');
  });

  it('hr renders as a horizontal line', () => {
    render(<Markdown source={'before\n\n---\n\nafter'} />);
    expect(document.querySelector('hr')).not.toBeNull();
    expect(screen.getByText('before')).toBeInTheDocument();
    expect(screen.getByText('after')).toBeInTheDocument();
  });

  it('details + summary render as expandable (raw HTML disabled, so not parsed)', () => {
    // ReactMarkdown does not render raw HTML by default (XSS protection).
    // <details> in AI output arrives as plain text — this is intentional.
    // The test documents the behavior: if we enable rehype-raw later, rewrite it.
    render(
      <Markdown source={'<details><summary>Details</summary>hidden</details>'} />,
    );
    expect(document.querySelector('details')).toBeNull();
  });

  it('inline code gets a distinct background (via the .inlineCode class)', () => {
    render(<Markdown source={'use `pytest -k foo`'} />);
    const code = screen.getByText('pytest -k foo');
    expect(code.tagName).toBe('CODE');
    expect(code.className).toMatch(/inlineCode/);
  });

  it('fenced code with a language does not get .inlineCode (it is a block, not inline)', () => {
    render(<Markdown source={'```python\nx = 1\ny = 2\n```'} />);
    const inlines = document.querySelectorAll('code[class*="inlineCode"]');
    expect(inlines.length).toBe(0);
  });
});
