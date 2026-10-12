# Toolset interface

**Context:** [README.md](README.md), [events.md](events.md), [docs/architecture.md](../architecture.md).

This document fixes the `Toolset` interface — the abstraction through which the agent works with the filesystem and shell. The agent uses `SandboxToolset`, which RPCs into the sandbox container via [`SandboxProvider`](sandbox-provider.md). Keeping the agent behind this interface makes it runtime-agnostic.

This is **not** the list of tools the LLM sees — this is the Python interface between the agent core and the execution environment. The mapping to LLM tool definitions (name, description, JSON schema) is a neighboring concern, described in §"LLM tool definitions".

---

## Why the interface is separate from the agent

If the agent directly called `open(path)` and `subprocess.run(cmd)`, switching the runtime (local vs a sandbox container) would require rewriting the agent. The `Toolset` moves these calls behind an interface — the agent doesn't know whether the filesystem is local or remote.

Additional benefit: tool calls are automatically logged to the event log (see §"Event emission"). If the agent called FS/subprocess directly, logging would need to be added at dozens of points.

**Hard rule:** the agent core makes no direct calls to `os`, `pathlib`, `subprocess`, `aiofiles`, etc. Everything goes through `Toolset`. Any violation is a bug.

---

## Composition

Six methods, the minimal set the agent needs:

| Method | Purpose |
|--------|---------|
| `read` | Read file contents |
| `write` | Write file in full (create or complete overwrite) |
| `edit` | Targeted string replacement in a file (with fuzzy matching) |
| `bash` | Run a shell command |
| `glob` | Find files by pattern |
| `grep` | Search file contents |

Extending the list is additive (no `protocol_version` bump). Deletion or rename is breaking.

---

## Interface

Python ≥ 3.12, async-only. All methods are async so that a specific implementation can be either local (instant) or remote (RPC into the sandbox) without changing the signature.

```python
from typing import Protocol
from dataclasses import dataclass

@dataclass
class BashResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    timed_out: bool

@dataclass
class GrepMatch:
    path: str
    line_number: int
    line: str

class Toolset(Protocol):
    async def read(
        self,
        path: str,
        offset: int = 0,
        limit: int | None = None,
    ) -> str: ...

    async def write(
        self,
        path: str,
        content: str,
    ) -> None: ...

    async def edit(
        self,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> None: ...

    async def bash(
        self,
        command: str,
        cwd: str | None = None,
        timeout_ms: int = 120_000,
    ) -> BashResult: ...

    async def glob(
        self,
        pattern: str,
    ) -> list[str]: ...

    async def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> list[GrepMatch]: ...
```

---

## Method semantics

### `read(path, offset=0, limit=None) → str`

Returns file contents as a UTF-8 string.

- `path` — a relative path from the working directory. Absolute paths are forbidden (see §"Sandbox").
- `offset`, `limit` — line-based pagination; 0-based; `limit=None` means "to end of file".
- Binary files — `FileEncodingError`. The agent doesn't read binaries.
- Non-existent file — `FileNotFoundError`.

### `write(path, content) → None`

Writes the file in full. Creates intermediate directories if necessary. Overwrites an existing file without confirmation (interactive accept/reject is a layer above; see [events.md](events.md), `diff_proposed`).

### `edit(path, old_string, new_string, replace_all=False) → None`

Targeted replacement. Semantics like `Edit` from Claude Code:

1. `old_string` is searched in the file:
   - First, an exact match.
   - If not found — fuzzy matching: ignore leading whitespace, normalize line endings, relative indentation. The implementation is borrowed from Aider (Apache 2.0).
2. If a single match is found or `replace_all=True` — replaced.
3. If multiple matches and `replace_all=False` — `MultipleMatchesError`. The agent must either tighten `old_string` (more context) or pass `replace_all=True`.
4. If `old_string` is not found exactly or fuzzily — `NoMatchError`.

`write` vs `edit`: the agent uses `edit` for modifications, `write` for creating new files or full rewrites. This is a convention, not enforced.

### `bash(command, cwd=None, timeout_ms=120_000) → BashResult`

Runs a command through a shell (`/bin/sh -c command` on Unix). Default `cwd` — the task working directory.

- `timed_out=True` if the command didn't complete within `timeout_ms` — the process is killed (SIGTERM, then SIGKILL after 5s), and partial `stdout`/`stderr` is returned.
- `stdout` and `stderr` — UTF-8 (with invalid bytes replaced by `?`).
- Maximum size of each of `stdout`/`stderr` — 1 MB. Exceeded — truncated with the suffix `... [truncated]`.
- The command inherits the sandbox container's `PATH` and environment variables.

