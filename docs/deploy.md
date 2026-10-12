# Production deployment template

This is an **unverified Linux deployment template**, not a tested production
quick-start. Use the [README quick-start](../README.md#self-host-quick-start) for
local development. Run one backend worker; multiple workers are not supported.

The repo ships **two** Docker stacks:

| File | Purpose |
|---|---|
| `docker-compose.yml` | **Dev only.** Source + `docker.sock` bind mounts, published `:5432`, hot-reload dev servers, committed `vibony/vibony` creds. **Never deploy this.** |
| `docker-compose.prod.yml` | **Production template.** Built prod images, gated Docker access, one-shot migrations, no published DB, env-supplied secrets; requires host verification. |

This file is the deploy contract; update it in the same commit when the prod stack, its env, or the hardening posture changes.

## Deployment controls

- the backend does **not** mount the raw host `docker.sock`. It reaches the daemon through a `docker-socket-proxy` (read-only socket, only the `CONTAINERS`/`IMAGES`/`EXEC`/`NETWORKS` API sections enabled) over `DOCKER_HOST=tcp://docker-socket-proxy:2375`, and runs as a non-root user. The enabled container APIs still require careful host isolation; the proxy alone is not a complete security boundary.
- prod images (`backend/Dockerfile.prod`, `dashboard/Dockerfile.prod`): backend installs `--only main` deps and runs `uvicorn` without `--reload`; the dashboard is a real `vite build` served as static files by Caddy (no Vite/esbuild dev server — GHSA-67mh-4wv8-2f99).
- DB migrations run as a one-shot `migrate` service (`alembic upgrade head`) that must complete before the backend starts; they are **not** in the backend CMD.
- DB creds + TLS come from env (`VIBONY_DATABASE_URL` with `?ssl=verify-full`, a rotated `POSTGRES_PASSWORD`); the backend `assert_production_config()` startup guard refuses the committed default creds or a TLS-less URL when `VIBONY_ENV=production`.
- `/docs`, `/redoc`, `/openapi.json` are off unless `VIBONY_ENABLE_DOCS=1`.
- `VIBONY_FRONTEND_URL` must be set to an `https://` URL in production (startup guard).
- SMTP uses STARTTLS with certificate + hostname verification.
- `/ready` (DB ping → 503 when unreachable) drives the healthcheck; the backend container has `mem_limit`/`cpus`/`pids_limit`.
- no source or socket mounts; Postgres is `expose`d to the app network only, never published to the host.
- uvicorn access logging is off in the prod CMD (`--no-access-log`) so candidate/share tokens and OIDC codes that travel in query strings are not logged.
- litellm anonymous telemetry is disabled in code (`litellm.telemetry = False`).

## Required env (root `.env`, NOT committed)

```
VIBONY_ENV=production
POSTGRES_USER=app
POSTGRES_PASSWORD=<rotated, strong>
POSTGRES_DB=vibony
VIBONY_DATABASE_URL=postgresql+asyncpg://app:<pw>@db.example.com:5432/vibony?ssl=verify-full
VIBONY_SESSION_SECRET=<openssl rand -hex 32>
VIBONY_FRONTEND_URL=https://app.example.com
VIBONY_COOKIE_SECURE=true
VIBONY_ALLOWED_HOSTS=app.example.com
VIBONY_ALLOWED_ORIGINS=https://app.example.com
VIBONY_SITE_ADDRESS=app.example.com        # Caddy auto-HTTPS (ACME)
VIBONY_SANDBOX_RUNTIME=runsc               # gVisor on a Linux host with gVisor installed
# Data residency (152-ФЗ): VIBONY_DATA_RESIDENCY=ru + VIBONY_INRU_LLM_PROVIDERS=...
# Provider key for VIBONY_DEFAULT_MODEL
```

The bundled Postgres service does not provision TLS certificates. Supply a
TLS-configured database and trusted certificates before using the production URL.

Bring up: `docker compose -f docker-compose.prod.yml up -d --build`.

## Operator checklist

- **Digest-pin base images.** Replace the floating tags in `*.Dockerfile.prod` and `docker-compose.prod.yml` (`python:3.12-slim`, `node:20-alpine`, `caddy:2-alpine`, `postgres:16`, `tecnativa/docker-socket-proxy:latest`) with `image@sha256:<digest>` pins. Left as floating tags in-repo because a pinned digest can't be resolved offline; pin at deploy time and record the digests in the deploy log.
- **Dev secrets stay in dev.** `infra/dev-oidc/dex.yaml` + the dev `docker-compose.yml` carry static dev secrets (the `dev-oidc` profile, `vibony/vibony`). They are dev-only and must never be reused in prod — the prod stack supplies every secret from env.
- **TLS.** Caddy terminates HTTPS via ACME when `VIBONY_SITE_ADDRESS` is a domain; otherwise front the stack with an external TLS terminator and keep `VIBONY_COOKIE_SECURE=true`.

## Residual — Linux-host verification (deferred)

The following have not been verified on a production Linux host and must be
smoke-tested on the target host before deployment:

- the `docker-socket-proxy` flag set actually permits the sandbox provider's create/exec/put-archive/remove calls over `tcp://`;
- the non-root prod backend + uid-1000 sandbox containers + read-only rootfs;
- gVisor `runsc` as the sandbox runtime.
