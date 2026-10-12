# Vibony Protocol — the shared language of the components

**Context:** [architecture.md](../architecture.md).

This directory is the single source of truth for the contracts between the backend and the dashboard. It fixes the shape of the data before any component-specific code is written.

## Contents

| File | What it fixes | Who uses it |
|------|--------------|-------------|
| [events.md](events.md) | Catalog of event types (`llm_request`, `tool_call`, `file_changed`, ...), common fields (`seq`, `timestamp`, `type`, `payload`), numbering and validation rules | backend emits + persists, dashboard consumes |
| [ws-contract.md](ws-contract.md) | WebSocket frames for the candidate (`user_message` → event stream) and the interviewer, heartbeat, reconnect, replay by `last_seq` | backend ↔ dashboard |
| [session.md](session.md) | Session config (what the backend gives the client on startup), session lifecycle (pending/active/completed), `protocol_version` | backend, dashboard |
| [task-format.md](task-format.md) | Task folder structure on disk (`spec.yaml`, `task.md`, `INTERVIEWER.md`, `weakness.yaml`, scaffold files), what the backend reads on session startup | backend, task authors |
| [toolset.md](toolset.md) | The Python `Toolset` interface (Read/Edit/Bash/Watch/...) that the agent relies on. `SandboxToolset` wraps the sandbox provider | backend (agent loop) |
| [sandbox-provider.md](sandbox-provider.md) | The `SandboxProvider` interface (`create`/`exec`/`read`/`write`/`watch`/`teardown`) | backend (sandbox runtime) |

## Principles

1. **Contract ≠ implementation.** The documents here describe the shape of data and the promises components make to each other. The implementation (Pydantic models, OpenAPI schema, TypeScript types) is derived from these documents, not the other way around.
2. **One contract, runtime-agnostic.** The candidate runs in the browser IDE; the agent loop lives backend-side, hidden behind `Toolset` and the WS contract. The events, WS frames, and session config are runtime-agnostic by design.
3. **Versioning via `protocol_version: int`.** Not semver, not URL versioning. A client may be behind the deployed backend; on a mismatch, a clear error. Details are in [session.md](session.md).
4. **No TODO in the documents.** A field with no decision is decided before it lands here.
