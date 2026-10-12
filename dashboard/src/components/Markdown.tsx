import ReactMarkdown from 'react-markdown';
import type { Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { oneLight } from 'react-syntax-highlighter/dist/esm/styles/prism';
import { prism as SyntaxHighlighter } from '../lib/prism';
import styles from './Markdown.module.css';

const components: Components = {
  code({ className, children, ...props }) {
    const text = String(children ?? '').replace(/\n$/, '');
    const langMatch = /language-([\w-]+)/.exec(className ?? '');
    const isBlock = Boolean(langMatch) || text.includes('\n');
    if (!isBlock) {
      return (
        <code className={styles.inlineCode} {...props}>
          {children}
        </code>
      );
    }
    const language = langMatch?.[1] ?? 'text';
    return (
      <SyntaxHighlighter
        language={language}
        style={oneLight}
        PreTag="div"
        customStyle={{
          margin: 0,
          padding: '10px 12px',
          background: 'var(--code-bg)',
          borderRadius: 6,
          fontSize: '12.5px',
          lineHeight: 1.5,
        }}
        codeTagProps={{
          style: { fontFamily: 'var(--font-mono)' },
        }}
      >
        {text}
      </SyntaxHighlighter>
    );
  },
  pre({ children }) {
    return <div className={styles.pre}>{children}</div>;
  },
  a({ href, children, ...props }) {
    return (
      <a href={href} target="_blank" rel="noopener noreferrer" {...props}>
        {children}
      </a>
    );
  },
  table({ children }) {
    return (
      <div className={styles.tableWrap}>
        <table className={styles.table}>{children}</table>
      </div>
    );
  },
};

interface MarkdownProps {
  source: string;
}

export function Markdown({ source }: MarkdownProps) {
  return (
    <div className={styles.root}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {source}
      </ReactMarkdown>
    </div>
  );
}
