import { useEffect, useRef } from 'react';
import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import '@xterm/xterm/css/xterm.css';
import styles from './XtermView.module.css';

export interface XtermChunk {
  stream: 'stdout' | 'stderr';
  data: string;
  seq: number;
}

export interface XtermViewProps {
  chunks: XtermChunk[];
  cols?: number;
  rows?: number;
}

const DEFAULT_COLS = 120;
const DEFAULT_ROWS = 30;
const MAX_VISIBLE_ROWS = 24;

function countOutputRows(chunks: XtermChunk[]): number {
  const text = chunks.map(c => c.data).join('');
  if (!text) return 1;
  // In a pty the cursor sits on the line AFTER the last \n (an empty trailing row),
  // and content without a trailing \n still takes its own line — hence +1.
  // Without this `ls` (a single newline) collapses to 1 row and an empty row shows
  // under the cursor while the real output scrolls off into scrollback.
  const newlineCount = (text.match(/\n/g) ?? []).length;
  return Math.max(1, newlineCount + 1);
}

function XtermView({ chunks, cols, rows }: XtermViewProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const terminalRef = useRef<Terminal | null>(null);
  const writtenCountRef = useRef(0);

  // Mount: create Terminal, write all known chunks once.
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return undefined;
    const terminal = new Terminal({
      cols: cols ?? DEFAULT_COLS,
      rows: rows ?? DEFAULT_ROWS,
      convertEol: true,
      disableStdin: true,
      cursorBlink: false,
      scrollback: 5000,
      fontFamily:
        "ui-monospace, 'SF Mono', Menlo, Consolas, 'Liberation Mono', monospace",
      fontSize: 13,
      theme: {
        background: '#1a1a1a',
        foreground: '#fafafa',
        cursor: '#fafafa',
      },
    });
    const fitAddon = new FitAddon();
    terminal.loadAddon(fitAddon);
    terminal.open(container);
    try {
      fitAddon.fit();
    } catch {
      // jsdom doesn't report real sizes — keep cols/rows as they are.
    }
    terminalRef.current = terminal;
    for (const chunk of chunks) {
      terminal.write(chunk.data);
    }
    writtenCountRef.current = chunks.length;

    const targetRows = Math.min(MAX_VISIBLE_ROWS, countOutputRows(chunks));
    if (targetRows !== terminal.rows) {
      terminal.resize(terminal.cols, targetRows);
    }
    return () => {
      terminal.dispose();
      terminalRef.current = null;
      writtenCountRef.current = 0;
    };
    // Only on mount/unmount. cols/rows are fixed at the moment the command
    // starts and don't change for a given XtermView (resize is backlog).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Delta-write new chunks, or a full rewrite when the set shrinks.
  useEffect(() => {
    const terminal = terminalRef.current;
    if (!terminal) return;
    const start = writtenCountRef.current;
    if (chunks.length === start) return; // nothing to append
    if (chunks.length < start) {
      // Replay rewind: the visible chunk set shrank. xterm can only append,
      // so we reset and rewrite from scratch — otherwise the console shows
      // output from events AFTER the cursor.
      terminal.reset();
      for (const chunk of chunks) {
        terminal.write(chunk.data);
      }
    } else {
      for (let i = start; i < chunks.length; i++) {
        terminal.write(chunks[i]!.data);
      }
    }
    writtenCountRef.current = chunks.length;

    const targetRows = Math.min(MAX_VISIBLE_ROWS, countOutputRows(chunks));
    if (targetRows !== terminal.rows) {
      terminal.resize(terminal.cols, targetRows);
    }
  }, [chunks]);

  return (
    <div
      ref={containerRef}
      className={styles.container}
      data-testid="xterm-container"
    />
  );
}

export default XtermView;
