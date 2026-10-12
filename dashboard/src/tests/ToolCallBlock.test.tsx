import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ToolCallBlock } from '../components/ToolCallBlock';
import type { EventEnvelope } from '../api/types';

// ---- Fixtures ----------------------------------------------------------------

const TS = '2026-05-15T10:00:00Z';

function makeCall(toolName: string, input: Record<string, unknown>): Extract<
  EventEnvelope,
  { type: 'tool_call' }
> {
  return {
    id: `tc-${toolName}`,
    seq: 1,
    session_code: 'AAAA-BBBB',
    timestamp: TS,
    type: 'tool_call',
    payload: { tool_call_id: 'cid-1', tool_name: toolName, input },
  };
}

function makeResult(extra: {
  output?: string;
  is_error?: boolean;
  diff?: string;
  stdout?: string;
  stderr?: string;
  exit_code?: number;
  duration_ms?: number;
  timed_out?: boolean;
}): Extract<EventEnvelope, { type: 'tool_result' }> {
  return {
    id: 'tr-1',
    seq: 2,
    session_code: 'AAAA-BBBB',
    timestamp: TS,
    type: 'tool_result',
    payload: {
      tool_call_id: 'cid-1',
      output: extra.output ?? '',
      is_error: extra.is_error ?? false,
      ...(extra.diff !== undefined ? { diff: extra.diff } : {}),
      ...(extra.stdout !== undefined ? { stdout: extra.stdout } : {}),
      ...(extra.stderr !== undefined ? { stderr: extra.stderr } : {}),
      ...(extra.exit_code !== undefined ? { exit_code: extra.exit_code } : {}),
      ...(extra.duration_ms !== undefined ? { duration_ms: extra.duration_ms } : {}),
      ...(extra.timed_out !== undefined ? { timed_out: extra.timed_out } : {}),
    },
  };
}

// ---- Header (tool_call.input) -----------------------------------------------

describe('ToolCallBlock — header per tool', () => {
  it('Read with offset/limit → `read <path> (lines start–end)`', () => {
    render(<ToolCallBlock call={makeCall('Read', { path: 'train.py', offset: 10, limit: 5 })} />);
    expect(screen.getByText(/read/)).toBeInTheDocument();
    expect(screen.getByText(/train\.py/)).toBeInTheDocument();
    expect(screen.getByText(/lines 11–15/)).toBeInTheDocument();
  });

  it('Read without offset/limit → just `read <path>`', () => {
    render(<ToolCallBlock call={makeCall('Read', { path: 'data.py' })} />);
    expect(screen.getByText(/data\.py/)).toBeInTheDocument();
    expect(screen.queryByText(/lines/)).not.toBeInTheDocument();
  });

  it('Edit → `edit <path>`, replace_all shown as a flag', () => {
    render(
      <ToolCallBlock
        call={makeCall('Edit', {
          path: 'train.py',
          old_string: 'foo',
          new_string: 'bar',
          replace_all: true,
        })}
      />,
    );
    expect(screen.getByText(/edit/)).toBeInTheDocument();
    expect(screen.getByText(/train\.py/)).toBeInTheDocument();
    expect(screen.getByText(/replace_all/)).toBeInTheDocument();
  });

  it('Write → `write <path>`', () => {
    render(<ToolCallBlock call={makeCall('Write', { path: 'new.py', content: 'x = 1' })} />);
    expect(screen.getByText(/write/)).toBeInTheDocument();
    expect(screen.getByText(/new\.py/)).toBeInTheDocument();
  });

  it('Bash → `$ <command>` with highlighting', () => {
    const { container } = render(
      <ToolCallBlock call={makeCall('Bash', { command: 'pytest -k checkpoint' })} />,
    );
    // the syntax highlighter renders tokenized spans — the text is split apart;
    // search via containerText
    expect(container.textContent).toContain('$ pytest -k checkpoint');
  });

  it('Bash with cwd ≠ "." → extra `cwd: ...` line', () => {
    render(
      <ToolCallBlock
        call={makeCall('Bash', { command: 'ls', cwd: 'subdir' })}
      />,
    );
    expect(screen.getByText(/cwd: subdir/)).toBeInTheDocument();
  });

  it('Glob → `glob <pattern>`', () => {
    render(<ToolCallBlock call={makeCall('Glob', { pattern: '**/*.py' })} />);
    expect(screen.getByText(/glob/)).toBeInTheDocument();
    expect(screen.getByText(/\*\*\/\*\.py/)).toBeInTheDocument();
  });

  it('Grep with path and glob → shows filters', () => {
    render(
      <ToolCallBlock
        call={makeCall('Grep', { pattern: 'def train', path: 'src', glob: '*.py' })}
      />,
    );
    expect(screen.getByText(/grep/)).toBeInTheDocument();
    expect(screen.getByText(/def train/)).toBeInTheDocument();
    expect(screen.getByText(/in src/)).toBeInTheDocument();
    expect(screen.getByText(/glob=\*\.py/)).toBeInTheDocument();
  });

  it('Unknown tool → fallback to input JSON', () => {
    render(<ToolCallBlock call={makeCall('Mystery', { foo: 'bar', n: 42 })} />);
    expect(screen.getByText(/"foo": "bar"/)).toBeInTheDocument();
    expect(screen.getByText(/"n": 42/)).toBeInTheDocument();
  });
});

