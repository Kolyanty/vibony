import type { EventEnvelope } from '../api/types';

type UserMsgEvent = Extract<EventEnvelope, { type: 'user_message' }>;
type AssistantMsgEvent = Extract<EventEnvelope, { type: 'assistant_message' }>;
type ToolCallEvent = Extract<EventEnvelope, { type: 'tool_call' }>;
type ToolResultEvent = Extract<EventEnvelope, { type: 'tool_result' }>;
type SandboxResetEvent = Extract<EventEnvelope, { type: 'sandbox_reset' }>;

export type DialogEvent =
  | UserMsgEvent
  | AssistantMsgEvent
  | ToolCallEvent
  | ToolResultEvent
  | SandboxResetEvent;

export type DialogItem =
  | { kind: 'message'; key: string; event: UserMsgEvent | AssistantMsgEvent }
  | { kind: 'tool'; key: string; call: ToolCallEvent }
  | { kind: 'group'; key: string; calls: ToolCallEvent[] }
  | { kind: 'system'; key: string; event: SandboxResetEvent };

/**
 * Lightweight read-only tools whose adjacent calls collapse into a group.
 * Edit/Write/Bash never land here — they are significant actions, each
 * rendered individually.
 */
export const GROUPABLE_TOOLS: ReadonlySet<string> = new Set(['Read', 'Glob', 'Grep']);

export function isDialogEvent(e: EventEnvelope): e is DialogEvent {
  return (
    e.type === 'user_message' ||
    e.type === 'assistant_message' ||
    e.type === 'tool_call' ||
    e.type === 'tool_result' ||
    e.type === 'sandbox_reset'
  );
}

/**
 * Collapses consecutive tool_call events of the same "lightweight" tool_name into
 * a group. tool_result events don't participate — they're already filtered as
 * paired in DialogTab and pulled in by `tool_call_id` via `resultByCallId`.
 */
export function groupConsecutiveTools(items: DialogEvent[]): DialogItem[] {
  const result: DialogItem[] = [];
  let pending: ToolCallEvent[] = [];
  let pendingName: string | null = null;

  const flush = () => {
    if (pending.length === 0) return;
    if (pending.length === 1) {
      const only = pending[0];
      result.push({ kind: 'tool', key: only.id, call: only });
    } else {
      result.push({ kind: 'group', key: `group-${pending[0].id}`, calls: pending });
    }
    pending = [];
    pendingName = null;
  };

  for (const e of items) {
    if (e.type === 'tool_call' && GROUPABLE_TOOLS.has(e.payload.tool_name)) {
      if (pendingName === null || pendingName === e.payload.tool_name) {
        pendingName = e.payload.tool_name;
        pending.push(e);
        continue;
      }
      flush();
      pendingName = e.payload.tool_name;
      pending.push(e);
      continue;
    }
    flush();
    if (e.type === 'tool_call') {
      result.push({ kind: 'tool', key: e.id, call: e });
    } else if (e.type === 'user_message' || e.type === 'assistant_message') {
      // Drop empty/whitespace-only bubbles. Defends against
      // any legacy empty assistant_message already persisted in the DB (the
      // backend fix only stops new ones) and is harmless for live events.
      if (e.payload.text.trim() !== '') {
        result.push({ kind: 'message', key: e.id, event: e });
      }
    } else if (e.type === 'sandbox_reset') {
      result.push({ kind: 'system', key: e.id, event: e });
    }
    // unpaired tool_result — ignore (edge case)
  }
  flush();
  return result;
}
