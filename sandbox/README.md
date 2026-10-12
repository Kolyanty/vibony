# Sandbox images

Runtime images for Vibony web candidate sessions. The backend provisions one
container per session from a task's `runtime_image` (spec.yaml) and drives it
via `docker exec`.

## Images

| Tag | Dockerfile | What's inside |
|---|---|---|
| `vibony/sandbox-py:3.12` | `images/py/Dockerfile` | `python:3.12-slim` + `git`, `ca-certificates`, `poetry`, and **task-01 deps baked** (torch ≥2.3 CPU). |
| `vibony/sandbox-node:20` | `images/node/Dockerfile` | `node:20-slim` + `git`, `ca-certificates`. No app deps baked yet. |

Common to every image: working dir `/work` (the task bundle is extracted here
by the backend), entrypoint `sleep infinity` — the container waits; the backend
drives it via `docker exec`.

Deps are **baked at build time** so the running container can stay on
`--network none` (default-deny egress); the candidate never runs an install
against the network.

## Build

```bash
# From the monorepo root (vibony/):
docker build -t vibony/sandbox-py:3.12 sandbox/images/py
docker build -t vibony/sandbox-node:20 sandbox/images/node
```

A task selects its image via `runtime_image:` in `spec.yaml` (e.g. task-01 uses
`vibony/sandbox-py:3.12`). Tasks without `runtime_image` fall back to
`VIBONY_SANDBOX_IMAGE`.

## Network isolation

Containers run with `--network none` by default (default-deny egress).
Granular per-host allowlisting (`VIBONY_SANDBOX_EGRESS_ALLOWLIST`) is a planned
refinement. Set `VIBONY_SANDBOX_NETWORK=bridge` in dev to allow package installs.

## Resource limits

Controlled via backend env vars (applied per container):

| Variable | Default | Description |
|---|---|---|
| `VIBONY_SANDBOX_IMAGE` | `vibony/sandbox-py:3.12` | Fallback image when a task omits `runtime_image` |
| `VIBONY_SANDBOX_CPU_LIMIT` | `1.0` | CPU cores (Docker `--cpus`) |
| `VIBONY_SANDBOX_MEM_LIMIT` | `512m` | Memory limit |
| `VIBONY_SANDBOX_PIDS_LIMIT` | `256` | Max processes/threads (fork-bomb guard) |
| `VIBONY_SANDBOX_NOFILE` | `1024` | Max open file descriptors (`nofile` ulimit) |
