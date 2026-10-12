import type { EventEnvelope, FileChangedPayload } from '../api/types';

export interface FileChangeEntry {
  eventId: string;
  seq: number;
  timestamp: string;
  source: FileChangedPayload['source'];
  // null when backend does not compute a diff (sandbox Write/Edit).
  diff: string | null;
  // undefined — field omitted (binary / old CLI);
  // null — file deleted;
  // string — UTF-8 content after the change.
  content: string | null | undefined;
}

export interface FileLog {
  path: string;
  entries: FileChangeEntry[]; // in ascending seq order
}

export type TreeNode =
  | { kind: 'dir'; name: string; path: string; children: TreeNode[] }
  // log === null means the file is in the workdir tree but has no file_changed events
  | { kind: 'file'; name: string; path: string; log: FileLog | null };

export function aggregateFileLogs(events: EventEnvelope[]): Map<string, FileLog> {
  const map = new Map<string, FileLog>();
  for (const e of events) {
    if (e.type !== 'file_changed') continue;
    const path = e.payload.file_path;
    let log = map.get(path);
    if (!log) {
      log = { path, entries: [] };
      map.set(path, log);
    }
    log.entries.push({
      eventId: e.id,
      seq: e.seq,
      timestamp: e.timestamp,
      source: e.payload.source,
      diff: e.payload.diff,
      content: e.payload.content,
    });
  }
  for (const log of map.values()) {
    log.entries.sort((a, b) => a.seq - b.seq);
  }
  return map;
}

export function sortByLatestSeq(logs: Map<string, FileLog>): FileLog[] {
  const arr = Array.from(logs.values());
  arr.sort((a, b) => {
    const aSeq = a.entries[a.entries.length - 1]?.seq ?? 0;
    const bSeq = b.entries[b.entries.length - 1]?.seq ?? 0;
    return bSeq - aSeq;
  });
  return arr;
}

type DirNode = Extract<TreeNode, { kind: 'dir' }>;

/**
 * Ensure a directory node (and all its ancestors) exists; return the leaf dir.
 * An empty `path` returns the root unchanged.
 */
function ensureDir(path: string, root: DirNode, dirIndex: Map<string, DirNode>): DirNode {
  const segments = path.split('/').filter((s) => s.length > 0);
  let parent: DirNode = root;
  let accum = '';
  for (const seg of segments) {
    accum = accum === '' ? seg : `${accum}/${seg}`;
    let dir = dirIndex.get(accum);
    if (!dir) {
      dir = { kind: 'dir', name: seg, path: accum, children: [] };
      dirIndex.set(accum, dir);
      parent.children.push(dir);
    }
    parent = dir;
  }
  return parent;
}

/**
 * Build a file tree.
 *
 * @param logs      - file_changed event logs (modified files)
 * @param allPaths  - all file paths from REST /files; when provided, unmodified
 *                    files (not in logs) are included as tree nodes with log === null.
 *                    When omitted, only files that have logs are shown (legacy behaviour).
 * @param dirPaths  - empty-directory paths from REST /files. Non-empty dirs are implied
 *                    by file paths; these surface dirs that hold no files yet (a fresh
 *                    `mkdir`) so they render in the tree.
 */
export function buildTree(
  logs: Map<string, FileLog>,
  allPaths?: readonly string[],
  dirPaths?: readonly string[],
): TreeNode {
  const root: DirNode = { kind: 'dir', name: '', path: '', children: [] };
  const dirIndex = new Map<string, DirNode>([['', root]]);

  // Union of REST paths and logs keys, deduplicated and sorted.
  const pathSet = new Set<string>();
  if (allPaths) {
    for (const p of allPaths) pathSet.add(p);
  }
  for (const k of logs.keys()) pathSet.add(k);
  const paths = [...pathSet].sort();

  for (const fullPath of paths) {
    const segments = fullPath.split('/').filter((s) => s.length > 0);
    if (segments.length === 0) continue;
    const parentPath = segments.slice(0, -1).join('/');
    const parent = ensureDir(parentPath, root, dirIndex);
    const fileSeg = segments[segments.length - 1]!;
    // log is null for REST-only files that have no file_changed events
    const log = logs.get(fullPath) ?? null;
    parent.children.push({
      kind: 'file',
      name: fileSeg,
      path: fullPath,
      log,
    });
  }

  if (dirPaths) {
    for (const d of dirPaths) ensureDir(d, root, dirIndex);
  }

  sortChildren(root);
  return root;
}

function sortChildren(node: TreeNode): void {
  if (node.kind !== 'dir') return;
  node.children.sort((a, b) => {
    if (a.kind !== b.kind) return a.kind === 'dir' ? -1 : 1;
    return a.name.localeCompare(b.name);
  });
  for (const c of node.children) sortChildren(c);
}

