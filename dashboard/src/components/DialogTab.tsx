import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';
import type { EventEnvelope, SessionStatus } from '../api/types';
import { MessageRow } from './MessageRow';
import { ToolCallBlock } from './ToolCallBlock';
import { ToolGroup } from './ToolGroup';
import { TypingIndicator } from './TypingIndicator';
import { EmptyState } from './EmptyState';
import { groupConsecutiveTools, isDialogEvent, type DialogItem } from './dialogTabUtils';
import styles from './DialogTab.module.css';

type ToolResultEvent = Extract<EventEnvelope, { type: 'tool_result' }>;

// "At bottom" threshold in px. A little slack so small scrollTop jitter
// during render doesn't kick us out of sticky-bottom mode.
const AT_BOTTOM_THRESHOLD_PX = 60;

interface DialogTabProps {
  events: EventEnvelope[];
  sessionActive?: boolean;
  sessionCode?: string;
  sessionStatus?: SessionStatus | null;
  candidateToken?: string;
  // Authoritative "an agent turn is in flight" signal (the backend
  // agent_status frame). When true, the typing indicator shows for the whole
  // turn — including the silent gap before the next LLM call — instead of the
  // last-event heuristic that goes quiet after an intermediate assistant message.
  forceTyping?: boolean;
}

export function DialogTab({
  events,
  sessionActive = true,
  sessionCode,
  sessionStatus,
  candidateToken,
  forceTyping = false,
}: DialogTabProps) {
  const { t } = useTranslation();
  const scrollRef = useRef<HTMLDivElement>(null);
  const atBottomRef = useRef(true);
  const [pendingCount, setPendingCount] = useState(0);
  const prevCountRef = useRef(0);

  // Snapshot of event seqs seen on first mount, so the entry animation
  // doesn't replay history when the page opens. Everything that arrives
  // after mount (live + reconnect tail) is treated as "new" and animated.
  const [initialSeqs] = useState(() => {
    const set = new Set<number>();
    for (const e of events) set.add(e.seq);
    return set;
  });

  // Seq of the last user_message in the events list (null if none).
  // Used to detect when the user sends a new message and force-scroll to bottom.
  const lastUserMsgSeq = useMemo(() => {
    for (let i = events.length - 1; i >= 0; i--) {
      if (events[i]?.type === 'user_message') return events[i]!.seq;
    }
    return null;
  }, [events]);

  const { displayItems, resultByCallId } = useMemo(() => {
    const filtered = events.filter(isDialogEvent);
    const byId = new Map<string, ToolResultEvent>();
    for (const e of filtered) {
      if (e.type === 'tool_result') {
        byId.set(e.payload.tool_call_id, e);
      }
    }
    const paired = new Set<string>();
    for (const e of filtered) {
      if (e.type === 'tool_call' && byId.has(e.payload.tool_call_id)) {
        paired.add(e.payload.tool_call_id);
      }
    }
    const unpaired = filtered.filter(
      (e) => !(e.type === 'tool_result' && paired.has(e.payload.tool_call_id)),
    );
    return { displayItems: groupConsecutiveTools(unpaired), resultByCallId: byId };
  }, [events]);

  // Show the TypingIndicator when the last message event is a user_message
  // and the session is active (the AI hasn't replied yet). tool_call/tool_result
  // between them are ignored: they're part of "the AI is thinking", they don't reset it.
  // The indicator is deliberately NOT cleared on tool_call (UX: "AI working") and
  // reads the raw events, not the filtered displayItems — so the empty-bubble
  // filter in dialogTabUtils doesn't affect it.
  const showTyping = useMemo(() => {
    if (!sessionActive) return false;
    // The backend says a turn is running — show it for the whole turn,
    // even through the tool-heavy / next-LLM-call gap the heuristic below misses.
    if (forceTyping) return true;
    for (let i = events.length - 1; i >= 0; i--) {
      const t = events[i]!.type;
      if (t === 'user_message') return true;
      if (t === 'assistant_message') return false;
    }
    return false;
  }, [events, sessionActive, forceTyping]);

  // Scroll to bottom on ANY new event — including tool_result that only updates
  // an existing tool_call block's content without adding a new displayItem.
  useEffect(() => {
    if (!atBottomRef.current) return;
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [events.length]);

  // Pending counter — only for new logical display items.
  // tool_result events don't add a displayItem and must not inflate this count.
  useEffect(() => {
    const curr = displayItems.length;
    const prev = prevCountRef.current;
    prevCountRef.current = curr;
    const delta = curr - prev;
    if (delta <= 0) return;
    if (atBottomRef.current) {
      setPendingCount(0);
    } else {
      setPendingCount((p) => p + delta);
    }
  }, [displayItems.length]);

  // The typing indicator appearing/disappearing changes height but not displayItems.
  // If the interviewer is at the bottom, scroll down so the indicator stays visible.
  useEffect(() => {
    if (!showTyping || !atBottomRef.current) return;
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [showTyping]);

  // Force-scroll when user sends a new message, regardless of current position.
  // Only fires for messages added after mount (not in initialSeqs snapshot).
  // initialSeqs is a frozen useState snapshot — it never changes after mount,
  // so it is a stable dep and will never trigger a spurious re-run.
  useEffect(() => {
    if (lastUserMsgSeq === null || initialSeqs.has(lastUserMsgSeq)) return;
    const el = scrollRef.current;
    if (el) {
      el.scrollTop = el.scrollHeight;
      atBottomRef.current = true;
      setPendingCount(0);
    }
  }, [lastUserMsgSeq, initialSeqs]);

  const handleScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const atB = el.scrollHeight - el.scrollTop - el.clientHeight < AT_BOTTOM_THRESHOLD_PX;
    atBottomRef.current = atB;
    if (atB) setPendingCount(0);
  };

  const handleJump = () => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    atBottomRef.current = true;
    setPendingCount(0);
  };

  return (
    <div className={styles.wrapper}>
      <div
        className={styles.container}
        ref={scrollRef}
        onScroll={handleScroll}
        data-testid="dialog-scroll"
      >
        <div className={styles.column}>
          {displayItems.length === 0 && !showTyping ? (
            sessionStatus === 'pending' && sessionCode ? (
              <EmptyState code={sessionCode} candidateToken={candidateToken} />
            ) : (
              <p className={styles.empty}>{t('dialog.empty')}</p>
            )
          ) : (
            <>
              {displayItems.map((item, index) => {
                const isLast = index === displayItems.length - 1;
                const isNew = isItemNew(item, initialSeqs);
                return (
                  <div
                    key={item.key}
                    className={isNew ? styles.enter : undefined}
                    data-new={isNew ? 'true' : undefined}
                  >
                    {renderItem(item, isLast, resultByCallId, t)}
                  </div>
                );
              })}
              {showTyping ? <TypingIndicator /> : null}
            </>
          )}
        </div>
      </div>
      {pendingCount > 0 && (
        <button
          type="button"
          className={styles.jumpButton}
          onClick={handleJump}
          data-testid="dialog-jump-button"
        >
          {t('dialog.jumpNew', { count: pendingCount })}
        </button>
      )}
    </div>
  );
}