// ---- Result rendering --------------------------------------------------------

describe('ToolCallBlock — result per tool', () => {
  it('Read result → `✓ N lines` by line count', () => {
    render(
      <ToolCallBlock
        call={makeCall('Read', { path: 'x.py' })}
        result={makeResult({ output: 'a\nb\nc\n' })}
      />,
    );
    expect(screen.getByText((_, el) => el?.textContent === '✓ 3 lines')).toBeInTheDocument();
  });

  it('Read result with empty output → `✓ 0 lines`', () => {
    render(
      <ToolCallBlock
        call={makeCall('Read', { path: 'x.py' })}
        result={makeResult({ output: '' })}
      />,
    );
    expect(screen.getByText((_, el) => el?.textContent === '✓ 0 lines')).toBeInTheDocument();
  });

  it('Edit result with backend diff → renders highlighted diff', () => {
    const diff = '--- a/train.py\n+++ b/train.py\n@@ -1 +1 @@\n-old\n+new\n';
    const { container } = render(
      <ToolCallBlock
        call={makeCall('Edit', { path: 'train.py', old_string: 'old', new_string: 'new' })}
        result={makeResult({ diff })}
      />,
    );
    // SyntaxHighlighter renders tokens; the whole diff is visible in textContent.
    expect(container.textContent).toContain('@@ -1 +1 @@');
    expect(container.textContent).toContain('-old');
    expect(container.textContent).toContain('+new');
  });

  it('Edit result without backend diff → client-side diff from old/new_string', () => {
    const { container } = render(
      <ToolCallBlock
        call={makeCall('Edit', {
          path: 'train.py',
          old_string: 'foo',
          new_string: 'bar',
        })}
        result={makeResult({ output: 'ok' })}
      />,
    );
    expect(container.textContent).toContain('-foo');
    expect(container.textContent).toContain('+bar');
  });

  it('Write result with backend diff → diff block', () => {
    const diff = '--- a/new.py\n+++ b/new.py\n@@ -0,0 +1 @@\n+x = 1\n';
    const { container } = render(
      <ToolCallBlock
        call={makeCall('Write', { path: 'new.py', content: 'x = 1' })}
        result={makeResult({ diff })}
      />,
    );
    expect(container.textContent).toContain('+x = 1');
  });

  it('Write result without diff → content highlighted by extension', () => {
    const { container } = render(
      <ToolCallBlock
        call={makeCall('Write', { path: 'new.py', content: 'x = 1\n' })}
        result={makeResult({ output: 'ok' })}
      />,
    );
    expect(container.textContent).toContain('x = 1');
  });

  it('Bash result success → exit 0, stdout, no stderr', () => {
    const { container } = render(
      <ToolCallBlock
        call={makeCall('Bash', { command: 'pytest tests/' })}
        result={makeResult({
          stdout: '12 passed in 0.5s\n',
          exit_code: 0,
          duration_ms: 42,
        })}
      />,
    );
    expect(screen.getByText(/exit 0 · 42ms/)).toBeInTheDocument();
    expect(screen.getByText(/12 passed/)).toBeInTheDocument();
    expect(screen.queryByText('stderr')).not.toBeInTheDocument();
    expect(container.querySelector("[data-status='ok']")).not.toBeNull();
  });

  it('Bash result non-zero → exit code in red, stderr section shown', () => {
    const { container } = render(
      <ToolCallBlock
        call={makeCall('Bash', { command: 'false' })}
        result={makeResult({
          stderr: 'oops\n',
          exit_code: 1,
          duration_ms: 5,
        })}
      />,
    );
    expect(screen.getByText(/exit 1/)).toBeInTheDocument();
    expect(screen.getByText('stderr')).toBeInTheDocument();
    expect(screen.getByText('oops')).toBeInTheDocument();
    const result = container.querySelector("[data-status='fail']");
    expect(result).not.toBeNull();
  });

  it('Bash result timed_out → TIMED OUT marker, fail status', () => {
    const { container } = render(
      <ToolCallBlock
        call={makeCall('Bash', { command: 'sleep 100' })}
        result={makeResult({
          stdout: '',
          exit_code: 124,
          duration_ms: 120000,
          timed_out: true,
        })}
      />,
    );
    expect(screen.getByText(/TIMED OUT/)).toBeInTheDocument();
    const result = container.querySelector("[data-status='fail']");
    expect(result).not.toBeNull();
  });

  it('Bash result without extra fields → fallback to raw output', () => {
    render(
      <ToolCallBlock
        call={makeCall('Bash', { command: 'echo hi' })}
        result={makeResult({ output: 'exit_code: 0\nstdout:\nhi' })}
      />,
    );
    expect(screen.getByText(/exit_code: 0/)).toBeInTheDocument();
  });

  it('Glob result → `✓ Glob N matches` + path list', () => {
    render(
      <ToolCallBlock
        call={makeCall('Glob', { pattern: '*.py' })}
        result={makeResult({ output: 'a.py\nb.py\nc.py\n' })}
      />,
    );
    expect(screen.getByText(/✓/)).toBeInTheDocument();
    expect(screen.getByText(/3 matches/)).toBeInTheDocument();
    expect(screen.getByText(/a\.py/)).toBeInTheDocument();
  });

  it('Glob result empty → `✓ Glob 0 matches`', () => {
    render(
      <ToolCallBlock
        call={makeCall('Glob', { pattern: 'nope/*' })}
        result={makeResult({ output: '' })}
      />,
    );
    expect(screen.getByText(/0 matches/)).toBeInTheDocument();
  });

  it('Grep result → `✓ Grep N matches`', () => {
    render(
      <ToolCallBlock
        call={makeCall('Grep', { pattern: 'def' })}
        result={makeResult({ output: 'a.py:1: def foo\nb.py:2: def bar\n' })}
      />,
    );
    expect(screen.getByText(/2 matches/)).toBeInTheDocument();
  });

  it('tool_result with is_error=true → red error panel with message', () => {
    render(
      <ToolCallBlock
        call={makeCall('Edit', { path: 'x.py', old_string: 'a', new_string: 'b' })}
        result={makeResult({ output: 'NoMatch: pattern not found', is_error: true })}
      />,
    );
    const alert = screen.getByRole('alert');
    expect(within(alert).getByText('error')).toBeInTheDocument();
    expect(within(alert).getByText(/NoMatch/)).toBeInTheDocument();
  });

  it('Without result → "waiting for result…"', () => {
    render(<ToolCallBlock call={makeCall('Read', { path: 'x.py' })} />);
    expect(screen.getByText(/waiting for result/)).toBeInTheDocument();
  });

  it('Unknown tool with output → generic "result" label + raw output', () => {
    render(
      <ToolCallBlock
        call={makeCall('Mystery', { foo: 1 })}
        result={makeResult({ output: 'whatever' })}
      />,
    );
    expect(screen.getByText('result')).toBeInTheDocument();
    expect(screen.getByText('whatever')).toBeInTheDocument();
  });

  it('Renders tool avatar (data-role="tool", size="sm")', () => {
    render(<ToolCallBlock call={makeCall('Read', { path: 'x.py' })} />);
    const avatar = screen.getByRole('img', { name: 'tool' });
    expect(avatar).toHaveAttribute('data-role', 'tool');
    expect(avatar).toHaveAttribute('data-size', 'sm');
  });
});