Important: `bash` is an **agent** call, separate from the shell wrapper the candidate uses for their own commands. Both emit `command_run`/`command_output`/`command_finished` events (see [events.md](events.md)), but from different subsystems and with a different `command_id`.

### `glob(pattern) → list[str]`

Returns a sorted list of relative paths matching the glob pattern (syntax like `pathlib.Path.glob` / fnmatch). Examples:
- `**/*.py` — all Python files recursively
- `tests/test_*.py` — tests with a prefix

Ignores private directories by the `.*` and `_*` rule (see [task-format.md](task-format.md)).

### `grep(pattern, path=None, glob=None) → list[GrepMatch]`

Regex search across file contents.

- `pattern` — Python `re` syntax.
- `path` — subdirectory to search in (default = working directory).
- `glob` — file filter (default — all text files).
- Returns at most 200 matches; more are truncated without error, as a signal to the agent to "refine the query".

---

## Sandbox

The `Toolset` enforces a perimeter on all methods:

1. **All paths are relative** to the working directory. Attempts to pass an absolute path, a path with `..` outside the directory, or a symlink outside — `PathOutsideWorkdirError`.
2. **System paths are forbidden.** Even if somehow possible (e.g., a symlink appeared during the session), the methods refuse.

In `SandboxToolset` the perimeter is hard, because the whole FS is the sandbox container and the agent can't exit it. The candidate's own editor and shell can still touch their workdir freely — the sandbox protects against the agent, not the candidate.

---

## Error contract

Exception hierarchy:

```
ToolsetError
├── FileNotFoundError              (read, edit)
├── FileEncodingError              (read of binary file)
├── PathOutsideWorkdirError        (any method)
├── NoMatchError                   (edit: old_string not found)
├── MultipleMatchesError           (edit: >1 match without replace_all)
├── BashTimeoutError               (only if agent explicitly passed raise_on_timeout — otherwise timed_out=True in BashResult)
└── ToolsetInternalError           (everything else: I/O fault, permission denied, etc.)
```

All exceptions inherit from `ToolsetError` — this lets the agent loop catch and convert to `tool_result(is_error=True, output=<error message>)` in one place.

Behavior: exceptions are **not** propagated directly to the LLM. The agent loop catches them, emits `tool_result(is_error=True, output="<error type>: <message>")`, and on the next iteration the LLM sees that the tool failed and decides what to do.

---

## Event emission

The `Toolset` **itself** emits events via the `EventSink` passed to the implementation's constructor. This removes the responsibility from the agent loop:

- Before calling the method — a `tool_call` event with `tool_name`, `input`, a new `tool_call_id`.
- After successful completion — a `tool_result` event with the same `tool_call_id`, `output`, `is_error=False`.
- On exception — a `tool_result` with `is_error=True` and a serialized message.

This correlation guarantees the invariant from [events.md](events.md): "each `tool_call` is closed by exactly one `tool_result`".

```python
class EventSink(Protocol):
    async def emit(self, type: str, payload: dict) -> None: ...
```

Specific constructor signatures are implementation details, not contract.

---

## LLM tool definitions

The mapping from `Toolset` methods to the JSON schema the LLM sees. Tool names for the LLM **match the method names** (PascalCase for industry-standard compatibility — `Read`/`Edit`/`Write`/`Bash`/`Glob`/`Grep`); descriptions and input schemas are assembled automatically from docstrings + type hints.

Only fixed by the contract:
- Tool names for the LLM: `Read`, `Write`, `Edit`, `Bash`, `Glob`, `Grep`.
- A new LLM description doesn't require a `protocol_version` bump — it's a build detail, not part of the contract between components.

---

## Implementations

### `SandboxToolset`

Lives in the backend agent host. Each method is an RPC into the sandbox container via `SandboxProvider` (see [sandbox-provider.md](sandbox-provider.md)). Event emission goes to the backend event bus. Implementation: `backend/vibony_backend/sandbox/toolset.py`.

---

## Not in scope

- `MultiEdit` (multiple edits in one tool-call) — additive, if needed to reduce LLM roundtrips.
- `WebFetch` / `WebSearch` — the agent shouldn't search the internet during a task.
- `Notebook` / Jupyter — not needed for the current tasks.
- Parallel execution of multiple tool-calls — an optimization, not contract.
- Streaming output from `bash` — bash returns its result in full; the stream of `command_output` events is done by the shell wrapper for the candidate's own commands, not tool-bash.