function renderItem(
  item: DialogItem,
  isLast: boolean,
  resultByCallId: Map<string, ToolResultEvent>,
  t: TFunction,
) {
  if (item.kind === 'message') {
    return <MessageRow event={item.event} />;
  }
  if (item.kind === 'tool') {
    return (
      <ToolCallBlock
        call={item.call}
        result={resultByCallId.get(item.call.payload.tool_call_id)}
        isLatest={isLast}
      />
    );
  }
  if (item.kind === 'system') {
    const label =
      item.event.payload.reason === 'expired'
        ? t('dialog.sandboxResetExpired')
        : t('dialog.sandboxReset');
    return (
      <div className={styles.systemDivider} data-testid="dialog-system-reset">
        <span className={styles.systemDividerLabel}>{label}</span>
      </div>
    );
  }
  return <ToolGroup calls={item.calls} resultByCallId={resultByCallId} />;
}

/**
 * An item is considered "new" (animated) if at least one of its seqs was not
 * present in the `initialSeqs` snapshot (events at the time DialogTab mounted).
 * For a group, check the last call: if it's not from the initial set, the group
 * was either created live or grew after mount.
 */
function isItemNew(item: DialogItem, initialSeqs: ReadonlySet<number>): boolean {
  switch (item.kind) {
    case 'message':
      return !initialSeqs.has(item.event.seq);
    case 'tool':
      return !initialSeqs.has(item.call.seq);
    case 'group': {
      const last = item.calls[item.calls.length - 1];
      return last ? !initialSeqs.has(last.seq) : false;
    }
    case 'system':
      return !initialSeqs.has(item.event.seq);
  }
}
