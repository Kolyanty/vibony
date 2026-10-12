// Centralized PrismLight language registration for the whole dashboard.
// Registration used to be duplicated between Markdown.tsx and ToolCallBlock.tsx —
// this is the single place + an extension → language table for diffs/dialog.

import { PrismLight as SyntaxHighlighter } from 'react-syntax-highlighter';

// Mainstream
import python from 'react-syntax-highlighter/dist/esm/languages/prism/python';
import javascript from 'react-syntax-highlighter/dist/esm/languages/prism/javascript';
import typescript from 'react-syntax-highlighter/dist/esm/languages/prism/typescript';
import java from 'react-syntax-highlighter/dist/esm/languages/prism/java';
import c from 'react-syntax-highlighter/dist/esm/languages/prism/c';
import cpp from 'react-syntax-highlighter/dist/esm/languages/prism/cpp';
import csharp from 'react-syntax-highlighter/dist/esm/languages/prism/csharp';
import go from 'react-syntax-highlighter/dist/esm/languages/prism/go';
import rust from 'react-syntax-highlighter/dist/esm/languages/prism/rust';
import ruby from 'react-syntax-highlighter/dist/esm/languages/prism/ruby';
import php from 'react-syntax-highlighter/dist/esm/languages/prism/php';
import swift from 'react-syntax-highlighter/dist/esm/languages/prism/swift';
import kotlin from 'react-syntax-highlighter/dist/esm/languages/prism/kotlin';
import scala from 'react-syntax-highlighter/dist/esm/languages/prism/scala';

// Web
import markup from 'react-syntax-highlighter/dist/esm/languages/prism/markup';
import css from 'react-syntax-highlighter/dist/esm/languages/prism/css';
import scss from 'react-syntax-highlighter/dist/esm/languages/prism/scss';
import jsx from 'react-syntax-highlighter/dist/esm/languages/prism/jsx';
import tsx from 'react-syntax-highlighter/dist/esm/languages/prism/tsx';

// Configs / data
import json from 'react-syntax-highlighter/dist/esm/languages/prism/json';
import yaml from 'react-syntax-highlighter/dist/esm/languages/prism/yaml';
import toml from 'react-syntax-highlighter/dist/esm/languages/prism/toml';
import docker from 'react-syntax-highlighter/dist/esm/languages/prism/docker';
import ini from 'react-syntax-highlighter/dist/esm/languages/prism/ini';
import properties from 'react-syntax-highlighter/dist/esm/languages/prism/properties';

// DB / queries
import sql from 'react-syntax-highlighter/dist/esm/languages/prism/sql';
import graphql from 'react-syntax-highlighter/dist/esm/languages/prism/graphql';

// Shell / ops
import bash from 'react-syntax-highlighter/dist/esm/languages/prism/bash';
import shellSession from 'react-syntax-highlighter/dist/esm/languages/prism/shell-session';
import powershell from 'react-syntax-highlighter/dist/esm/languages/prism/powershell';
import makefile from 'react-syntax-highlighter/dist/esm/languages/prism/makefile';
import hcl from 'react-syntax-highlighter/dist/esm/languages/prism/hcl';
import nginx from 'react-syntax-highlighter/dist/esm/languages/prism/nginx';

// Markup
import markdown from 'react-syntax-highlighter/dist/esm/languages/prism/markdown';
import latex from 'react-syntax-highlighter/dist/esm/languages/prism/latex';

// Functional
import haskell from 'react-syntax-highlighter/dist/esm/languages/prism/haskell';
import ocaml from 'react-syntax-highlighter/dist/esm/languages/prism/ocaml';
import fsharp from 'react-syntax-highlighter/dist/esm/languages/prism/fsharp';
import clojure from 'react-syntax-highlighter/dist/esm/languages/prism/clojure';
import elixir from 'react-syntax-highlighter/dist/esm/languages/prism/elixir';
import erlang from 'react-syntax-highlighter/dist/esm/languages/prism/erlang';
import lisp from 'react-syntax-highlighter/dist/esm/languages/prism/lisp';

// Other
import r from 'react-syntax-highlighter/dist/esm/languages/prism/r';
import julia from 'react-syntax-highlighter/dist/esm/languages/prism/julia';
import lua from 'react-syntax-highlighter/dist/esm/languages/prism/lua';
import dart from 'react-syntax-highlighter/dist/esm/languages/prism/dart';
import solidity from 'react-syntax-highlighter/dist/esm/languages/prism/solidity';
import protobuf from 'react-syntax-highlighter/dist/esm/languages/prism/protobuf';
import regex from 'react-syntax-highlighter/dist/esm/languages/prism/regex';
import diff from 'react-syntax-highlighter/dist/esm/languages/prism/diff';

