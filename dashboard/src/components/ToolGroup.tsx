import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { EventEnvelope } from '../api/types';
import { Avatar } from './Avatar';
import { ToolCallBlock } from './ToolCallBlock';
import styles from './ToolGroup.module.css';

type ToolCallEvent = Extract<EventEnvelope, { type: 'tool_call' }>;
type ToolResultEvent = Extract<EventEnvelope, { type: 'tool_result' }>;

interface ToolGroupProps {
  calls: ToolCallEvent[];
  resultByCallId: Map<string, ToolResultEvent>;
}

const AUTO_COLLAPSE_THRESHOLD = 2;

export function ToolGroup({ calls, resultByCallId }: ToolGroupProps) {
  const { t } = useTranslation();
  const toolName = calls[0]?.payload.tool_name ?? '';
  const [expanded, setExpanded] = useState(calls.length <= AUTO_COLLAPSE_THRESHOLD);

  return (
    <div
      className={styles.group}
      data-testid={`tool-group-${toolName.toLowerCase()}`}
      data-expanded={expanded ? 'true' : 'false'}
    >
      <Avatar role="tool" size="sm" />
      <button
        type="button"
        className={styles.header}
        onClick={() => setExpanded((e) => !e)}
        aria-expanded={expanded}
      >
        <span className={styles.chevron} aria-hidden="true">
          {expanded ? '▼' : '▶'}
        </span>
        <span className={styles.name}>{toolName}</span>
        <span className={styles.count}>× {calls.length}</span>
        <span className={styles.hint}>
          {expanded ? t('tool.collapse.collapse') : t('tool.collapse.expand')}
        </span>
      </button>
      {expanded ? (
        <div className={styles.list}>
          {calls.map((c) => (
            <ToolCallBlock
              key={c.id}
              call={c}
              result={resultByCallId.get(c.payload.tool_call_id)}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}
