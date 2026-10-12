import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { FileTree } from '../components/FileTree';
import type { TreeNode, FileLog, FileChangeEntry } from '../components/filesTabUtils';

function makeEntry(overrides: Partial<FileChangeEntry> = {}): FileChangeEntry {
  return {
    eventId: 'e1',
    seq: 1,
    timestamp: '2026-05-15T10:00:00Z',
    source: 'agent',
    diff: '',
    content: 'x',
    ...overrides,
  };
}

function makeLog(path: string, source: 'agent' | 'manual' = 'agent'): FileLog {
  return { path, entries: [makeEntry({ source })] };
}

function dir(name: string, path: string, children: TreeNode[]): TreeNode {
  return { kind: 'dir', name, path, children };
}

function file(name: string, path: string, source: 'agent' | 'manual' = 'agent'): TreeNode {
  return { kind: 'file', name, path, log: makeLog(path, source) };
}

const root = (children: TreeNode[]): TreeNode =>
  ({ kind: 'dir', name: '', path: '', children });

describe('FileTree', () => {
  it('renders flat list of files', () => {
    const tree = root([file('a.py', 'a.py'), file('b.ts', 'b.ts')]);
    render(
      <FileTree
        tree={tree}
        selectedPath={null}
        collapsedDirs={new Set()}
        onSelect={vi.fn()}
        onToggleDir={vi.fn()}
      />,
    );
    expect(screen.getByTestId('tree-file-a.py')).toBeInTheDocument();
    expect(screen.getByTestId('tree-file-b.ts')).toBeInTheDocument();
  });

  it('renders nested directories', () => {
    const tree = root([
      dir('src', 'src', [
        dir('lib', 'src/lib', [file('util.ts', 'src/lib/util.ts')]),
        file('main.ts', 'src/main.ts'),
      ]),
    ]);
    render(
      <FileTree
        tree={tree}
        selectedPath={null}
        collapsedDirs={new Set()}
        onSelect={vi.fn()}
        onToggleDir={vi.fn()}
      />,
    );
    expect(screen.getByTestId('tree-dir-src')).toBeInTheDocument();
    expect(screen.getByTestId('tree-dir-src/lib')).toBeInTheDocument();
    expect(screen.getByTestId('tree-file-src/lib/util.ts')).toBeInTheDocument();
    expect(screen.getByTestId('tree-file-src/main.ts')).toBeInTheDocument();
  });

  it('hides children of collapsed directories', () => {
    const tree = root([
      dir('src', 'src', [file('main.ts', 'src/main.ts')]),
    ]);
    render(
      <FileTree
        tree={tree}
        selectedPath={null}
        collapsedDirs={new Set(['src'])}
        onSelect={vi.fn()}
        onToggleDir={vi.fn()}
      />,
    );
    expect(screen.getByTestId('tree-dir-src')).toBeInTheDocument();
    expect(screen.queryByTestId('tree-file-src/main.ts')).not.toBeInTheDocument();
  });

  it('calls onSelect when a file is clicked', () => {
    const onSelect = vi.fn();
    const tree = root([file('a.py', 'a.py')]);
    render(
      <FileTree
        tree={tree}
        selectedPath={null}
        collapsedDirs={new Set()}
        onSelect={onSelect}
        onToggleDir={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByTestId('tree-file-a.py'));
    expect(onSelect).toHaveBeenCalledWith('a.py');
  });

  it('calls onToggleDir when a directory is clicked', () => {
    const onToggleDir = vi.fn();
    const tree = root([
      dir('src', 'src', [file('main.ts', 'src/main.ts')]),
    ]);
    render(
      <FileTree
        tree={tree}
        selectedPath={null}
        collapsedDirs={new Set()}
        onSelect={vi.fn()}
        onToggleDir={onToggleDir}
      />,
    );
    fireEvent.click(screen.getByTestId('tree-dir-src'));
    expect(onToggleDir).toHaveBeenCalledWith('src');
  });

  it('marks active file row', () => {
    const tree = root([file('a.py', 'a.py'), file('b.ts', 'b.ts')]);
    render(
      <FileTree
        tree={tree}
        selectedPath="b.ts"
        collapsedDirs={new Set()}
        onSelect={vi.fn()}
        onToggleDir={vi.fn()}
      />,
    );
    expect(screen.getByTestId('tree-file-a.py')).toHaveAttribute('data-active', 'false');
    expect(screen.getByTestId('tree-file-b.ts')).toHaveAttribute('data-active', 'true');
  });

  it('renders source badge from last entry', () => {
    const tree = root([
      file('a.py', 'a.py', 'agent'),
      file('b.ts', 'b.ts', 'manual'),
    ]);
    render(
      <FileTree
        tree={tree}
        selectedPath={null}
        collapsedDirs={new Set()}
        onSelect={vi.fn()}
        onToggleDir={vi.fn()}
      />,
    );
    const aBadge = screen.getByTestId('tree-file-a.py').querySelector('[data-source]');
    const bBadge = screen.getByTestId('tree-file-b.ts').querySelector('[data-source]');
    expect(aBadge?.getAttribute('data-source')).toBe('agent');
    expect(bBadge?.getAttribute('data-source')).toBe('manual');
  });

  it('shows modified dot for files in modifiedPaths', () => {
    const tree = root([file('a.py', 'a.py'), file('b.ts', 'b.ts')]);
    render(
      <FileTree
        tree={tree}
        selectedPath={null}
        collapsedDirs={new Set()}
        onSelect={vi.fn()}
        onToggleDir={vi.fn()}
        modifiedPaths={new Set(['a.py'])}
      />,
    );
    expect(screen.getByTestId('tree-modified-dot-a.py')).toBeInTheDocument();
    expect(screen.queryByTestId('tree-modified-dot-b.ts')).not.toBeInTheDocument();
  });

  it('does not show modified dot when modifiedPaths is not provided', () => {
    const tree = root([file('a.py', 'a.py')]);
    render(
      <FileTree
        tree={tree}
        selectedPath={null}
        collapsedDirs={new Set()}
        onSelect={vi.fn()}
        onToggleDir={vi.fn()}
      />,
    );
    expect(screen.queryByTestId('tree-modified-dot-a.py')).not.toBeInTheDocument();
  });

  it('removes modified dot when file path is absent from modifiedPaths', () => {
    const tree = root([file('a.py', 'a.py'), file('b.ts', 'b.ts')]);
    render(
      <FileTree
        tree={tree}
        selectedPath={null}
        collapsedDirs={new Set()}
        onSelect={vi.fn()}
        onToggleDir={vi.fn()}
        modifiedPaths={new Set(['b.ts'])}
      />,
    );
    expect(screen.queryByTestId('tree-modified-dot-a.py')).not.toBeInTheDocument();
    expect(screen.getByTestId('tree-modified-dot-b.ts')).toBeInTheDocument();
  });

  it('sets data-modified attribute on modified file row', () => {
    const tree = root([file('a.py', 'a.py')]);
    render(
      <FileTree
        tree={tree}
        selectedPath={null}
        collapsedDirs={new Set()}
        onSelect={vi.fn()}
        onToggleDir={vi.fn()}
        modifiedPaths={new Set(['a.py'])}
      />,
    );
    expect(screen.getByTestId('tree-file-a.py')).toHaveAttribute('data-modified', 'true');
  });
});
