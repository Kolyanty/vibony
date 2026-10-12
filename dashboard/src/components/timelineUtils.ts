import type { EventEnvelope } from '../api/types';
import { GROUPABLE_TOOLS } from './dialogTabUtils';

/**
 * Structural session timeline. A curated projection of the event log into
 * phases (each `user_message` opens one) with a handful of key entries each, used
 * for navigation — clicking an entry sets the replay cursor to that event's `seq`.
 * Distinct from the fine-grained `ReplayTimeline` scrubber (which steps over every
 * raw event); this is the "navigate by structure" + deep-link surface.
 */

export type TimelineEntryKind =
  | 'session_start'
  | 'assistant_message'
  | 'tool'
  | 'tool_group'
  | 'command'
  | 'file_saved'
  | 'file_deleted'
  | 'diff_proposed'
  | 'diff_accepted'
  | 'diff_rejected'
  | 'model_switched'
  | 'sandbox_reset'
  | 'sandbox_restored'
  | 'session_end';

export interface TimelineEntry {
  /** The event's seq — the cursor target when this entry is clicked. */
  seq: number;
  kind: TimelineEntryKind;
  /** Primary raw text (path / command / tool name / message line / model pair). */
  text: string;
  /** Folded count for `tool_group`. */
  count?: number;
  /** Exit code, filled in when the matching `command_finished` arrives. */
  exitCode?: number;
}

export interface TimelinePhase {
  /** Opening user-message text; null = the implicit setup phase (pre-first message). */
  label: string | null;
  /** Seq of the opening `user_message` (cursor target for the header); null for setup. */
  headerSeq: number | null;
  entries: TimelineEntry[];
}

const FIRST_LINE_CAP = 100;

function firstLine(text: string, cap = FIRST_LINE_CAP): string {
  const line = text.split('\n', 1)[0]?.trim() ?? '';
  return line.length > cap ? `${line.slice(0, cap - 1).trimEnd()}…` : line;
}

/**
 * Fold the event log into navigable phases. Skips noise that has no navigation
 * value: `command_output` (folded into its command), `tool_result` (folded into
 * its `tool_call`), baseline `file_changed`, directory ops. Read/Glob/Grep
 * `tool_call`s collapse into a single counted `tool_group` (mirrors the dialog's
 * `GROUPABLE_TOOLS`). A `command_finished` mutates its `command_run` entry's
 * `exitCode` rather than adding a row.
 */
export function buildTimeline(events: EventEnvelope[]): TimelinePhase[] {
  const phases: TimelinePhase[] = [];
  let current: TimelinePhase = { label: null, headerSeq: null, entries: [] };
  const commandEntries = new Map<string, TimelineEntry>();

  let groupSeq: number | null = null;
  let groupCount = 0;

  const flushGroup = () => {
    if (groupSeq === null) return;
    current.entries.push({ seq: groupSeq, kind: 'tool_group', text: '', count: groupCount });
    groupSeq = null;
    groupCount = 0;
  };

  const closePhase = () => {
    flushGroup();
    // Keep the setup phase only if it has entries; user-message phases always show.
    if (current.entries.length > 0 || current.headerSeq !== null) phases.push(current);
  };

  for (const e of events) {
    if (e.type === 'user_message') {
      closePhase();
      current = { label: firstLine(e.payload.text), headerSeq: e.seq, entries: [] };
      continue;
    }

    if (e.type === 'tool_call' && GROUPABLE_TOOLS.has(e.payload.tool_name)) {
      if (groupSeq === null) groupSeq = e.seq;
      groupCount += 1;
      continue;
    }

    flushGroup();

    switch (e.type) {
      case 'session_start':
        current.entries.push({ seq: e.seq, kind: 'session_start', text: e.payload.task_id });
        break;
      case 'session_end':
        current.entries.push({ seq: e.seq, kind: 'session_end', text: e.payload.reason });
        break;
      case 'assistant_message':
        current.entries.push({ seq: e.seq, kind: 'assistant_message', text: firstLine(e.payload.text) });
        break;
      case 'tool_call':
        current.entries.push({ seq: e.seq, kind: 'tool', text: e.payload.tool_name });
        break;
      case 'command_run': {
        const entry: TimelineEntry = { seq: e.seq, kind: 'command', text: e.payload.command };
        commandEntries.set(e.payload.command_id, entry);
        current.entries.push(entry);
        break;
      }
      case 'command_finished': {
        const entry = commandEntries.get(e.payload.command_id);
        if (entry) entry.exitCode = e.payload.exit_code;
        break;
      }
      case 'file_changed': {
        if (e.payload.source === 'baseline' || e.payload.is_dir) break;
        const deleted = e.payload.op === 'deleted' || e.payload.content === null;
        current.entries.push({
          seq: e.seq,
          kind: deleted ? 'file_deleted' : 'file_saved',
          text: e.payload.file_path,
        });
        break;
      }
      case 'diff_proposed':
        current.entries.push({ seq: e.seq, kind: 'diff_proposed', text: e.payload.file_path });
        break;
      case 'diff_accepted':
        current.entries.push({ seq: e.seq, kind: 'diff_accepted', text: '' });
        break;
      case 'diff_rejected':
        current.entries.push({ seq: e.seq, kind: 'diff_rejected', text: e.payload.reason ?? '' });
        break;
      case 'model_switched':
        current.entries.push({
          seq: e.seq,
          kind: 'model_switched',
          text: `${e.payload.from_model} → ${e.payload.to_model}`,
        });
        break;
      case 'sandbox_reset':
        current.entries.push({ seq: e.seq, kind: 'sandbox_reset', text: '' });
        break;
      case 'sandbox_restored':
        current.entries.push({
          seq: e.seq,
          kind: 'sandbox_restored',
          text: String(e.payload.file_count),
        });
        break;
      // command_output, tool_result, baseline file_changed → folded/skipped above.
      default:
        break;
    }
  }

  closePhase();
  return phases;
}

/**
 * Resolve a target seq (cursor's event, or a deep-link citation) to the seq of the
 * timeline point that "contains" it: the greatest header/entry seq ≤ target. This
 * makes navigation robust when the target seq is a folded event (e.g. a finding
 * cites a `command_finished` whose row is the earlier `command_run`). Relies on
 * phases + entries being in ascending seq order (they are — built in stream order).
 */
export function resolveActiveSeq(phases: TimelinePhase[], target: number | null): number | null {
  if (target === null) return null;
  let best: number | null = null;
  for (const phase of phases) {
    if (phase.headerSeq !== null && phase.headerSeq <= target) best = phase.headerSeq;
    for (const entry of phase.entries) {
      if (entry.seq <= target) best = entry.seq;
    }
  }
  return best;
}
