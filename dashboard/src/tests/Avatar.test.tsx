import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { Avatar } from '../components/Avatar';

describe('Avatar', () => {
  it('candidate → role=img with aria-label "candidate" and data-role="candidate"', () => {
    render(<Avatar role="candidate" />);
    const el = screen.getByRole('img', { name: 'candidate' });
    expect(el).toHaveAttribute('data-role', 'candidate');
    expect(el).toHaveAttribute('data-size', 'md');
  });

  it('ai → aria-label "AI"', () => {
    render(<Avatar role="ai" />);
    expect(screen.getByRole('img', { name: 'AI' })).toBeInTheDocument();
  });

  it('tool → aria-label "tool" and data-role="tool"', () => {
    render(<Avatar role="tool" />);
    const el = screen.getByRole('img', { name: 'tool' });
    expect(el).toHaveAttribute('data-role', 'tool');
  });

  it('size="sm" forwards through data-size', () => {
    render(<Avatar role="tool" size="sm" />);
    expect(screen.getByRole('img', { name: 'tool' })).toHaveAttribute('data-size', 'sm');
  });

  it('contains SVG icon with aria-hidden', () => {
    const { container } = render(<Avatar role="ai" />);
    const svg = container.querySelector('svg');
    expect(svg).not.toBeNull();
    expect(svg).toHaveAttribute('aria-hidden', 'true');
  });
});
