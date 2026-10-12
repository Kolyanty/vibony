import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ReplayTimeline } from '../components/ReplayTimeline';
import type { EventEnvelope } from '../api/types';

function makeEvent(seq: number): EventEnvelope {
  return {
    id: `evt-${seq}`,
    seq,
    session_code: 'TEST-CODE',
    timestamp: '2026-05-19T10:00:00Z',
    type: 'user_message',
    payload: { text: `msg ${seq}` },
  } as EventEnvelope;
}

const events = [makeEvent(1), makeEvent(2), makeEvent(3), makeEvent(4), makeEvent(5)];

describe('ReplayTimeline', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders with play button and slider', () => {
    const onCursor = vi.fn();
    render(
      <ReplayTimeline total={5} cursor={3} events={events} onCursor={onCursor} />,
    );
    expect(screen.getByRole('button', { name: /play/i })).toBeInTheDocument();
    expect(screen.getByRole('slider')).toBeInTheDocument();
  });

  it('returns null when total is 0', () => {
    const { container } = render(
      <ReplayTimeline total={0} cursor={0} events={[]} onCursor={vi.fn()} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it('shows position counter', () => {
    render(
      <ReplayTimeline total={5} cursor={3} events={events} onCursor={vi.fn()} />,
    );
    expect(screen.getByText('3 / 5')).toBeInTheDocument();
  });

  it('slider change calls onCursor with numeric value', () => {
    const onCursor = vi.fn();
    render(
      <ReplayTimeline total={5} cursor={2} events={events} onCursor={onCursor} />,
    );
    const slider = screen.getByRole('slider');
    fireEvent.change(slider, { target: { value: '4' } });
    expect(onCursor).toHaveBeenCalledWith(4);
  });

  it('skip-to-start button calls onCursor(0)', () => {
    const onCursor = vi.fn();
    render(
      <ReplayTimeline total={5} cursor={3} events={events} onCursor={onCursor} />,
    );
    fireEvent.click(screen.getByRole('button', { name: /start/i }));
    expect(onCursor).toHaveBeenCalledWith(0);
  });

  it('skip-to-end button calls onCursor(total)', () => {
    const onCursor = vi.fn();
    render(
      <ReplayTimeline total={5} cursor={2} events={events} onCursor={onCursor} />,
    );
    fireEvent.click(screen.getByRole('button', { name: /end/i }));
    expect(onCursor).toHaveBeenCalledWith(5);
  });

  it('play button shows pause after click', () => {
    const onCursor = vi.fn();
    render(
      <ReplayTimeline total={5} cursor={0} events={events} onCursor={onCursor} />,
    );
    const playBtn = screen.getByRole('button', { name: /play/i });
    act(() => {
      fireEvent.click(playBtn);
    });
    expect(screen.getByRole('button', { name: /pause/i })).toBeInTheDocument();
  });

  it('pause button stops playback', () => {
    const onCursor = vi.fn();
    render(
      <ReplayTimeline total={5} cursor={0} events={events} onCursor={onCursor} />,
    );
    act(() => {
      fireEvent.click(screen.getByRole('button', { name: /play/i }));
    });
    act(() => {
      fireEvent.click(screen.getByRole('button', { name: /pause/i }));
    });
    expect(screen.getByRole('button', { name: /play/i })).toBeInTheDocument();
  });
});
