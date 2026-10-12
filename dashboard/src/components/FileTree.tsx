import {
  ChevronRight,
  File,
  FileCode,
  FileCog,
  FileJson,
  FileText,
  Folder,
  FolderOpen,
  type LucideIcon,
} from 'lucide-react';
import type { TreeNode } from './filesTabUtils';
import styles from './FileTree.module.css';

export type { TreeNode };

// Lightweight extension → icon map. Unknown extensions fall back to a plain file.
const ICON_BY_EXT: Record<string, LucideIcon> = {
  py: FileCode,
  ts: FileCode,
  tsx: FileCode,
  js: FileCode,
  jsx: FileCode,
  mjs: FileCode,
  go: FileCode,
  rs: FileCode,
  java: FileCode,
  kt: FileCode,
  rb: FileCode,
  php: FileCode,
  c: FileCode,
  h: FileCode,
  cpp: FileCode,
  cs: FileCode,
  sh: FileCode,
  sql: FileCode,
  json: FileJson,
  md: FileText,
  txt: FileText,
  rst: FileText,
  yaml: FileCog,
  yml: FileCog,
  toml: FileCog,
  ini: FileCog,
  cfg: FileCog,
  env: FileCog,
};

function fileIconFor(name: string): LucideIcon {
  const dot = name.lastIndexOf('.');
  const ext = dot >= 0 ? name.slice(dot + 1).toLowerCase() : '';
  return ICON_BY_EXT[ext] ?? File;
}

interface FileTreeProps {
  tree: TreeNode;
  selectedPath: string | null;
  collapsedDirs: Set<string>;
  onSelect: (path: string) => void;
  onToggleDir: (path: string) => void;
  modifiedPaths?: Set<string>;
}

export function FileTree({
  tree,
  selectedPath,
  collapsedDirs,
  onSelect,
  onToggleDir,
  modifiedPaths,
}: FileTreeProps) {
  if (tree.kind !== 'dir') return null;
  return (
    <ul className={styles.root} role="tree">
      {tree.children.map((child) => (
        <TreeNodeRow
          key={`${child.kind}:${child.path}`}
          node={child}
          depth={0}
          selectedPath={selectedPath}
          collapsedDirs={collapsedDirs}
          onSelect={onSelect}
          onToggleDir={onToggleDir}
          modifiedPaths={modifiedPaths}
        />
      ))}
    </ul>
  );
}

interface RowProps {
  node: TreeNode;
  depth: number;
  selectedPath: string | null;
  collapsedDirs: Set<string>;
  onSelect: (path: string) => void;
  onToggleDir: (path: string) => void;
  modifiedPaths?: Set<string>;
}

function TreeNodeRow({
  node,
  depth,
  selectedPath,
  collapsedDirs,
  onSelect,
  onToggleDir,
  modifiedPaths,
}: RowProps) {
  if (node.kind === 'dir') {
    const collapsed = collapsedDirs.has(node.path);
    const FolderIcon = collapsed ? Folder : FolderOpen;
    return (
      <li role="treeitem" aria-expanded={!collapsed} aria-level={depth + 1}>
        <button
          type="button"
          className={styles.dirRow}
          onClick={() => onToggleDir(node.path)}
          data-testid={`tree-dir-${node.path}`}
        >
          <ChevronRight
            className={styles.chevron}
            data-open={!collapsed}
            size={13}
            aria-hidden
          />
          <FolderIcon className={styles.dirIcon} size={15} aria-hidden />
          <span className={styles.dirName}>{node.name}</span>
        </button>
        {!collapsed && node.children.length > 0 ? (
          <ul className={styles.nested} role="group">
            {node.children.map((c) => (
              <TreeNodeRow
                key={`${c.kind}:${c.path}`}
                node={c}
                depth={depth + 1}
                selectedPath={selectedPath}
                collapsedDirs={collapsedDirs}
                onSelect={onSelect}
                onToggleDir={onToggleDir}
                modifiedPaths={modifiedPaths}
              />
            ))}
          </ul>
        ) : null}
      </li>
    );
  }

  const active = selectedPath === node.path;
  // log is null for REST-only files with no file_changed events
  const lastEntry = node.log ? node.log.entries[node.log.entries.length - 1] : undefined;
  const source = lastEntry?.source;
  const isModified = modifiedPaths?.has(node.path) ?? false;
  const FileIcon = fileIconFor(node.name);
  return (
    <li role="treeitem" aria-selected={active} aria-level={depth + 1}>
      <button
        type="button"
        className={styles.fileRow}
        data-active={active}
        data-modified={isModified}
        onClick={() => onSelect(node.path)}
        data-testid={`tree-file-${node.path}`}
      >
        <FileIcon className={styles.fileIcon} size={15} aria-hidden />
        <span className={styles.fileName}>{node.name}</span>
        {isModified && (
          <span
            className={styles.modifiedDot}
            aria-label="modified"
            data-testid={`tree-modified-dot-${node.path}`}
          />
        )}
        {source ? (
          <span className={styles.sourceBadge} data-source={source}>
            {source}
          </span>
        ) : null}
      </button>
    </li>
  );
}