const LANGUAGES: Record<string, unknown> = {
  python,
  py: python,
  javascript,
  js: javascript,
  typescript,
  ts: typescript,
  java,
  c,
  cpp,
  'c++': cpp,
  csharp,
  cs: csharp,
  go,
  rust,
  rs: rust,
  ruby,
  rb: ruby,
  php,
  swift,
  kotlin,
  kt: kotlin,
  scala,
  markup,
  html: markup,
  xml: markup,
  svg: markup,
  css,
  scss,
  sass: scss,
  jsx,
  tsx,
  json,
  yaml,
  yml: yaml,
  toml,
  docker,
  dockerfile: docker,
  ini,
  properties,
  sql,
  graphql,
  bash,
  sh: bash,
  shell: bash,
  zsh: bash,
  'shell-session': shellSession,
  console: shellSession,
  powershell,
  ps1: powershell,
  makefile,
  hcl,
  terraform: hcl,
  nginx,
  markdown,
  md: markdown,
  latex,
  tex: latex,
  haskell,
  hs: haskell,
  ocaml,
  ml: ocaml,
  fsharp,
  fs: fsharp,
  clojure,
  clj: clojure,
  elixir,
  ex: elixir,
  exs: elixir,
  erlang,
  erl: erlang,
  lisp,
  cl: lisp,
  r,
  julia,
  jl: julia,
  lua,
  dart,
  solidity,
  sol: solidity,
  protobuf,
  proto: protobuf,
  regex,
  diff,
};

let registered = false;

function registerAll() {
  if (registered) return;
  for (const [name, lang] of Object.entries(LANGUAGES)) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    SyntaxHighlighter.registerLanguage(name, lang as any);
  }
  registered = true;
}

registerAll();

export const prism = SyntaxHighlighter;

// Extension → language name (the ones registered above). Used by
// FileDiffHistory.tsx and ToolCallBlock.tsx (Write fallback). Extensions with no
// entry → null, and the caller can render a plain <pre>.
const EXT_TO_LANG: Record<string, string> = {
  py: 'python',
  pyi: 'python',
  js: 'javascript',
  mjs: 'javascript',
  cjs: 'javascript',
  ts: 'typescript',
  jsx: 'jsx',
  tsx: 'tsx',
  java: 'java',
  c: 'c',
  h: 'c',
  cc: 'cpp',
  cpp: 'cpp',
  cxx: 'cpp',
  hpp: 'cpp',
  hxx: 'cpp',
  cs: 'csharp',
  go: 'go',
  rs: 'rust',
  rb: 'ruby',
  php: 'php',
  swift: 'swift',
  kt: 'kotlin',
  kts: 'kotlin',
  scala: 'scala',
  sc: 'scala',
  html: 'markup',
  htm: 'markup',
  xml: 'markup',
  svg: 'markup',
  xhtml: 'markup',
  css: 'css',
  scss: 'scss',
  sass: 'scss',
  json: 'json',
  yaml: 'yaml',
  yml: 'yaml',
  toml: 'toml',
  ini: 'ini',
  cfg: 'ini',
  conf: 'ini',
  properties: 'properties',
  sql: 'sql',
  graphql: 'graphql',
  gql: 'graphql',
  sh: 'bash',
  bash: 'bash',
  zsh: 'bash',
  ps1: 'powershell',
  hcl: 'hcl',
  tf: 'hcl',
  tfvars: 'hcl',
  md: 'markdown',
  markdown: 'markdown',
  tex: 'latex',
  hs: 'haskell',
  ml: 'ocaml',
  mli: 'ocaml',
  fs: 'fsharp',
  fsx: 'fsharp',
  fsi: 'fsharp',
  clj: 'clojure',
  cljs: 'clojure',
  cljc: 'clojure',
  ex: 'elixir',
  exs: 'elixir',
  erl: 'erlang',
  hrl: 'erlang',
  lisp: 'lisp',
  cl: 'lisp',
  el: 'lisp',
  r: 'r',
  jl: 'julia',
  lua: 'lua',
  dart: 'dart',
  sol: 'solidity',
  proto: 'protobuf',
  regex: 'regex',
  diff: 'diff',
  patch: 'diff',
};

// Special filenames without an extension.
const FILENAME_TO_LANG: Record<string, string> = {
  dockerfile: 'docker',
  makefile: 'makefile',
  'gnumakefile': 'makefile',
  '.bashrc': 'bash',
  '.zshrc': 'bash',
  '.profile': 'bash',
  'nginx.conf': 'nginx',
};

export function resolveLang(path: string): string | null {
  if (!path) return null;
  const basename = path.split('/').pop() ?? path;
  const lower = basename.toLowerCase();
  if (FILENAME_TO_LANG[lower]) return FILENAME_TO_LANG[lower];
  const dot = lower.lastIndexOf('.');
  if (dot < 0 || dot === lower.length - 1) return null;
  const ext = lower.slice(dot + 1);
  return EXT_TO_LANG[ext] ?? null;
}
