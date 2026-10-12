# Backend instructions

Inherits [../AGENTS.md](../AGENTS.md).

## Conventions and entry points

- Python `>=3.12,<3.14`; manage dependencies with Poetry and keep `poetry.lock` aligned.
- FastAPI handlers and database access are async. Use the engine/session helpers in
  `vibony_backend/db/engine.py`; synchronous psycopg is for Alembic migrations.
- Keep the cancellation-safe asyncpg dialect and session cleanup: ASGI cancellation
  must not interrupt rollback/connection termination, including pool pre-ping failures.
  Regression coverage lives in `tests/test_db_engine.py`.
- LLM calls go through `litellm.acompletion`, not individual provider SDKs.
- Use Python `logging`; do not log secrets, auth tokens or candidate content casually.
- `main.py` owns application startup, configuration guards and router registration.
- `auth/` owns password/OIDC login, workspaces, memberships and audit records.
- `sessions/` owns REST/WebSocket lifecycle, event persistence, replay and timers.
- `agent_host/` owns the assistant loop, tool calls, compaction and trap controller.
- `sandbox/` owns execution providers, limits and container lifecycle.
- `assessment/` assembles session evidence and produces assessment reports.
- Runtime tasks come from the database through `tasks/store.py`. Repository `tasks/`
  directories are imported by `tasks/importer.py` and seeded by `tasks/seed.py`.
- Update Alembic migrations for schema changes. Keep `alembic.ini` in runnable exports.
- Preserve tenant authorization, candidate/interviewer data separation, path confinement,
  execution limits and event ordering. Local sandbox mode is for trusted development.
- The controller is optional (`VIBONY_CONTROLLER_MODEL`) and may fail open; do not
  describe it as a guaranteed enforcement or security boundary.

## Development and tests

From the repository root, `docker compose up -d postgres` starts the development DB.
From `backend/`, install with `poetry install`, copy `.env.example` to `.env` only if
absent, configure it, then run:

```bash
poetry run alembic upgrade head
poetry run uvicorn vibony_backend.main:app --reload
poetry run pytest -q
```

Tests use `pytest-postgresql` to spawn a local Postgres process; they need `pg_ctl` and
`initdb` in PATH, or `PGCTL=/path/to/pg_ctl`. A running Compose DB does not satisfy this
fixture. Read `tests/conftest.py` before changing test setup.

- Mark async tests with `pytest.mark.asyncio(loop_scope="session")`, at module level
  for all-async files or per test for mixed files. The shared asyncpg pool belongs to
  the session loop; function loops can cause cascading failures and hangs.
- Keep test model/auth/local-sandbox defaults set before importing the app and loading
  dotenv. An explicit shell environment can override `setdefault`; avoid accidental
  Docker or paid-provider execution during unit tests.
- Mock LLM calls. Tests for `scripts.assessment_eval` also use mocks; the real harnesses
  are separate, paid, owner-run workflows (see root instructions).
- Docker-marked tests need a daemon and the image specified by the test. An unavailable
  daemon may justify a conditional skip; a merely unbuilt image should be built.
- Use `pytest -x` to diagnose the first failure before investigating cascading errors.
- SQLAlchemy and unraisable-exception warnings fail the suite; do not suppress connection
  cleanup failures. Fixture teardown must not roll back an already inactive transaction.
