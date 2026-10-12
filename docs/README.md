# Vibony — Documentation

## For contributors

| Document | What's inside |
|----------|--------------|
| [architecture.md](architecture.md) | System architecture: components, data flow, tech choices |
| [Quick-start](../README.md#self-host-quick-start) | Run PostgreSQL, the backend and the dashboard locally |
| [CONTRIBUTING.md](../CONTRIBUTING.md) | Development workflow, tests and dependency audits |
| [deploy.md](deploy.md) | Unverified production deployment template and required checks |
| [weakness-format.md](weakness-format.md) | Format of `weakness.yaml` — AI assistant calibration config |

## Protocol contracts

[`protocol/`](protocol/) — source of truth for backend ↔ dashboard interfaces.

| File | Content |
|------|---------|
| [protocol/events.md](protocol/events.md) | Event envelope, all event types, correlation IDs |
| [protocol/ws-contract.md](protocol/ws-contract.md) | WebSocket endpoints, frame kinds, reconnect/replay |
| [protocol/session.md](protocol/session.md) | Session lifecycle, REST bootstrap, config schema |
| [protocol/task-format.md](protocol/task-format.md) | Task directory layout, bundle format |
| [protocol/toolset.md](protocol/toolset.md) | `Toolset` interface (6 methods), sandbox, event emission |
| [protocol/sandbox-provider.md](protocol/sandbox-provider.md) | `SandboxProvider` interface for container runtimes |

## Task authoring

[`task-design/format.md`](task-design/format.md) — criteria and spec template for vibe-coding interview tasks.
