import Editor, { loader, type OnMount } from '@monaco-editor/react';
import * as monaco from 'monaco-editor';
import editorWorker from 'monaco-editor/editor/editor.worker?worker';
import jsonWorker from 'monaco-editor/language/json/json.worker?worker';
import cssWorker from 'monaco-editor/language/css/css.worker?worker';
import htmlWorker from 'monaco-editor/language/html/html.worker?worker';
import tsWorker from 'monaco-editor/language/typescript/ts.worker?worker';
import { useCallback, useEffect, useRef } from 'react';
import styles from './MonacoEditor.module.css';

// Self-host Monaco instead of @monaco-editor/react's default jsdelivr CDN.
// Without this, the editor fetches ~3 MB of JS from cdn.jsdelivr.net at runtime
// — which hangs indefinitely behind firewalls / offline / VPN setups and shows
// Monaco's built-in "Loading..." indicator (visually identical to our own
// editorLoading state, so the user can't tell what's actually broken).
//
// Vite's `?worker` import syntax bundles each Monaco worker as its own chunk;
// MonacoEnvironment.getWorker is consulted by Monaco when it spawns workers for
// syntax services. Without explicit workers, Monaco runs them inline on the
// main thread — functional but slower for large files.
//
// MonacoEditor itself is lazy-loaded (React.lazy in pages), so this whole
// import graph only enters the bundle when a candidate / interviewer first
// opens a file, not on initial app load.
self.MonacoEnvironment = {
  getWorker(_workerId, label) {
    if (label === 'json') return new jsonWorker();
    if (label === 'css' || label === 'scss' || label === 'less') return new cssWorker();
    if (label === 'html' || label === 'handlebars' || label === 'razor') return new htmlWorker();
    if (label === 'typescript' || label === 'javascript') return new tsWorker();
    return new editorWorker();
  },
};
loader.config({ monaco });

/** Debounce delay for auto-save (ms). Cmd/Ctrl+S saves immediately. */
const AUTO_SAVE_DELAY_MS = 1000;

interface MonacoEditorProps {
  path: string;
  content: string;
  onSave?: (value: string) => void;
  readOnly?: boolean;
}

export default function MonacoEditor({ path, content, onSave, readOnly = false }: MonacoEditorProps) {
  const editorRef = useRef<Parameters<OnMount>[0] | null>(null);
  const autoSaveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Tracks the last content imposed externally (file select / sandbox reset / initial load).
  // onChange values matching this string are skipped — they came from setValue(), not the user.
  const lastExternalContentRef = useRef(content);

  const handleMount: OnMount = useCallback(
    (editor, monaco) => {
      editorRef.current = editor;
      if (onSave) {
        editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => {
          // Cmd/Ctrl+S: flush debounce immediately.
          if (autoSaveTimerRef.current) {
            clearTimeout(autoSaveTimerRef.current);
            autoSaveTimerRef.current = null;
          }
          onSave(editor.getValue());
        });
      }
    },
    [onSave],
  );

  // Update content when path or externally-driven content changes (file select / sandbox reset).
  useEffect(() => {
    lastExternalContentRef.current = content;
    if (autoSaveTimerRef.current) {
      clearTimeout(autoSaveTimerRef.current);
      autoSaveTimerRef.current = null;
    }
    const editor = editorRef.current;
    if (editor) {
      const model = editor.getModel();
      if (model && model.getValue() !== content) {
        model.setValue(content);
      }
    }
  }, [path, content]);

  // Cleanup timer on unmount.
  useEffect(() => () => {
    if (autoSaveTimerRef.current) clearTimeout(autoSaveTimerRef.current);
  }, []);

  const handleChange = useCallback(
    (value: string | undefined) => {
      if (!onSave || readOnly || value === undefined) return;
      // Skip changes that match the last externally-set content — these were fired by
      // setValue() (possibly multiple times), not by the user.
      if (value === lastExternalContentRef.current) return;
      if (autoSaveTimerRef.current) clearTimeout(autoSaveTimerRef.current);
      autoSaveTimerRef.current = setTimeout(() => {
        autoSaveTimerRef.current = null;
        onSave(value);
      }, AUTO_SAVE_DELAY_MS);
    },
    [onSave, readOnly],
  );

  return (
    <div className={styles.editorWrap} data-testid="monaco-editor">
      <Editor
        path={path}
        defaultValue={content}
        language={resolveMonacoLang(path)}
        options={{
          readOnly,
          minimap: { enabled: false },
          scrollBeyondLastLine: false,
          automaticLayout: true,
          fontSize: 14,
          tabSize: 4,
          wordWrap: 'off',
          renderLineHighlight: 'line',
        }}
        theme="vs"
        onMount={handleMount}
        onChange={handleChange}
      />
    </div>
  );
}

function resolveMonacoLang(filePath: string): string {
  const base = filePath.split('/').pop() ?? '';
  const ext = base.includes('.') ? base.split('.').pop()! : '';
  const map: Record<string, string> = {
    py: 'python',
    ts: 'typescript',
    tsx: 'typescript',
    js: 'javascript',
    jsx: 'javascript',
    json: 'json',
    yaml: 'yaml',
    yml: 'yaml',
    toml: 'toml',
    md: 'markdown',
    sh: 'shell',
    bash: 'shell',
    css: 'css',
    html: 'html',
    xml: 'xml',
    sql: 'sql',
    go: 'go',
    rs: 'rust',
    rb: 'ruby',
    java: 'java',
    cpp: 'cpp',
    c: 'c',
    cs: 'csharp',
    txt: 'plaintext',
    dockerfile: 'dockerfile',
  };
  if (base.toLowerCase() === 'dockerfile') return 'dockerfile';
  return map[ext.toLowerCase()] ?? 'plaintext';
}
