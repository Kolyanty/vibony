# SandboxProvider interface

**Context:** [README.md](README.md), [toolset.md](toolset.md), [docs/architecture.md](../architecture.md).

This document fixes the `SandboxProvider` interface — the module boundary that the backend's `SandboxToolset` attaches to. The shipped implementation is `DockerSandboxProvider` (a Docker container per session); the interface is designed so a different backend (a K8s fleet, another sandbox provider) is an implementation swap behind a stable contract.

---

## Interface

Async-only, Python ≥ 3.12.

```python
from typing import Protocol
from dataclasses import dataclass

@dataclass
class SandboxHandle:
    sandbox_id: str
    # Backend-internal identifier of the active sandbox container.

@dataclass
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    timed_out: bool

class SandboxProvider(Protocol):
    async def create(
        self,
        task_bundle: bytes,                 # tar.gz content (same as what REST returns)
        task_dir_name: str,                 # name of the working directory inside the sandbox
        env: dict[str, str] | None = None,
        resource_limits: "ResourceLimits" | None = None,
    ) -> SandboxHandle: ...

    async def exec(
        self,
        handle: SandboxHandle,
        command: str,
        cwd: str | None = None,
        timeout_ms: int = 120_000,
    ) -> ExecResult: ...

    async def read_file(
        self,
        handle: SandboxHandle,
        path: str,
    ) -> str: ...

    async def write_file(
        self,
        handle: SandboxHandle,
        path: str,
        content: str,
    ) -> None: ...

    async def list_changed_since(
        self,
        handle: SandboxHandle,
        since_ts: float,
    ) -> list[str]: ...

    async def teardown(
        self,
        handle: SandboxHandle,
    ) -> None: ...
```

`ResourceLimits` — a structure with CPU/memory/disk limits.

---

## Method semantics

### `create`
Starts a new sandbox container, unpacks `task_bundle` into `~/{task_dir_name}/`, returns a `SandboxHandle`. The container is ephemeral — it lives while the `handle` exists and is deleted on `teardown`. Not reused between sessions.

### `exec`
Runs a shell command inside the sandbox. Corresponds to the `Toolset.bash` method — `SandboxToolset.bash` is implemented as `await provider.exec(self.handle, command, ...)`.

### `read_file` / `write_file`
FS operations inside the sandbox. Correspond to `Toolset.read` / `Toolset.write`. Paths are relative to the working directory.

### `list_changed_since`
Returns the relative paths of non-private files modified after `since_ts`. Polled after each candidate command to derive `file_changed` events (see [events.md](events.md)); it runs Python inside the container (no host↔container timezone issues) and applies a small clock-skew buffer.

### `teardown`
Stops and deletes the container. Idempotent.

---

## Lifecycle

```
session(active) → SandboxProvider.create() → SandboxHandle
                       │
                       │ exec/read_file/write_file/list_changed_since
                       │ (via SandboxToolset → agent)
                       ▼
session(completed) → SandboxProvider.teardown(handle)
```

The sandbox is bound 1:1 to the session lifetime. On a WS break the sandbox stays alive; when the interviewer ends the session it is killed.

On a backend restart mid-session the container is destroyed by the startup reaper and a fresh one is provisioned; the file state is restored by replaying the `file_changed` event log (the event log is the source of truth for the sandbox FS — see [architecture.md](../architecture.md)).

---

## Module boundary

The provider is the single place that touches subprocess/FS on behalf of the candidate. Backend logic outside `sandbox/` does not call `subprocess.run`, `os.system`, or `Popen` directly — anything that *could* go through the sandbox does. The one deliberate exception is reading a task's `spec.yaml` seed at startup, which is backend-side configuration, not user code.

---

## Relationship with `Toolset`

`SandboxToolset` is an implementation of the `Toolset` interface (see [toolset.md](toolset.md)), where each method does an RPC through `SandboxProvider`. Pseudocode:

```python
class SandboxToolset:
    def __init__(self, provider: SandboxProvider, handle: SandboxHandle, event_sink: EventSink):
        self._provider = provider
        self._handle = handle
        self._event_sink = event_sink

    async def read(self, path: str, ...) -> str:
        await self._event_sink.emit("tool_call", {...})
        try:
            content = await self._provider.read_file(self._handle, path)
            await self._event_sink.emit("tool_result", {... "is_error": False})
            return content
        except Exception:
            await self._event_sink.emit("tool_result", {... "is_error": True})
            raise
```

Event emission and exception handling are the same as in [toolset.md](toolset.md). This is the "one contract, both runtimes" property.

---

## Open questions

- **Alternative providers.** Custom Docker pool / a K8s fleet / a managed sandbox service — revisit when scale demands it. See [architecture.md](../architecture.md).
- **Persistent volume vs ephemeral.** The current interface is ephemeral; a persistent volume could be added if long sessions need it.
- **Network isolation.** The sandbox runs with default-deny egress (`--network none`); see [architecture.md](../architecture.md), Security.
