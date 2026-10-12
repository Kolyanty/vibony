import { describe, expect, it } from 'vitest';
import { buildTree, type FileLog, type TreeNode } from '../components/filesTabUtils';

const noLogs = new Map<string, FileLog>();

function childNames(node: TreeNode): string[] {
  return node.kind === 'dir' ? node.children.map((c) => c.name) : [];
}

function find(node: TreeNode, path: string): TreeNode | undefined {
  if (node.path === path) return node;
  if (node.kind !== 'dir') return undefined;
  for (const c of node.children) {
    const hit = find(c, path);
    if (hit) return hit;
  }
  return undefined;
}

describe('buildTree', () => {
  it('builds nested dirs from file paths, dirs sorted before files', () => {
    const tree = buildTree(noLogs, ['src/main.ts', 'src/lib/util.ts', 'README.md']);
    expect(childNames(tree)).toEqual(['src', 'README.md']);
    const src = find(tree, 'src');
    expect(src?.kind).toBe('dir');
    expect(childNames(src!)).toEqual(['lib', 'main.ts']);
  });

  it('surfaces empty directories passed via dirPaths', () => {
    const tree = buildTree(noLogs, ['main.py'], ['empty']);
    const empty = find(tree, 'empty');
    expect(empty?.kind).toBe('dir');
    expect(childNames(empty!)).toEqual([]);
  });

  it('creates a nested empty dir with all its ancestors', () => {
    const tree = buildTree(noLogs, undefined, ['a/b/c']);
    expect(find(tree, 'a')?.kind).toBe('dir');
    expect(find(tree, 'a/b')?.kind).toBe('dir');
    const leaf = find(tree, 'a/b/c');
    expect(leaf?.kind).toBe('dir');
    expect(childNames(leaf!)).toEqual([]);
  });

  it('does not duplicate a dir that already exists from a file path', () => {
    const tree = buildTree(noLogs, ['pkg/mod.py'], ['pkg']);
    const root = tree as Extract<TreeNode, { kind: 'dir' }>;
    const pkgNodes = root.children.filter((c) => c.name === 'pkg');
    expect(pkgNodes).toHaveLength(1);
    expect(childNames(pkgNodes[0]!)).toEqual(['mod.py']);
  });
});
