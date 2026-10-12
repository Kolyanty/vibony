import { diffWords } from 'diff';

export type LineKind = 'context' | 'add' | 'remove';

export interface DiffSegment {
  text: string;
  emphasis: boolean;
}

export interface DiffLine {
  kind: LineKind;
  text: string;
  oldNo: number | null;
  newNo: number | null;
  segments?: DiffSegment[];
}

export interface DiffHunk {
  header: string;
  lines: DiffLine[];
}

export interface ParsedDiff {
  fileHeaders: string[];
  hunks: DiffHunk[];
  changedCount: number;
}

// Tolerant regex: the count after the comma may be a non-numeric placeholder
// (e.g. "N" in synthetic fixtures) — accept any non-whitespace
// sequence as long as it reaches the closing " @@".
const HUNK_RE = /^@@ -(\d+)\S* \+(\d+)\S* @@/;

export function parseDiff(text: string): ParsedDiff {
  const lines = text.split('\n');
  const fileHeaders: string[] = [];
  const hunks: DiffHunk[] = [];
  let current: DiffHunk | null = null;
  let oldNo = 0;
  let newNo = 0;
  let changedCount = 0;

  for (const raw of lines) {
    const m = HUNK_RE.exec(raw);
    if (m) {
      current = { header: raw, lines: [] };
      hunks.push(current);
      oldNo = parseInt(m[1], 10);
      newNo = parseInt(m[2], 10);
      continue;
    }
    if (!current) {
      if (
        raw.startsWith('---') ||
        raw.startsWith('+++') ||
        raw.startsWith('Index:') ||
        raw.startsWith('===')
      ) {
        fileHeaders.push(raw);
      }
      continue;
    }
    if (raw.startsWith('\\')) {
      // "\ No newline at end of file" — skip.
      continue;
    }
    if (raw.startsWith('+')) {
      current.lines.push({ kind: 'add', text: raw.slice(1), oldNo: null, newNo });
      newNo++;
      changedCount++;
    } else if (raw.startsWith('-')) {
      current.lines.push({ kind: 'remove', text: raw.slice(1), oldNo, newNo: null });
      oldNo++;
      changedCount++;
    } else if (raw.startsWith(' ')) {
      current.lines.push({ kind: 'context', text: raw.slice(1), oldNo, newNo });
      oldNo++;
      newNo++;
    } else if (raw === '') {
      continue;
    } else {
      // Unknown prefix — treat as context (defensive fallback).
      current.lines.push({ kind: 'context', text: raw, oldNo, newNo });
      oldNo++;
      newNo++;
    }
  }

  annotateWordChanges(hunks);
  return { fileHeaders, hunks, changedCount };
}

function annotateWordChanges(hunks: DiffHunk[]) {
  for (const h of hunks) {
    let i = 0;
    while (i < h.lines.length) {
      if (h.lines[i].kind !== 'remove') {
        i++;
        continue;
      }
      const removes: DiffLine[] = [];
      while (i < h.lines.length && h.lines[i].kind === 'remove') {
        removes.push(h.lines[i]);
        i++;
      }
      const adds: DiffLine[] = [];
      while (i < h.lines.length && h.lines[i].kind === 'add') {
        adds.push(h.lines[i]);
        i++;
      }
      const pairs = Math.min(removes.length, adds.length);
      for (let k = 0; k < pairs; k++) {
        const parts = diffWords(removes[k].text, adds[k].text);
        const rmSegs: DiffSegment[] = [];
        const addSegs: DiffSegment[] = [];
        for (const part of parts) {
          if (part.added) {
            addSegs.push({ text: part.value, emphasis: true });
          } else if (part.removed) {
            rmSegs.push({ text: part.value, emphasis: true });
          } else {
            rmSegs.push({ text: part.value, emphasis: false });
            addSegs.push({ text: part.value, emphasis: false });
          }
        }
        removes[k].segments = rmSegs;
        adds[k].segments = addSegs;
      }
    }
  }
}
