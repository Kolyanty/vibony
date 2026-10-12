import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AssessmentTab } from '../components/AssessmentTab';
import type { SessionAssessment } from '../api/types';

function jsonResponse(body: unknown) {
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
}

const READY: SessionAssessment = {
  status: 'ready',
  summary_md: '## What went well\nSolid checkpointing.',
  findings: [
    {
      category: 'gap',
      title: 'No optimizer state in checkpoint',
      detail: 'Only model weights were saved.',
      severity: 'blocking',
      event_seq: 7,
    },
    {
      category: 'strength',
      title: 'Verified by running the loop',
      detail: 'Executed train.py and read the output.',
      severity: null,
      event_seq: null,
    },
  ],
  model: 'deepseek/deepseek-chat',
  error: null,
  created_at: '2026-06-01T10:00:00Z',
  updated_at: '2026-06-01T10:01:00Z',
};

/** Mock fetch routing GET vs POST regenerate. */
function mockFetch(getBody: SessionAssessment, regenBody?: SessionAssessment) {
  const fetchMock = vi.fn().mockImplementation((url: string, opts?: RequestInit) => {
    if (typeof url === 'string' && url.includes('/assessment/regenerate')) {
      return jsonResponse(regenBody ?? { ...getBody, status: 'pending' });
    }
    if (opts?.method === 'POST') return jsonResponse(regenBody ?? getBody);
    return jsonResponse(getBody);
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

describe('AssessmentTab', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('renders ready: summary + findings with category/severity/seq', async () => {
    mockFetch(READY);
    render(<AssessmentTab code="AAAA-BBBB" sessionCompleted />);

    await waitFor(() => expect(screen.getByTestId('assessment-ready')).toBeInTheDocument());
    expect(screen.getByTestId('assessment-summary')).toHaveTextContent('What went well');

    const findings = screen.getAllByTestId('assessment-finding');
    expect(findings).toHaveLength(2);
    expect(findings[0]).toHaveTextContent('No optimizer state in checkpoint');
    expect(findings[0]).toHaveTextContent('Gap');
    expect(findings[0]).toHaveTextContent('Blocking');
    expect(screen.getByTestId('assessment-finding-seq')).toHaveTextContent('event #7');
    expect(screen.getByTestId('assessment-model')).toHaveTextContent('deepseek/deepseek-chat');
  });

  it('renders pending as a generating spinner', async () => {
    mockFetch({ ...READY, status: 'pending', summary_md: null, findings: [], model: null });
    render(<AssessmentTab code="AAAA-BBBB" sessionCompleted />);
    await waitFor(() =>
      expect(screen.getByTestId('assessment-generating')).toBeInTheDocument(),
    );
  });

  it('renders failed with the error and a regenerate button', async () => {
    mockFetch({
      ...READY,
      status: 'failed',
      summary_md: null,
      findings: [],
      error: 'llm timeout',
    });
    render(<AssessmentTab code="AAAA-BBBB" sessionCompleted />);
    await waitFor(() => expect(screen.getByTestId('assessment-failed')).toBeInTheDocument());
    expect(screen.getByTestId('assessment-failed')).toHaveTextContent('llm timeout');
    expect(screen.getByTestId('assessment-regenerate')).toBeInTheDocument();
  });

  it('not_started on a completed session offers Generate → calls regenerate', async () => {
    const fetchMock = mockFetch(
      { ...READY, status: 'not_started', summary_md: null, findings: [], model: null },
      { ...READY, status: 'pending', summary_md: null, findings: [], model: null },
    );
    render(<AssessmentTab code="AAAA-BBBB" sessionCompleted />);

    await waitFor(() =>
      expect(screen.getByTestId('assessment-not-started')).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId('assessment-generate'));

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          ([u]) => typeof u === 'string' && u.includes('/assessment/regenerate'),
        ),
      ).toBe(true),
    );
  });

  it('not_started on a non-completed session shows the pending hint, no Generate', async () => {
    mockFetch({ ...READY, status: 'not_started', summary_md: null, findings: [], model: null });
    render(<AssessmentTab code="AAAA-BBBB" sessionCompleted={false} />);
    await waitFor(() =>
      expect(screen.getByTestId('assessment-not-started')).toBeInTheDocument(),
    );
    expect(screen.queryByTestId('assessment-generate')).not.toBeInTheDocument();
  });

  it('regenerate from ready calls the API', async () => {
    const fetchMock = mockFetch(READY, {
      ...READY,
      status: 'pending',
      summary_md: null,
      findings: [],
    });
    render(<AssessmentTab code="AAAA-BBBB" sessionCompleted />);
    await waitFor(() => expect(screen.getByTestId('assessment-ready')).toBeInTheDocument());

    fireEvent.click(screen.getByTestId('assessment-regenerate'));
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          ([u]) => typeof u === 'string' && u.includes('/assessment/regenerate'),
        ),
      ).toBe(true),
    );
  });

  it('with onJumpToSeq, the event-seq chip is a button that deep-links', async () => {
    mockFetch(READY);
    const onJumpToSeq = vi.fn();
    render(<AssessmentTab code="AAAA-BBBB" sessionCompleted onJumpToSeq={onJumpToSeq} />);
    await waitFor(() => expect(screen.getByTestId('assessment-ready')).toBeInTheDocument());

    const chip = screen.getByTestId('assessment-finding-seq');
    expect(chip.tagName).toBe('BUTTON');
    fireEvent.click(chip);
    expect(onJumpToSeq).toHaveBeenCalledWith(7);
  });

  it('without onJumpToSeq the chip stays a non-interactive span', async () => {
    mockFetch(READY);
    render(<AssessmentTab code="AAAA-BBBB" sessionCompleted />);
    await waitFor(() => expect(screen.getByTestId('assessment-ready')).toBeInTheDocument());
    expect(screen.getByTestId('assessment-finding-seq').tagName).toBe('SPAN');
  });

  it('shows an error state with retry when the fetch fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: false, status: 500, json: () => Promise.resolve({}) }),
    );
    render(<AssessmentTab code="AAAA-BBBB" sessionCompleted />);
    await waitFor(() => expect(screen.getByTestId('assessment-error')).toBeInTheDocument());
    expect(screen.getByTestId('assessment-retry')).toBeInTheDocument();
  });
});