// ---- Collapse heavy output ---------------------------------------

describe('ToolCallBlock — collapse heavy output', () => {
  // Read result -----------------------------------------------------------------

  it('Read with non-empty output → collapsed by default, preview pre hidden', () => {
    const { container } = render(
      <ToolCallBlock
        call={makeCall('Read', { path: 'x.py' })}
        result={makeResult({ output: 'line1\nline2\nline3\n' })}
      />,
    );
    const wrapper = screen.getByTestId('tool-result-read');
    expect(wrapper).toHaveAttribute('data-expanded', 'false');
    const toggle = within(wrapper).getByRole('button');
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(container.textContent).not.toContain('line1');
  });

  it('Read collapsed → clicking the header expands the preview with content', () => {
    render(
      <ToolCallBlock
        call={makeCall('Read', { path: 'x.py' })}
        result={makeResult({ output: 'line1\nline2\nline3\n' })}
      />,
    );
    const wrapper = screen.getByTestId('tool-result-read');
    fireEvent.click(within(wrapper).getByRole('button'));
    expect(wrapper).toHaveAttribute('data-expanded', 'true');
    expect(screen.getByText(/line1[\s\S]*line2[\s\S]*line3/)).toBeInTheDocument();
  });

  it('Read with isLatest=true → expanded by default (preview visible immediately)', () => {
    const { container } = render(
      <ToolCallBlock
        call={makeCall('Read', { path: 'x.py' })}
        result={makeResult({ output: 'line1\nline2\n' })}
        isLatest
      />,
    );
    const wrapper = screen.getByTestId('tool-result-read');
    expect(wrapper).toHaveAttribute('data-expanded', 'true');
    expect(container.textContent).toContain('line1');
  });

  it('Read with empty output → no toggle (collapsible does not wrap)', () => {
    render(
      <ToolCallBlock
        call={makeCall('Read', { path: 'x.py' })}
        result={makeResult({ output: '' })}
      />,
    );
    expect(screen.queryByTestId('tool-result-read')).toBeNull();
  });

  // Edit/Write diff -------------------------------------------------------------

  function makeBigDiff(lines: number): string {
    const header = '--- a/big.py\n+++ b/big.py\n@@ -1,1 +1,N @@\n';
    const body = Array.from({ length: lines }, (_, i) => `+line ${i + 1}`).join('\n') + '\n';
    return header + body;
  }

  it('Edit with a large diff (>20 lines) → collapsed by default, diff hidden', () => {
    const { container } = render(
      <ToolCallBlock
        call={makeCall('Edit', { path: 'big.py' })}
        result={makeResult({ diff: makeBigDiff(25) })}
      />,
    );
    const wrapper = screen.getByTestId('tool-result-diff');
    expect(wrapper).toHaveAttribute('data-expanded', 'false');
    expect(screen.getByText(/diff · 28 lines/)).toBeInTheDocument(); // 3 header + 25 body
    expect(container.textContent).not.toContain('+line 1');
  });

  it('Edit with a large diff + isLatest=true → expanded directly (no toggle wrapper)', () => {
    const { container } = render(
      <ToolCallBlock
        call={makeCall('Edit', { path: 'big.py' })}
        result={makeResult({ diff: makeBigDiff(25) })}
        isLatest
      />,
    );
    expect(screen.queryByTestId('tool-result-diff')).toBeNull();
    expect(container.textContent).toContain('+line 1');
  });

  it('Edit with a small diff (≤20 lines) → diff visible without collapse', () => {
    const diff = '--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n';
    const { container } = render(
      <ToolCallBlock
        call={makeCall('Edit', { path: 'x.py' })}
        result={makeResult({ diff })}
      />,
    );
    expect(screen.queryByTestId('tool-result-diff')).toBeNull();
    expect(container.textContent).toContain('-old');
    expect(container.textContent).toContain('+new');
  });

  it('Edit collapsed → click expands the diff', () => {
    const { container } = render(
      <ToolCallBlock
        call={makeCall('Edit', { path: 'big.py' })}
        result={makeResult({ diff: makeBigDiff(25) })}
      />,
    );
    const wrapper = screen.getByTestId('tool-result-diff');
    fireEvent.click(within(wrapper).getByRole('button'));
    expect(wrapper).toHaveAttribute('data-expanded', 'true');
    expect(container.textContent).toContain('+line 1');
  });

  // Bash stdout -----------------------------------------------------------------

  function bigStdout(lines: number): string {
    return Array.from({ length: lines }, (_, i) => `row ${i + 1}`).join('\n') + '\n';
  }

  it('Bash with stdout >30 lines → fade-out preview + "expand" button', () => {
    render(
      <ToolCallBlock
        call={makeCall('Bash', { command: 'pytest -v' })}
        result={makeResult({
          stdout: bigStdout(50),
          exit_code: 0,
          duration_ms: 100,
        })}
      />,
    );
    const expandBtn = screen.getByTestId('bash-stdout-expand');
    expect(expandBtn).toHaveTextContent(/expand/i);
    expect(expandBtn).toHaveTextContent('50 lines');
  });

  it('Bash stdout >30 + isLatest=true → no fade and no button', () => {
    render(
      <ToolCallBlock
        call={makeCall('Bash', { command: 'pytest -v' })}
        result={makeResult({
          stdout: bigStdout(50),
          exit_code: 0,
          duration_ms: 100,
        })}
        isLatest
      />,
    );
    expect(screen.queryByTestId('bash-stdout-expand')).toBeNull();
  });

  it('Bash collapsed stdout → clicking the button expands', () => {
    render(
      <ToolCallBlock
        call={makeCall('Bash', { command: 'pytest -v' })}
        result={makeResult({
          stdout: bigStdout(50),
          exit_code: 0,
          duration_ms: 100,
        })}
      />,
    );
    fireEvent.click(screen.getByTestId('bash-stdout-expand'));
    expect(screen.queryByTestId('bash-stdout-expand')).toBeNull();
  });

  it('Bash stdout ≤30 lines → no fade, plain pre', () => {
    render(
      <ToolCallBlock
        call={makeCall('Bash', { command: 'ls' })}
        result={makeResult({
          stdout: bigStdout(10),
          exit_code: 0,
          duration_ms: 5,
        })}
      />,
    );
    expect(screen.queryByTestId('bash-stdout-expand')).toBeNull();
  });

  // Bash stderr — same rules as stdout ----------------

  it('Bash with stderr >30 lines → fade + "expand" button (stderr variant)', () => {
    render(
      <ToolCallBlock
        call={makeCall('Bash', { command: 'python build.py' })}
        result={makeResult({
          stderr: bigStdout(40),
          exit_code: 1,
          duration_ms: 200,
        })}
      />,
    );
    const btn = screen.getByTestId('bash-stderr-expand');
    expect(btn).toHaveAttribute('data-variant', 'stderr');
    expect(btn).toHaveTextContent('40 lines');
    // The section label stays visible.
    expect(screen.getByText('stderr')).toBeInTheDocument();
  });

  it('Bash stderr >30 + isLatest=true → no fade and no button', () => {
    render(
      <ToolCallBlock
        call={makeCall('Bash', { command: 'python build.py' })}
        result={makeResult({
          stderr: bigStdout(40),
          exit_code: 1,
          duration_ms: 200,
        })}
        isLatest
      />,
    );
    expect(screen.queryByTestId('bash-stderr-expand')).toBeNull();
    expect(screen.getByText('stderr')).toBeInTheDocument();
  });

  it('Bash stderr ≤30 lines → no fade (short stack as-is)', () => {
    render(
      <ToolCallBlock
        call={makeCall('Bash', { command: 'false' })}
        result={makeResult({ stderr: 'oops\n', exit_code: 1, duration_ms: 5 })}
      />,
    );
    expect(screen.queryByTestId('bash-stderr-expand')).toBeNull();
    expect(screen.getByText('oops')).toBeInTheDocument();
  });

  it('Bash collapsed stderr → clicking the button expands', () => {
    render(
      <ToolCallBlock
        call={makeCall('Bash', { command: 'python build.py' })}
        result={makeResult({
          stderr: bigStdout(40),
          exit_code: 1,
          duration_ms: 200,
        })}
      />,
    );
    fireEvent.click(screen.getByTestId('bash-stderr-expand'));
    expect(screen.queryByTestId('bash-stderr-expand')).toBeNull();
  });
});
