import type { EventEnvelope } from '../api/types';

type CommandRunEvent = Extract<EventEnvelope, { type: 'command_run' }>;
type CommandFinishedEvent = Extract<EventEnvelope, { type: 'command_finished' }>;

export interface CommandChunk {
  stream: 'stdout' | 'stderr';
  data: string;
  seq: number;
}

export interface CommandLog {
  commandId: string;
  runEvent: CommandRunEvent;
  chunks: CommandChunk[];
  finished: CommandFinishedEvent | null;
}

export function aggregateCommands(events: EventEnvelope[]): CommandLog[] {
  const map = new Map<string, CommandLog>();

  for (const e of events) {
    if (e.type !== 'command_run') continue;
    const id = e.payload.command_id;
    if (!map.has(id)) {
      map.set(id, { commandId: id, runEvent: e, chunks: [], finished: null });
    }
  }

  for (const e of events) {
    if (e.type === 'command_output') {
      const log = map.get(e.payload.command_id);
      if (!log) continue;
      log.chunks.push({
        stream: e.payload.stream,
        data: e.payload.data,
        seq: e.seq,
      });
    } else if (e.type === 'command_finished') {
      const log = map.get(e.payload.command_id);
      if (log && log.finished === null) log.finished = e;
    }
  }

  for (const log of map.values()) {
    log.chunks.sort((a, b) => a.seq - b.seq);
  }

  return Array.from(map.values()).sort((a, b) => a.runEvent.seq - b.runEvent.seq);
}

export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return '—';
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const sec = ms / 1000;
  if (sec < 60) return `${sec.toFixed(sec < 10 ? 1 : 0)}s`;
  const min = Math.floor(sec / 60);
  const rest = Math.round(sec - min * 60);
  return `${min}m ${rest}s`;
}
