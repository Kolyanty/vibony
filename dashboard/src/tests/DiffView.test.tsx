import { fireEvent, render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { DiffView } from '../components/DiffView';
import { parseDiff } from '../components/diffViewUtils';

// ---- parseDiff ---------------------------------------------------------------

describe('parseDiff', () => {
  it('extracts file headers and hunks', () => {
    const text = '--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-foo\n+bar\n';
    const parsed = parseDiff(text);
    expect(parsed.fileHeaders).toEqual(['--- a/x.py', '+++ b/x.py']);
    expect(parsed.hunks).toHaveLength(1);
    expect(parsed.hunks[0].lines).toHaveLength(2);
    expect(parsed.changedCount).toBe(2);
  });

  it('hunk header with count `,N` where N is a non-numeric placeholder is parsed leniently', () => {
    const text = '@@ -1,1 +1,N @@\n+line a\n+line b\n';
    const parsed = parseDiff(text);
    expect(parsed.hunks).toHaveLength(1);
    expect(parsed.hunks[0].lines).toHaveLength(2);
    expect(parsed.changedCount).toBe(2);
  });

  it('tracks old and new line numbers in a hunk', () => {
    const text = '@@ -10,3 +20,3 @@\n line a\n-old\n+new\n line b\n';
    const parsed = parseDiff(text);
    const lines = parsed.hunks[0].lines;
    expect(lines[0]).toMatchObject({ kind: 'context', oldNo: 10, newNo: 20 });
    expect(lines[1]).toMatchObject({ kind: 'remove', oldNo: 11, newNo: null });
    expect(lines[2]).toMatchObject({ kind: 'add', oldNo: null, newNo: 21 });
    expect(lines[3]).toMatchObject({ kind: 'context', oldNo: 12, newNo: 22 });
  });

  it('counts changedCount excluding context lines', () => {
    const text = '@@ -1,3 +1,3 @@\n a\n-b\n+c\n d\n';
    const parsed = parseDiff(text);
    expect(parsed.changedCount).toBe(2);
  });

  it('skips the "\\\\ No newline at end of file" marker', () => {
    const text =
      '@@ -1 +1 @@\n-foo\n\\ No newline at end of file\n+bar\n\\ No newline at end of file\n';
    const parsed = parseDiff(text);
    expect(parsed.hunks[0].lines).toHaveLength(2);
  });

  it('paired remove+add get word-level segments via diffWords', () => {
    const text = '@@ -1 +1 @@\n-hello world\n+hello mars\n';
    const parsed = parseDiff(text);
    const [rm, add] = parsed.hunks[0].lines;
    expect(rm.segments).toBeDefined();
    expect(add.segments).toBeDefined();
    const rmEmph = rm.segments!.filter((s) => s.emphasis).map((s) => s.text).join('');
    const addEmph = add.segments!.filter((s) => s.emphasis).map((s) => s.text).join('');
    expect(rmEmph).toContain('world');
    expect(addEmph).toContain('mars');
    // 'hello ' is the common prefix, must not appear in the emphasis segments.
    expect(rmEmph).not.toContain('hello');
    expect(addEmph).not.toContain('hello');
  });

  it('unpaired remove with no adds → segments are not set (no pair possible)', () => {
    const text = '@@ -1,2 +1,1 @@\n-foo\n-bar\n+baz\n';
    const parsed = parseDiff(text);
    const lines = parsed.hunks[0].lines;
    // The first remove pairs with the add; the second remove stays unpaired.
    expect(lines[0].segments).toBeDefined();
    expect(lines[1].segments).toBeUndefined();
    expect(lines[2].segments).toBeDefined();
  });

  it('preserves multiple hunks', () => {
    const text = '@@ -1 +1 @@\n-a\n+b\n@@ -10 +10 @@\n-c\n+d\n';
    const parsed = parseDiff(text);
    expect(parsed.hunks).toHaveLength(2);
    expect(parsed.changedCount).toBe(4);
  });
});

// ---- Mode selection ----------------------------------------------------------

describe('DiffView — mode', () => {
  it('≤5 changed lines → unified', () => {
    const text = '@@ -1,3 +1,3 @@\n-a\n+b\n-c\n+d\n';
    const { container } = render(<DiffView diffText={text} />);
    expect(container.querySelector('[data-mode="unified"]')).not.toBeNull();
  });

  it('>5 changed lines → still unified (auto no longer switches to SBS)', () => {
    const adds = Array.from({ length: 6 }, (_, i) => `+line${i + 1}`).join('\n');
    const text = `@@ -0,0 +1,6 @@\n${adds}\n`;
    const { container } = render(<DiffView diffText={text} />);
    expect(container.querySelector('[data-mode="unified"]')).not.toBeNull();
  });

  it('mode="side-by-side" overrides the auto choice for short diffs', () => {
    const text = '@@ -1 +1 @@\n-a\n+b\n';
    const { container } = render(<DiffView diffText={text} mode="side-by-side" />);
    expect(container.querySelector('[data-mode="side-by-side"]')).not.toBeNull();
  });

  it('mode="unified" overrides the auto choice for long diffs', () => {
    const adds = Array.from({ length: 10 }, (_, i) => `+l${i}`).join('\n');
    const text = `@@ -0,0 +1,10 @@\n${adds}\n`;
    const { container } = render(<DiffView diffText={text} mode="unified" />);
    expect(container.querySelector('[data-mode="unified"]')).not.toBeNull();
  });
});

// ---- Rendering ---------------------------------------------------------------

describe('DiffView — render', () => {
  it('unified: the +/- prefix is preserved in textContent', () => {
    const text = '@@ -1 +1 @@\n-old\n+new\n';
    const { container } = render(<DiffView diffText={text} />);
    expect(container.textContent).toContain('-old');
    expect(container.textContent).toContain('+new');
  });

  it('unified: hunk header is visible', () => {
    const text = '@@ -1 +1 @@\n-x\n+y\n';
    const { container } = render(<DiffView diffText={text} />);
    expect(container.textContent).toContain('@@ -1 +1 @@');
  });

  it('unified: file headers render in a separate section', () => {
    const text = '--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n';
    const { container } = render(<DiffView diffText={text} />);
    expect(container.textContent).toContain('--- a/x.py');
    expect(container.textContent).toContain('+++ b/x.py');
  });

  it('unified: data-kind is set on add/remove/context lines', () => {
    const text = '@@ -1,3 +1,3 @@\n line a\n-old\n+new\n line b\n';
    const { container } = render(<DiffView diffText={text} />);
    expect(container.querySelectorAll('[data-kind="add"]').length).toBeGreaterThanOrEqual(1);
    expect(container.querySelectorAll('[data-kind="remove"]').length).toBeGreaterThanOrEqual(1);
    expect(container.querySelectorAll('[data-kind="context"]').length).toBeGreaterThanOrEqual(1);
  });

  it('word-level emphasis renders as spans with data-emphasis="true"', () => {
    const text = '@@ -1 +1 @@\n-hello world\n+hello mars\n';
    const { container } = render(<DiffView diffText={text} />);
    const emphases = container.querySelectorAll('[data-emphasis="true"]');
    expect(emphases.length).toBeGreaterThan(0);
    const emphasisText = Array.from(emphases).map((el) => el.textContent).join('|');
    expect(emphasisText).toContain('world');
    expect(emphasisText).toContain('mars');
  });

  it('side-by-side: empty cell for an unpaired remove (right side)', () => {
    const removes = Array.from({ length: 6 }, (_, i) => `-row${i + 1}`).join('\n');
    const text = `@@ -1,6 +0,0 @@\n${removes}\n`;
    const { container } = render(<DiffView diffText={text} mode="side-by-side" />);
    const emptyRight = container.querySelectorAll('[data-kind="empty"][data-side="right"]');
    expect(emptyRight.length).toBe(6);
  });

  it('side-by-side: empty cell for an unpaired add (left side)', () => {
    const adds = Array.from({ length: 6 }, (_, i) => `+row${i + 1}`).join('\n');
    const text = `@@ -0,0 +1,6 @@\n${adds}\n`;
    const { container } = render(<DiffView diffText={text} mode="side-by-side" />);
    const emptyLeft = container.querySelectorAll('[data-kind="empty"][data-side="left"]');
    expect(emptyLeft.length).toBe(6);
  });

  it('side-by-side: a context line occupies both columns with the same text', () => {
    const text =
      '@@ -1,8 +1,8 @@\n ctx top\n-a\n-b\n-c\n+d\n+e\n+f\n-g\n+h\n ctx bot\n';
    const { container } = render(<DiffView diffText={text} mode="side-by-side" />);
    const ctxLeft = container.querySelectorAll(
      '[data-kind="context"][data-side="left"]',
    );
    const ctxRight = container.querySelectorAll(
      '[data-kind="context"][data-side="right"]',
    );
    expect(ctxLeft.length).toBe(2);
    expect(ctxRight.length).toBe(2);
    expect(ctxLeft[0].textContent).toContain('ctx top');
    expect(ctxRight[0].textContent).toContain('ctx top');
  });

  it('line numbers are visible in the gutter', () => {
    const text = '@@ -5 +5 @@\n-old\n+new\n';
    const { container } = render(<DiffView diffText={text} />);
    expect(container.textContent).toContain('5');
  });
});

// ---- Empty / no-changes ----------------------------------------------

describe('DiffView — empty/identical diff', () => {
  it('empty diffText → "no changes" placeholder, no unified/SBS render', () => {
    const { getByTestId, queryByTestId } = render(<DiffView diffText="" />);
    expect(getByTestId('diff-empty')).toBeTruthy();
    // the toolbar is still present, but the diff body is absent
    expect(queryByTestId('diff-mode-toggle')).toBeTruthy();
  });

  it('hunk with only context lines (changedCount=0) → placeholder', () => {
    const text = '@@ -1,2 +1,2 @@\n unchanged a\n unchanged b\n';
    const { getByTestId } = render(<DiffView diffText={text} />);
    expect(getByTestId('diff-empty')).toBeTruthy();
  });

  it('diff with changes → no placeholder', () => {
    const text = '@@ -1 +1 @@\n-a\n+b\n';
    const { queryByTestId } = render(<DiffView diffText={text} />);
    expect(queryByTestId('diff-empty')).toBeNull();
  });
});

// ---- Toggle ------------------------------------------------------------------

describe('DiffView — toggle', () => {
  const simpleDiff = '@@ -1 +1 @@\n-a\n+b\n';

  it('toolbar with toggle is visible, both buttons present', () => {
    const { getByTestId } = render(<DiffView diffText={simpleDiff} />);
    expect(getByTestId('diff-mode-toggle')).toBeTruthy();
    expect(getByTestId('diff-mode-unified')).toBeTruthy();
    expect(getByTestId('diff-mode-sbs')).toBeTruthy();
  });

  it('unified is active by default (auto)', () => {
    const { getByTestId, container } = render(<DiffView diffText={simpleDiff} />);
    expect(getByTestId('diff-mode-unified').getAttribute('data-active')).toBe('true');
    expect(getByTestId('diff-mode-sbs').getAttribute('data-active')).toBe('false');
    expect(container.querySelector('[data-mode="unified"]')).not.toBeNull();
  });

  it('clicking SBS switches data-mode to side-by-side', () => {
    const { getByTestId, container } = render(<DiffView diffText={simpleDiff} />);
    fireEvent.click(getByTestId('diff-mode-sbs'));
    expect(container.querySelector('[data-mode="side-by-side"]')).not.toBeNull();
    expect(getByTestId('diff-mode-sbs').getAttribute('data-active')).toBe('true');
    expect(getByTestId('diff-mode-unified').getAttribute('data-active')).toBe('false');
  });

  it('clicking unified switches back to unified', () => {
    const { getByTestId, container } = render(<DiffView diffText={simpleDiff} />);
    fireEvent.click(getByTestId('diff-mode-sbs'));
    fireEvent.click(getByTestId('diff-mode-unified'));
    expect(container.querySelector('[data-mode="unified"]')).not.toBeNull();
    expect(getByTestId('diff-mode-unified').getAttribute('data-active')).toBe('true');
  });

  it('mode="side-by-side" as initial — toggle switches to unified', () => {
    const { getByTestId, container } = render(
      <DiffView diffText={simpleDiff} mode="side-by-side" />,
    );
    expect(container.querySelector('[data-mode="side-by-side"]')).not.toBeNull();
    fireEvent.click(getByTestId('diff-mode-unified'));
    expect(container.querySelector('[data-mode="unified"]')).not.toBeNull();
  });
});
