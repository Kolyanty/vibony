import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { TypingIndicator } from '../components/TypingIndicator';

describe('TypingIndicator', () => {
  it('renders three dots in the AI bubble', () => {
    const { container } = render(<TypingIndicator />);
    expect(screen.getByTestId('typing-indicator')).toBeInTheDocument();
    // Only three spans — the dots. Ignore the SVG avatars.
    const dots = container.querySelectorAll('span[aria-hidden="true"]');
    expect(dots.length).toBe(3);
  });

  it('is accessible to screen readers via role=status and aria-label', () => {
    render(<TypingIndicator />);
    const bubble = screen.getByRole('status');
    expect(bubble).toHaveAttribute('aria-label', 'AI is typing');
  });

  it('renders the avatar in the ai role', () => {
    render(<TypingIndicator />);
    const avatar = screen.getByRole('img', { name: 'AI' });
    expect(avatar).toHaveAttribute('data-role', 'ai');
  });
});
