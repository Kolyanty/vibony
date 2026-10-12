import type { EventEnvelope } from '../api/types';

// Pure projections of an event stream → sandbox state at a replay point.
// Extracted from SessionView so the delete/reset logic is unit-testable
// without rendering. `events` is the visible slice (events[0..cursor]).

/**
 * Paths that exist in the sandbox at this replay point.
 * - `op: 'deleted'` (or legacy `content === null` for older events)
 *   removes the path from the tree.
 * - `sandbox_reset` restores the baseline set: candidate-created files vanish,
 *   baseline scaffold reappears. The backend re-emits baseline `file_changed`
 *   events on reset (re-seeding File History), but those land after the reset;
 *   restoring from the baseline set accumulated before the reset keeps the tree
 *   correct at the exact reset point regardless. The re-emitted events are
 *   idempotent here (already-present paths).
 * - Directory events are ignored — the tree is rebuilt from file paths.
 */
export function replayTreePaths(events: EventEnvelope[]): string[] {
  const seen = new Set<string>();
  const baseline = new Set<string>();
  for (const e of events) {
    if (e.type === 'sandbox_reset') {
      seen.clear();
      for (const p of baseline) seen.add(p);
      continue;
    }
    if (e.type !== 'file_changed' || e.payload.is_dir) continue;
    const { file_path, op, content, source } = e.payload;
    const deleted = op === 'deleted' || (!op && content === null);
    // Track baseline paths separately so sandbox_reset can restore them. Baseline
    // events also fall through to the `seen` update below — they're real files now.
    if (source === 'baseline') {
      if (deleted) baseline.delete(file_path);
      else baseline.add(file_path);
    }
    if (deleted) seen.delete(file_path);
    else seen.add(file_path);
  }
  return [...seen];
}

/**
 * Paths the candidate/agent modified at this replay point (drives the
 * "modified" badge). Baseline-sourced changes are excluded — they seed File
 * History diffs, not "the file was edited". `sandbox_reset` clears the set.
 */
export function replayModifiedPaths(events: EventEnvelope[]): Set<string> {
  const modified = new Set<string>();
  for (const e of events) {
    if (e.type === 'sandbox_reset') {
      modified.clear();
      continue;
    }
    if (e.type !== 'file_changed' || e.payload.is_dir) continue;
    if (e.payload.source === 'baseline') continue;
    const { file_path, op, content } = e.payload;
    if (op === 'deleted' || (!op && content === null)) modified.delete(file_path);
    else modified.add(file_path);
  }
  return modified;
}
